"""SmartRecruiters platform scanner (registry form).

GET-paginated Posting API. Per-job description requires a secondary
GET; ``_fetch_smartrecruiters_description`` lives in ``ats_platforms.py``
because ``tests/test_smartrecruiters_scanner.py`` imports it directly.
This module calls it via lazy import to avoid a circular dependency.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from jobcannon.engine.ats_platforms._concurrency import get_page_fetch_concurrency
from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_platforms._registry import (
    BOARD_GONE_STATUSES,
    BoardGoneError,
    PlatformScanner,
    _auth_block_statuses,
    coerce_remote_bool,
    label_or_str,
)
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.location_canonical import JobLocation

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
# Per-board pagination budget. Raised 500 -> 2000 (matching Workday's effective
# budget) so large enterprise boards (e.g. AbbVie ~1460) are fully discovered.
_MAX_RESULTS = 2000
# Pacing for the LIST endpoint between successive page fetches. Pre-F1
# (commit b99e1d9) the list-endpoint cadence was incidentally paced by
# the per-matched-posting detail-fetch sleep in the same per-page loop.
# See .planning/specs/2026-05-26-polish-review-audit.md (MAJOR — Workday
# + SmartRecruiters pagination).
_PAGE_FETCH_SLEEP_S = 0.1


def _fetch_postings_with_completeness(
    slug: str, max_pages: int | None = None
) -> tuple[list[dict], bool]:
    """GET + paginate over SmartRecruiters /v1/companies/{slug}/postings, tracking completeness.

    Returns ``(postings, complete)`` where ``complete`` is ``True`` only
    when the board was **fully** fetched:

    - First-page error (network / HTTP / JSON) → ``complete=False``.
    - ``totalFound > _MAX_RESULTS`` → ``complete=False`` (board too large to paginate).
    - Pagination stops before ``total_fetched >= totalFound`` → ``complete=False``.
    - Genuine empty board (``totalFound=0``) → ``complete=True``.

    The completeness flag is the gate used by the ATS reconciler to decide
    whether expiry-reconciliation is safe for a SmartRecruiters company. A
    warning is logged whenever the board is incomplete so operators can see
    which companies exceed the pagination cap (#217).

    ``ats_reconciler.py`` calls this function directly today via a private
    import, bypassing the ``PlatformScanner.fetch_postings_with_completeness``
    registry field entirely. The field itself is forward-wiring for the
    wider reconciler chain (issues #1030-1033) and currently has no callers.

    Args:
        slug: SmartRecruiters company slug.
        max_pages: Optional page budget (not used by SmartRecruiters, which
            uses a _MAX_RESULTS cap instead). Kept for signature compatibility.
    """
    base_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    out: list[dict] = []
    total_fetched = 0
    saw_total = False
    total_found = 0
    pages_fetched = 0

    # Fetch page 1 serially to learn the total
    try:
        resp = get_session().get(
            base_url,
            params={"offset": 0, "limit": _PAGE_SIZE},
            headers={"Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("scan_smartrecruiters('%s') request failed: %s", slug, exc)
        return [], False

    if resp.status_code != 200:
        if resp.status_code in BOARD_GONE_STATUSES:
            raise BoardGoneError(resp.status_code, slug)
        if resp.status_code in _auth_block_statuses():
            logger.warning(
                "scan_smartrecruiters('%s') possible auth/anti-bot wall: HTTP %d",
                slug,
                resp.status_code,
            )
        else:
            logger.debug("scan_smartrecruiters('%s') returned HTTP %d", slug, resp.status_code)
        return [], False

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("scan_smartrecruiters('%s') JSON parse error: %s", slug, exc)
        return [], False

    total_found = data.get("totalFound", 0)
    saw_total = True
    pages_fetched += 1

    if total_found > _MAX_RESULTS:
        logger.warning(
            "scan_smartrecruiters('%s') board has %d postings; fetching first %d "
            "(cap) — discovery partial, reconciliation will skip this company",
            slug,
            total_found,
            _MAX_RESULTS,
        )

    postings = data.get("content", [])
    if not postings:
        return [], saw_total and total_found == 0

    out.extend(postings)
    total_fetched += len(postings)

    # If we've fetched everything or hit the cap, we're done
    if total_fetched >= total_found or total_fetched >= _MAX_RESULTS:
        complete = saw_total and total_fetched >= total_found
        return out, complete

    # Fetch remaining pages in parallel
    concurrency = get_page_fetch_concurrency()
    remaining_pages = min(
        (_MAX_RESULTS - total_fetched + _PAGE_SIZE - 1) // _PAGE_SIZE,
        (total_found - total_fetched + _PAGE_SIZE - 1) // _PAGE_SIZE,
    )

    if remaining_pages <= 0:
        complete = saw_total and total_fetched >= total_found
        return out, complete

    def _fetch_page(offset: int) -> tuple[int, list[dict]]:
        """Fetch one page, returning (offset, postings)."""
        time.sleep(_PAGE_FETCH_SLEEP_S)  # pacing
        try:
            resp = get_session().get(
                base_url,
                params={"offset": offset, "limit": _PAGE_SIZE},
                headers={"Accept": "application/json"},
                timeout=_PROBE_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.debug(
                    "scan_smartrecruiters('%s') page %d returned HTTP %d",
                    slug,
                    offset // _PAGE_SIZE,
                    resp.status_code,
                )
                return offset, []
            data = resp.json()
            page_postings = data.get("content", [])
            return offset, page_postings
        except Exception as exc:
            logger.debug(
                "scan_smartrecruiters('%s') page %d failed: %s", slug, offset // _PAGE_SIZE, exc
            )
            return offset, []

    # Build list of offsets for remaining pages
    offsets = [_PAGE_SIZE * (pages_fetched + i) for i in range(remaining_pages)]

    # Fetch in parallel
    page_results: list[tuple[int, list[dict]]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(_fetch_page, offset): offset for offset in offsets}
        for future in as_completed(futures):
            try:
                offset, page_postings = future.result()
                page_results.append((offset, page_postings))
            except Exception as exc:
                logger.debug("scan_smartrecruiters('%s') parallel fetch failed: %s", slug, exc)

    # Sort by offset to ensure deterministic output
    page_results.sort(key=lambda x: x[0])

    # Extend results in page order
    for _offset, page_postings in page_results:
        out.extend(page_postings)
        total_fetched += len(page_postings)
        pages_fetched += 1
        if total_fetched >= total_found or total_fetched >= _MAX_RESULTS:
            break

    complete = saw_total and total_fetched >= total_found
    return out, complete


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """GET + paginate over SmartRecruiters /v1/companies/{slug}/postings.

    Thin wrapper around :func:`_fetch_postings_with_completeness` — the
    completeness signal is consumed by the ATS reconciler but is not needed
    by the standard scanner flow.

    Args:
        slug: SmartRecruiters company slug.
        max_pages: Optional page budget (not used by SmartRecruiters, which
            uses a _MAX_RESULTS cap instead). Kept for signature compatibility.
    """
    postings = _fetch_postings_with_completeness(slug, max_pages)[0]
    # Stash the slug in each posting so _detail_fetch can use it
    for posting in postings:
        posting["__smartrecruiters_slug"] = slug
    return postings


def _to_canonical(posting: dict) -> list[JobLocation]:
    """Layer-1 mapping for SmartRecruiters posting → list[JobLocation].

    SmartRecruiters returns ``location.{city, region, regionCode, country,
    countryCode, remote}``. ``remote: true`` is the workplace_type signal.
    Single location per posting (no multi-location array on the v1 list
    endpoint).
    """
    loc = posting.get("location")
    if not isinstance(loc, dict):
        return []
    city = (loc.get("city") or "").strip() or None
    region = (loc.get("region") or "").strip() or None
    region_code = (loc.get("regionCode") or "").strip().upper() or None
    country = (loc.get("country") or "").strip() or None
    country_code = (loc.get("countryCode") or "").strip().upper() or None
    workplace_type = "REMOTE" if loc.get("remote") else "UNSPECIFIED"
    if (
        not any((city, region, region_code, country, country_code))
        and workplace_type == "UNSPECIFIED"
    ):
        return []
    raw = ", ".join(
        p
        for p in [loc.get("city"), loc.get("region"), loc.get("country")]
        if isinstance(p, str) and p
    )
    return [
        JobLocation(
            city=city,
            region=region,
            region_code=region_code,
            country=country,
            country_code=country_code,
            workplace_type=workplace_type,
            raw=raw,
            unresolved=False,
        )
    ]


def _detail_fetch(posting: dict) -> dict:
    """Fetch SmartRecruiters job description for one posting.

    Returns a dict with the fetched description under the key
    '__fetched_description'. The parallel fetch runner merges this
    into the posting dict before posting_to_job runs.

    This function is called by the ThreadPoolExecutor in run_platform_scan.
    """
    from jobcannon.engine.ats_platforms import _fetch_smartrecruiters_description

    # SmartRecruiters detail fetch needs the slug, which is not in the posting dict.
    # We'll store it in the posting during the fetch_postings phase.
    slug = posting.get("__smartrecruiters_slug", "")
    posting_id = posting.get("id")
    description = _fetch_smartrecruiters_description(slug, str(posting_id)) if posting_id else ""

    return {"__fetched_description": description}


def _posting_to_job(posting: dict, slug: str) -> dict:
    loc = posting.get("location", {})
    if isinstance(loc, dict):
        parts = [loc.get("city", ""), loc.get("region", ""), loc.get("country", "")]
        location = ", ".join(p for p in parts if isinstance(p, str) and p)
    else:
        location = ""

    posting_id = posting.get("id") or None
    source_url = f"https://jobs.smartrecruiters.com/{slug}/{posting_id}" if posting_id else ""

    # ── source_id (F-04: was missing on 100% of rows) ────────────────────────
    source_id = str(posting_id) if posting_id is not None else None

    # ── posted_date (F-02: optional, cheap key lookup) ────────────────────────
    # SmartRecruiters exposes ``releasedDate`` (ISO-8601, first publication) on
    # list-endpoint results. No fallback to ``postingStatusUpdatedOn`` (#360):
    # that field is last-status-change, not first-posted — a wrong date is
    # worse than no date (D-08).
    posted_date: str | None = posting.get("releasedDate") or None

    # Use pre-fetched description if available (parallel path), otherwise fetch
    # serially (fallback for tests or non-registry callers).
    if "__fetched_description" in posting:
        description = posting["__fetched_description"]
    else:
        # Lazy import for the serial fallback path
        from jobcannon.engine.ats_platforms import _fetch_smartrecruiters_description

        description = (
            _fetch_smartrecruiters_description(slug, str(posting_id)) if posting_id else ""
        )

    # ── Structured-field CAPTURE (#451) — raw-as-provided, no synthesis ───────
    # SmartRecruiters emits ``location.remote`` (bool) and the
    # ``typeOfEmployment`` / ``department`` objects ({id, label}).
    is_remote = coerce_remote_bool(loc.get("remote") if isinstance(loc, dict) else None)
    employment_type = label_or_str(posting.get("typeOfEmployment"))
    department = label_or_str(posting.get("department"))

    return {
        "title": posting.get("name", ""),
        "company_source": "SmartRecruiters",
        "location": location,
        "locations_structured": _to_canonical(posting),
        "description": description,
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "source_id": source_id,
        "posted_date": posted_date,
        "is_remote": is_remote,
        "employment_type": employment_type,
        "department": department,
    }


SCANNER = PlatformScanner(
    name="smartrecruiters",
    company_source="SmartRecruiters",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("name", ""),
    posting_to_job=_posting_to_job,
    detail_fetch=_detail_fetch,
    fetch_postings_with_completeness=_fetch_postings_with_completeness,
)

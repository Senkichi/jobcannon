"""Workday platform scanner (registry form).

Workday exposes a standardized POST JSON API across all tenants at
``/wday/cxs/{tenant}/{board}/jobs``. Slug format is ``"{subdomain}/{board}"``
(e.g. ``"walmart.wd5/WalmartExternal"``).

Per-job description requires a secondary GET against the detail
endpoint. ``_fetch_workday_description`` lives in ``_detail_fetchers.py``
(re-exported through ``ats_platforms/__init__.py``) because it is
imported directly from the package namespace by
``tests/engine/test_workday_scanner.py``; this module calls it via a lazy
import to avoid a circular dependency.

Layer-1 emission (Phase 48.02):
  - ``source_id``: the posting's ``externalPath`` (unique per job per
    Workday board; the Workday requisition ID is embedded in the path,
    e.g. ``"/job/Senior-Data-Scientist_R-12345"``). Using ``externalPath``
    rather than attempting to parse ``bulletFields`` array entries avoids
    reliance on tenant-specific field names while still providing a stable
    per-posting identifier.
  - ``posted_date``: parsed from ``postedOn`` (date string, typically
    ``"MM/DD/YYYY"`` or ISO ``"YYYY-MM-DD"``). Treated as UTC midnight.
  - ``locations_structured``: Layer-1 ``JobLocation`` list parsed from
    ``locationsText`` with workplace-type detection (REMOTE/HYBRID) and
    best-effort ``City, ST`` extraction for US addresses.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta

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
from jobcannon.engine.location_canonical import JobLocation, WorkplaceType, dedupe_locations

logger = logging.getLogger(__name__)

_PAGE_SIZE = 20
# Per-board page budget. At _PAGE_SIZE=20 the default of 100 pages covers
# boards up to 2,000 postings before discovery is marked incomplete. Tenants
# larger than the budget still return their first ``budget * _PAGE_SIZE``
# postings (so discovery is non-empty) with ``complete=False`` — the
# reconciler's completeness gate then declines expiry-reconciliation for that
# tenant, but discovery is no longer silently zeroed.  Tunable via
# ``config.ats.workday_max_pages`` threaded through ``run_ats_scan`` /
# ``reconcile_all_companies``.
_DEFAULT_MAX_PAGES = 100


# Pacing for the LIST endpoint between successive page fetches. Before the
# F1 pagination refactor, the list-endpoint cadence was incidentally paced by
# the per-matched-posting detail-fetch sleep in the same per-page loop.
# Restoring an explicit inter-page delay preserves the polite-pacing
# intent for high-page-count Workday tenants.
_PAGE_FETCH_SLEEP_S = 0.1


# ---------------------------------------------------------------------------
# Location-parsing helpers (Layer-1)
# ---------------------------------------------------------------------------

_REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.IGNORECASE)

# "City Name, XX" where XX is a 2-letter code (US state or CA province).
# Anchored so "Hybrid - San Francisco, CA" doesn't match the raw text;
# callers strip workplace-type prefixes before applying this pattern.
_US_CITY_STATE_RE = re.compile(r"^([A-Za-z][A-Za-z\s\-\.\']+),\s*([A-Z]{2})\s*$")

# Tokens that are purely workplace-type keywords (handled specially).
_WORKPLACE_ONLY_TOKENS: frozenset[str] = frozenset({"remote", "hybrid", "onsite", "on-site"})

# Prefixes that Workday sometimes prepends to a city ("Hybrid - City, ST").
_WORKPLACE_PREFIX_RE = re.compile(r"^(?:remote|hybrid|onsite|on-site)\s*[-–—]\s*", re.IGNORECASE)


def _detect_workplace_type(text: str) -> WorkplaceType:
    """Infer WorkplaceType from a location token string."""
    if _REMOTE_RE.search(text):
        return "REMOTE"
    if _HYBRID_RE.search(text):
        return "HYBRID"
    return "UNSPECIFIED"


def _to_canonical(posting: dict) -> list[JobLocation]:
    """Layer-1 mapping: Workday posting → list[JobLocation].

    Parses ``locationsText`` (a flat semicolon/pipe-separated string) into
    ``JobLocation`` objects. Each segment is:
      - A pure workplace-type keyword (``"Remote"``, ``"Hybrid"``) →
        workplace-type-only ``JobLocation`` with ``unresolved=True``.
      - A ``"City, ST"`` US pattern (after stripping any leading keyword
        prefix) → fully-structured ``JobLocation`` with ``unresolved=False``.
      - Anything else → ``unresolved=True`` preserving ``raw``.

    Multi-location postings (e.g. ``"New York, NY; Remote"``) produce one
    ``JobLocation`` per resolved segment. Duplicates are removed by
    ``dedupe_locations``.
    """
    locations_text = (posting.get("locationsText") or "").strip()
    if not locations_text:
        return []

    # Split on semicolons and pipes (Workday uses both as multi-location
    # separators depending on tenant configuration).
    segments = [s.strip() for s in re.split(r"[;|]", locations_text) if s.strip()]

    results: list[JobLocation] = []
    for segment in segments:
        workplace_type = _detect_workplace_type(segment)

        # Pure keyword segments — no city/region data to extract.
        if segment.lower() in _WORKPLACE_ONLY_TOKENS:
            results.append(
                JobLocation(
                    city=None,
                    region=None,
                    region_code=None,
                    country=None,
                    country_code=None,
                    workplace_type=workplace_type,
                    raw=segment,
                    unresolved=True,
                )
            )
            continue

        # Strip any leading workplace-type prefix before trying city parse.
        clean = _WORKPLACE_PREFIX_RE.sub("", segment).strip()

        m = _US_CITY_STATE_RE.match(clean)
        if m:
            city = m.group(1).strip()
            region_code = m.group(2).upper()
            results.append(
                JobLocation(
                    city=city,
                    region=None,
                    region_code=region_code,
                    country="United States",
                    country_code="US",
                    workplace_type=workplace_type,
                    raw=segment,
                    unresolved=False,
                )
            )
        else:
            # Can't structurally resolve — preserve raw for audit/display.
            results.append(
                JobLocation(
                    city=None,
                    region=None,
                    region_code=None,
                    country=None,
                    country_code=None,
                    workplace_type=workplace_type,
                    raw=segment,
                    unresolved=True,
                )
            )

    return dedupe_locations(results)


# Relative postedOn strings — what most real tenants emit (#364). At audit
# time (2026-06-11) 734 of the last 30 days' Workday jobs had NULL
# posted_date because only the two absolute formats below were recognised.
# "30+" parses as a 30-day floor: genuinely lossy, still a useful
# "not fresh" signal.
_RELATIVE_POSTED_RE = re.compile(
    r"^(?:posted\s+)?(?:(today)|(yesterday)|(\d+)\+?\s+days?\s+ago)$",
    re.IGNORECASE,
)


def _parse_posted_date(value: str | None) -> tuple[datetime | None, str | None]:
    """Parse a Workday ``postedOn`` string to ``(naive UTC datetime, precision)``.

    Formats seen across Workday tenants:
      - ``"MM/DD/YYYY"`` / ``"YYYY-MM-DD"`` absolute dates → ``'exact'``
      - ``"Posted Today"`` / ``"Posted Yesterday"`` /
        ``"Posted N Days Ago"`` / ``"Posted 30+ Days Ago"`` relative
        strings → date-level value computed against UTC now, ``'approximate'``
        (#364). This parses what the platform actually said — it is NOT
        synthesis from first_seen (D-08).

    Anything else (or empty) → ``(None, None)``; a NULL is written to
    ``posted_date``.
    """
    if not value:
        return None, None
    text = value.strip()
    # ISO date: "YYYY-MM-DD"
    try:
        return datetime.strptime(text, "%Y-%m-%d"), "exact"
    except ValueError:
        pass
    # US date: "MM/DD/YYYY"
    try:
        return datetime.strptime(text, "%m/%d/%Y"), "exact"
    except ValueError:
        pass
    m = _RELATIVE_POSTED_RE.match(text)
    if m:
        today_m, yesterday_m, n_days = m.groups()
        days = 0 if today_m else 1 if yesterday_m else int(n_days)
        utc_today = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        return utc_today - timedelta(days=days), "approximate"
    logger.debug("scan_workday: unrecognised postedOn format %r — skipping", value)
    return None, None


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------


def _fetch_postings_with_completeness(
    slug: str, max_pages: int | None = None
) -> tuple[list[dict], bool]:
    """POST + paginate over Workday CXS list endpoint, tracking completeness.

    Pagination runs up to a **page budget** (``max_pages``, default
    :data:`_DEFAULT_MAX_PAGES`). Boards larger than the budget still return
    the first ``max_pages * _PAGE_SIZE`` postings — discovery is never
    silently zeroed for a large tenant — but with
    ``complete=False`` so the reconciler declines expiry-reconciliation.

    Returns ``(postings, complete)`` where ``complete`` is ``True`` only
    when the board was **fully** fetched:

    - First-page error (network / HTTP / JSON) → ``([], False)``.
    - ``total`` exceeds what the page budget can fetch → ``(partial, False)``.
      ``partial`` holds every posting that did land (NOT ``[]``) so discovery
      gets the first N pages instead of nothing.
    - Pagination stops on a mid-run error before ``total_fetched >= total``
      → ``(partial, False)``.
    - Genuine empty board (``total=0``) → ``([], True)``.

    A ``([], False)`` (error) is therefore distinguishable from a
    ``([], True)`` (true zero): callers that must not mass-expire on a fetch
    failure key off ``complete``, not ``len(postings)``.

    In the private source, a dedicated ``ats_reconciler.py`` module called
    this function directly via a private import, bypassing the
    ``PlatformScanner.fetch_postings_with_completeness`` registry field
    entirely; that module did not survive extraction into this engine port.
    Reconciliation here is host-supplied instead (``services.py``'s
    ``reconcile_company_ats`` field, driven from ``ats_scanner/_promote.py``),
    and the registry field remains forward-wiring for the wider reconciler
    chain (issues #1030-1033) with currently no callers.

    Args:
        slug: ``"subdomain/board"`` Workday slug.
        max_pages: Per-board page budget. ``None`` falls back to
            :data:`_DEFAULT_MAX_PAGES`.

    A warning is logged whenever the board exceeds the page budget so
    operators can see which tenants are only partially discovered.
    """
    if max_pages is None or max_pages <= 0:
        effective_max_pages = _DEFAULT_MAX_PAGES
    else:
        effective_max_pages = max_pages

    parts = slug.split("/", 1)
    if len(parts) != 2:
        logger.warning("scan_workday: invalid slug format '%s'", slug)
        return [], False

    subdomain, board = parts
    dot_wd_idx = subdomain.find(".wd")
    tenant = subdomain[:dot_wd_idx] if dot_wd_idx > 0 else subdomain

    api_url = f"https://{subdomain}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"
    out: list[dict] = []
    total_fetched = 0
    saw_total = False
    total = 0
    pages_fetched = 0

    # Fetch page 1 serially to learn the total
    body = {
        "appliedFacets": {},
        "limit": _PAGE_SIZE,
        "offset": 0,
        "searchText": "",
    }
    try:
        resp = get_session().post(
            api_url,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("scan_workday('%s') request failed: %s", slug, exc)
        return [], False

    if resp.status_code != 200:
        if resp.status_code in BOARD_GONE_STATUSES:
            raise BoardGoneError(resp.status_code, slug)
        if resp.status_code in _auth_block_statuses():
            logger.warning(
                "scan_workday('%s') possible auth/anti-bot wall: HTTP %d",
                slug,
                resp.status_code,
            )
        else:
            logger.debug("scan_workday('%s') returned HTTP %d", slug, resp.status_code)
        return [], False

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("scan_workday('%s') JSON parse error: %s", slug, exc)
        return [], False

    total = data.get("total", 0)
    saw_total = True
    pages_fetched += 1

    postings = data.get("jobPostings", [])
    if not postings:
        return [], saw_total and total == 0

    # Stash the slug-derived URL parts on each posting so _posting_to_job
    # can build source_url + call the detail endpoint without re-parsing.
    for posting in postings:
        posting["__workday_subdomain"] = subdomain
        posting["__workday_tenant"] = tenant
        posting["__workday_board"] = board
    out.extend(postings)

    total_fetched += len(postings)

    # If we've fetched everything or hit the budget, we're done
    if total_fetched >= total or pages_fetched >= effective_max_pages:
        complete = saw_total and total_fetched >= total
        if saw_total and not complete and total > total_fetched:
            logger.warning(
                "scan_workday('%s') board has %d postings; fetched %d in %d pages "
                "(budget %d pages) — discovery partial, reconciliation will skip "
                "this tenant",
                slug,
                total,
                total_fetched,
                pages_fetched,
                effective_max_pages,
            )
        return out, complete

    # Fetch remaining pages in parallel
    concurrency = get_page_fetch_concurrency()
    remaining_pages = min(
        effective_max_pages - pages_fetched,
        (total - total_fetched + _PAGE_SIZE - 1) // _PAGE_SIZE,
    )

    if remaining_pages <= 0:
        complete = saw_total and total_fetched >= total
        return out, complete

    def _fetch_page(offset: int) -> tuple[int, list[dict]]:
        """Fetch one page, returning (offset, postings)."""
        time.sleep(_PAGE_FETCH_SLEEP_S)  # pacing
        body = {
            "appliedFacets": {},
            "limit": _PAGE_SIZE,
            "offset": offset,
            "searchText": "",
        }
        try:
            resp = get_session().post(
                api_url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=_PROBE_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.debug(
                    "scan_workday('%s') page %d returned HTTP %d",
                    slug,
                    offset // _PAGE_SIZE,
                    resp.status_code,
                )
                return offset, []
            data = resp.json()
            page_postings = data.get("jobPostings", [])
            # Stash slug-derived URL parts
            for posting in page_postings:
                posting["__workday_subdomain"] = subdomain
                posting["__workday_tenant"] = tenant
                posting["__workday_board"] = board
            return offset, page_postings
        except Exception as exc:
            logger.debug("scan_workday('%s') page %d failed: %s", slug, offset // _PAGE_SIZE, exc)
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
                logger.debug("scan_workday('%s') parallel fetch failed: %s", slug, exc)

    # Sort by offset to ensure deterministic output
    page_results.sort(key=lambda x: x[0])

    # Extend results in page order
    for _offset, page_postings in page_results:
        out.extend(page_postings)
        total_fetched += len(page_postings)
        pages_fetched += 1
        if total_fetched >= total:
            break

    complete = saw_total and total_fetched >= total
    if saw_total and not complete and total > total_fetched:
        logger.warning(
            "scan_workday('%s') board has %d postings; fetched %d in %d pages "
            "(budget %d pages) — discovery partial, reconciliation will skip "
            "this tenant",
            slug,
            total,
            total_fetched,
            pages_fetched,
            effective_max_pages,
        )
    return out, complete


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """POST + paginate over Workday CXS list endpoint.

    Returns the raw posting list; description fetches happen later in
    ``_posting_to_job`` so the title-match gate runs first and we only
    pay for detail fetches on matched postings.

    Thin wrapper around :func:`_fetch_postings_with_completeness` — the
    completeness signal is consumed by the ATS reconciler but is not
    needed by the standard scanner flow.

    Args:
        slug: ``"subdomain/board"`` Workday slug.
        max_pages: Per-board page budget. ``None`` falls back to
            :data:`_DEFAULT_MAX_PAGES`.
    """
    return _fetch_postings_with_completeness(slug, max_pages)[0]


def _detail_fetch(posting: dict) -> dict:
    """Fetch Workday job description for one posting.

    Returns a dict with the fetched description under the key
    '__fetched_description'. The parallel fetch runner merges this
    into the posting dict before posting_to_job runs.

    This function is called by the ThreadPoolExecutor in run_platform_scan.
    """
    # Lazy import — _fetch_workday_description lives in _detail_fetchers.py,
    # re-exported through ats_platforms/__init__.py because
    # tests/engine/test_workday_scanner.py imports it from the package
    # namespace directly, and this module must not depend on the package's
    # __init__ at import time (would risk a cycle once __init__ delegates
    # back to run_platform_scan).
    from jobcannon.engine.ats_platforms import _fetch_workday_description

    subdomain = posting.get("__workday_subdomain", "")
    tenant = posting.get("__workday_tenant", "")
    board = posting.get("__workday_board", "")
    external_path = posting.get("externalPath", "")

    description = (
        _fetch_workday_description(subdomain, tenant, board, external_path) if external_path else ""
    )

    return {"__fetched_description": description}


def _posting_to_job(posting: dict, _slug: str) -> dict:
    subdomain = posting.get("__workday_subdomain", "")
    board = posting.get("__workday_board", "")
    external_path = posting.get("externalPath", "")
    location = posting.get("locationsText", "")

    # externalPath from the CXS API already begins with "/job/...".
    # Do NOT prepend another "/job/" — earlier templates emitted
    # "/job//job/..." URLs that 406'd at the API.
    source_url = (
        f"https://{subdomain}.myworkdayjobs.com/en-US/{board}{external_path}"
        if external_path
        else ""
    )

    # Use pre-fetched description if available (parallel path), otherwise fetch
    # serially (fallback for tests or non-registry callers).
    if "__fetched_description" in posting:
        description = posting["__fetched_description"]
    else:
        # Lazy import for the serial fallback path
        from jobcannon.engine.ats_platforms import _fetch_workday_description

        tenant = posting.get("__workday_tenant", "")
        description = (
            _fetch_workday_description(subdomain, tenant, board, external_path)
            if external_path
            else ""
        )

    # --- Layer-1 emission (Phase 48.02) ------------------------------------
    # source_id: use externalPath as the stable per-job identifier.
    # externalPath is unique per posting per board (e.g. "/job/Title_R-12345")
    # and is already the key used to build source_url and fetch descriptions.
    # Using the full path avoids parsing the requisition suffix, which varies
    # by tenant configuration.
    source_id: str | None = external_path if external_path else None

    # posted_date: parsed from postedOn (varies by tenant format). Relative
    # strings yield 'approximate' precision; absolute dates 'exact' (#364).
    posted_date, posted_date_precision = _parse_posted_date(posting.get("postedOn"))

    # locations_structured: Layer-1 parse of locationsText with
    # workplace-type detection and best-effort City, ST extraction.
    locations_structured = _to_canonical(posting)
    # -----------------------------------------------------------------------

    # ── Structured-field CAPTURE (#451) — raw-as-provided, no synthesis ───────
    # The Workday CXS list payload does not reliably surface remote /
    # employment-type / department fields; read the candidate keys defensively
    # so any tenant that does emit them is captured, and fall to None otherwise.
    is_remote = coerce_remote_bool(
        posting.get("isRemote") if posting.get("isRemote") is not None else posting.get("remote")
    )
    employment_type = (
        label_or_str(posting.get("employmentType"))
        or label_or_str(posting.get("typeOfEmployment"))
        or label_or_str(posting.get("jobType"))
    )
    department = label_or_str(posting.get("department")) or label_or_str(posting.get("team"))

    return {
        "title": posting.get("title", ""),
        "company_source": "Workday",
        "location": location,
        "locations_structured": locations_structured,
        "description": description,
        "source_url": source_url,
        "source_id": source_id,
        "posted_date": posted_date,
        "posted_date_precision": posted_date_precision,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "is_remote": is_remote,
        "employment_type": employment_type,
        "department": department,
    }


SCANNER = PlatformScanner(
    name="workday",
    company_source="Workday",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
    detail_fetch=_detail_fetch,
    fetch_postings_with_completeness=_fetch_postings_with_completeness,
)

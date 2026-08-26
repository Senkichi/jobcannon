"""Oracle Recruiting Cloud (ORC / Fusion Candidate Experience) platform scanner.

Oracle's Fusion HCM Recruiting exposes a public, unauthenticated REST finder for
every Candidate-Experience site::

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true
        &expand=requisitionList.workLocation,requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},limit={N},offset={M},sortBy=POSTING_DATES_DESC

``{host}`` is the full Fusion pod hostname (``{pod}.fa.{region}.oraclecloud.com``,
e.g. ``ibtcjb.fa.ocs.oraclecloud.com``) and ``{site}`` is the CE site number
(``CX_1`` is the near-universal default for single-site tenants). The registry
``slug`` packs both as ``"{host}|{site}"`` — analogous to Workday's
``"{subdomain}/{board}"`` and Eightfold's ``"host|domain"``.

Response shape: ``{"items": [{"TotalJobsCount": N, "requisitionList": [...]}]}``.
Each requisition carries ``Id``, ``Title``, ``PostedDate`` (already ISO),
``PrimaryLocation``, ``WorkplaceTypeCode`` and a short ``ShortDescriptionStr``.
The list endpoint omits the full job description; ``jd_full`` is filled later by
enrichment from the per-requisition detail endpoint.

Offset pagination (page size :data:`_PAGE_SIZE`) up to ``TotalJobsCount``, capped
at :data:`_MAX_RESULTS`. A first-page 404/410 means the pod/site stopped
resolving → :class:`BoardGoneError` so the stale ``hit`` is demoted. Any other
error returns ``[]``.
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
from jobcannon.engine.location_parser import parse_locations

logger = logging.getLogger(__name__)

_REST_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_PAGE_SIZE = 50
_MAX_RESULTS = 2000
_PAGE_FETCH_SLEEP_S = 0.2
_DEFAULT_SITE = "CX_1"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _split_slug(slug: str) -> tuple[str, str]:
    """``"{host}|{site}"`` → ``(host, site)``; missing site defaults to CX_1."""
    host, _, site = (slug or "").partition("|")
    return host.strip(), (site.strip() or _DEFAULT_SITE)


def _job_url(host: str, site: str, req_id: str) -> str:
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req_id}"


def _is_remote(posting: dict) -> bool | None:
    """Tri-state remote flag from Oracle's WorkplaceTypeCode / WorkplaceType."""
    code = (posting.get("WorkplaceTypeCode") or "").upper()
    if "REMOTE" in code:
        return True
    if "ON_SITE" in code or "HYBRID" in code:
        return False
    text = (posting.get("WorkplaceType") or "").lower()
    if "remote" in text:
        return True
    if "on-site" in text or "hybrid" in text:
        return False
    return coerce_remote_bool(None)


def _fetch_postings_with_completeness(
    slug: str, max_pages: int | None = None
) -> tuple[list[dict], bool]:
    """Fetch one ORC Candidate-Experience site, offset-paginated, tracking completeness.

    Returns ``(postings, complete)`` where ``complete`` is ``True`` only
    when the board was **fully** fetched:

    - First-page error (network / HTTP / JSON) → ``([], False)``.
    - ``total > _MAX_RESULTS`` → ``complete=False`` (board too large to paginate).
    - Pagination stops before ``total_fetched >= total`` → ``complete=False``.
    - Genuine empty board (``total=0``) → ``complete=True``.

    The completeness flag is the gate used by the ATS reconciler to decide
    whether expiry-reconciliation is safe for an Oracle Cloud company. A
    warning is logged whenever the board is incomplete so operators can see
    which companies exceed the pagination cap.

    Args:
        slug: Oracle Cloud slug in format "{host}|{site}".
        max_pages: Optional page budget (not used by Oracle Cloud, which
            uses a _MAX_RESULTS cap instead). Kept for signature compatibility.
    """
    host, site = _split_slug(slug)
    if not host:
        return [], False

    base = f"https://{host}{_REST_PATH}"
    out: list[dict] = []
    total_fetched = 0
    pages_fetched = 0
    saw_total = False

    # Fetch page 1 serially to learn the total
    url = (
        f"{base}?onlyData=true"
        "&expand=requisitionList.workLocation,requisitionList.secondaryLocations"
        f"&finder=findReqs;siteNumber={site},limit={_PAGE_SIZE},"
        f"offset=0,sortBy=POSTING_DATES_DESC"
    )
    try:
        resp = get_session().get(url, headers=_HEADERS, timeout=_PROBE_TIMEOUT)
    except Exception as exc:
        logger.warning("scan_oracle_cloud('%s') request failed: %s", slug, exc)
        return [], False

    if resp.status_code in BOARD_GONE_STATUSES:
        raise BoardGoneError(resp.status_code, slug)
    if resp.status_code != 200:
        if resp.status_code in _auth_block_statuses():
            logger.warning(
                "scan_oracle_cloud('%s') possible auth/anti-bot wall: HTTP %d",
                slug,
                resp.status_code,
            )
        else:
            logger.debug("scan_oracle_cloud('%s') returned HTTP %d", slug, resp.status_code)
        return [], False

    try:
        payload = resp.json()
    except Exception as exc:
        logger.warning("scan_oracle_cloud('%s') JSON parse error: %s", slug, exc)
        return [], False

    items = payload.get("items") or []
    if not items:
        return [], True
    item0 = items[0] or {}
    reqs = item0.get("requisitionList") or []
    if not reqs:
        return [], True

    out.extend(reqs)
    total_fetched += len(reqs)
    pages_fetched += 1

    total = int(item0.get("TotalJobsCount") or 0)
    saw_total = True

    # If we've fetched everything or hit the cap, we're done
    if total_fetched >= total or total_fetched >= _MAX_RESULTS or len(reqs) < _PAGE_SIZE:
        complete = saw_total and total_fetched >= total
        if saw_total and not complete and total > total_fetched:
            logger.warning(
                "scan_oracle_cloud('%s') board has %d postings; fetched %d in %d pages "
                "(cap %d) — discovery partial, reconciliation will skip expiry-reconciliation",
                slug,
                total,
                total_fetched,
                pages_fetched,
                _MAX_RESULTS,
            )
        return out, complete

    # Fetch remaining pages in parallel
    concurrency = get_page_fetch_concurrency()
    remaining_pages = min(
        (_MAX_RESULTS - total_fetched + _PAGE_SIZE - 1) // _PAGE_SIZE,
        (total - total_fetched + _PAGE_SIZE - 1) // _PAGE_SIZE,
    )

    if remaining_pages <= 0:
        complete = saw_total and total_fetched >= total
        if saw_total and not complete and total > total_fetched:
            logger.warning(
                "scan_oracle_cloud('%s') board has %d postings; fetched %d in %d pages "
                "(cap %d) — discovery partial, reconciliation will skip expiry-reconciliation",
                slug,
                total,
                total_fetched,
                pages_fetched,
                _MAX_RESULTS,
            )
        return out, complete

    def _fetch_page(offset: int) -> tuple[int, list[dict]]:
        """Fetch one page, returning (offset, requisitions)."""
        time.sleep(_PAGE_FETCH_SLEEP_S)  # pacing
        url = (
            f"{base}?onlyData=true"
            "&expand=requisitionList.workLocation,requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={site},limit={_PAGE_SIZE},"
            f"offset={offset},sortBy=POSTING_DATES_DESC"
        )
        try:
            resp = get_session().get(url, headers=_HEADERS, timeout=_PROBE_TIMEOUT)
            if resp.status_code != 200:
                logger.debug(
                    "scan_oracle_cloud('%s') page %d returned HTTP %d",
                    slug,
                    offset // _PAGE_SIZE,
                    resp.status_code,
                )
                return offset, []
            payload = resp.json()
            page_items = payload.get("items") or []
            if not page_items:
                return offset, []
            page_item0 = page_items[0] or {}
            page_reqs = page_item0.get("requisitionList") or []
            return offset, page_reqs
        except Exception as exc:
            logger.debug(
                "scan_oracle_cloud('%s') page %d failed: %s", slug, offset // _PAGE_SIZE, exc
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
                offset, page_reqs = future.result()
                page_results.append((offset, page_reqs))
            except Exception as exc:
                logger.debug("scan_oracle_cloud('%s') parallel fetch failed: %s", slug, exc)

    # Sort by offset to ensure deterministic output
    page_results.sort(key=lambda x: x[0])

    # Extend results in page order. Deliberately do NOT break on
    # `len(page_reqs) < _PAGE_SIZE` here (unlike the first-page check above,
    # where a short page is a reliable "no more pages" signal from a
    # standalone serial fetch): a failed page in the parallel pool also
    # returns `[]` (len 0 < _PAGE_SIZE), and a short-page break would
    # misclassify that failure as "end of data" and silently discard every
    # already-fetched HIGHER-offset page too — one bad HTTP response
    # cascading into mass data loss instead of degrading just that page
    # (house standard: failure isolation). `total_fetched >=
    # total` (computed from the precomputed, total-bounded offset list) is
    # the sole termination signal, matching the Workday/SmartRecruiters
    # pattern.
    for _offset, page_reqs in page_results:
        out.extend(page_reqs)
        total_fetched += len(page_reqs)
        pages_fetched += 1
        if total_fetched >= total or total_fetched >= _MAX_RESULTS:
            break

    complete = saw_total and total_fetched >= total
    if saw_total and not complete and total > total_fetched:
        logger.warning(
            "scan_oracle_cloud('%s') board has %d postings; fetched %d in %d pages "
            "(cap %d) — discovery partial, reconciliation will skip expiry-reconciliation",
            slug,
            total,
            total_fetched,
            pages_fetched,
            _MAX_RESULTS,
        )
    return out, complete


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """Fetch one ORC Candidate-Experience site, offset-paginated.

    Thin wrapper around :func:`_fetch_postings_with_completeness` — the
    completeness signal is consumed by the ATS reconciler but is not
    needed by the standard scanner flow.

    Args:
        slug: Oracle Cloud slug in format "{host}|{site}".
        max_pages: Optional page budget (not used by Oracle Cloud, which
            uses a _MAX_RESULTS cap instead). Kept for signature compatibility.
    """
    return _fetch_postings_with_completeness(slug, max_pages)[0]


def _posting_to_job(posting: dict, slug: str) -> dict | None:
    req_id = posting.get("Id")
    if req_id is None:
        return None
    host, site = _split_slug(slug)
    location = posting.get("PrimaryLocation") or ""

    return {
        "title": posting.get("Title", ""),
        "company_source": "Oracle Cloud",
        "location": location,
        "locations_structured": parse_locations(location),
        # List endpoint only carries a short blurb; jd_full is filled by
        # enrichment from the per-requisition detail endpoint.
        "description": posting.get("ShortDescriptionStr", "") or "",
        "source_url": _job_url(host, site, str(req_id)),
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "source_id": str(req_id),
        "posted_date": posting.get("PostedDate") or None,
        "is_remote": _is_remote(posting),
        "employment_type": label_or_str(posting.get("JobSchedule")),
        "department": label_or_str(posting.get("Department") or posting.get("Organization")),
    }


SCANNER = PlatformScanner(
    name="oracle_cloud",
    company_source="Oracle Cloud",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("Title", ""),
    posting_to_job=_posting_to_job,
    fetch_postings_with_completeness=_fetch_postings_with_completeness,
)

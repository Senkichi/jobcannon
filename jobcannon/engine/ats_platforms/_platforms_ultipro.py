"""UKG Pro Recruiting (formerly UltiPro) platform scanner.

UKG Pro Recruiting exposes a public, unauthenticated JSON search endpoint per
job board::

    POST https://{host}/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults
    Content-Type: application/json
    {"opportunitySearch": {"Top": N, "Skip": M, "QueryString": "", ...},
     "matchCriteria": {...}}

``{host}`` is the recruiting subdomain (``recruiting2.ultipro.com``), ``{tenant}``
is the customer code (``JAN1000JANI``), and ``{board}`` is the board GUID — all
three live in the careers URL ``https://{host}/{tenant}/JobBoard/{board}``. The
registry ``slug`` packs them as ``"{host}/{tenant}/{board}"`` (Workday-style).

Response shape: ``{"opportunities": [...], "totalCount": N, "locations": [...]}``.
Each opportunity carries ``Id`` (GUID), ``Title``, ``RequisitionNumber``,
``FullTime``, ``JobCategoryName``, a ``Locations`` array, ``PostedDate`` (ISO
datetime) and a short ``BriefDescription``. The list omits the full job text;
``jd_full`` is filled later by enrichment.

``Top``/``Skip`` pagination up to ``totalCount``, capped at :data:`_MAX_RESULTS`.
A first-page 404/410 means the board GUID stopped resolving →
:class:`BoardGoneError`. Any other error returns ``[]``.
"""

from __future__ import annotations

import logging
import time

from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_platforms._registry import (
    BOARD_GONE_STATUSES,
    BoardGoneError,
    PlatformScanner,
    _auth_block_statuses,
)
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.location_parser import parse_locations

logger = logging.getLogger(__name__)

_PAGE_SIZE = 50
_MAX_RESULTS = 2000
_PAGE_FETCH_SLEEP_S = 0.2
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
}


def _split_slug(slug: str) -> tuple[str, str, str]:
    """``"{host}/{tenant}/{board}"`` → ``(host, tenant, board)``; ``("","","")`` if malformed."""
    parts = (slug or "").split("/")
    if len(parts) >= 3 and all(parts[:3]):
        return parts[0], parts[1], parts[2]
    return "", "", ""


def _search_body(top: int, skip: int) -> dict:
    return {
        "opportunitySearch": {
            "Top": top,
            "Skip": skip,
            "QueryString": "",
            "OrderBy": [
                {"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}
            ],
            "Filters": [],
        },
        "matchCriteria": {
            "PreferredJobs": [],
            "Educations": [],
            "LicenseAndCertifications": [],
            "Skills": [],
            "WorkExperiences": [],
            "DegreeFlexFields": [],
            "IsCurrentlyEmployed": False,
            "IsWillingToRelocate": False,
            "IsWillingToTravel": False,
            "EmploymentDesiredFlexFields": [],
        },
    }


def _location(posting: dict) -> str:
    locs = posting.get("Locations") or []
    if not locs:
        return ""
    first = locs[0] or {}
    name = first.get("LocalizedName")
    if name:
        return name
    addr = first.get("Address") or {}
    city = addr.get("City")
    state = (addr.get("State") or {}).get("Code")
    return ", ".join(p for p in (city, state) if p)


def _is_remote(posting: dict) -> bool | None:
    # JobLocationType is usually a string ("Remote"/"On-site") but some tenants
    # emit a numeric enum — coerce to str before matching (no signal on an
    # unrecognized code rather than a crash).
    text = str(posting.get("JobLocationType") or "").lower()
    if "remote" in text:
        return True
    if "onsite" in text or "on-site" in text or "hybrid" in text:
        return False
    return None


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """POST-paginate one UKG Pro job board. Raises :class:`BoardGoneError` on a
    first-page 404/410; returns ``[]`` on any other error.

    Args:
        slug: ``"host/tenant/board"`` UKG Pro slug.
        max_pages: Kept for signature compatibility; not applicable — UKG Pro
            paginates by a fixed ``_MAX_RESULTS`` offset cap instead.
    """
    host, tenant, board = _split_slug(slug)
    if not host:
        return []

    url = f"https://{host}/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults"
    out: list[dict] = []
    skip = 0

    while skip < _MAX_RESULTS:
        if skip > 0:
            time.sleep(_PAGE_FETCH_SLEEP_S)
        try:
            resp = get_session().post(
                url, json=_search_body(_PAGE_SIZE, skip), headers=_HEADERS, timeout=_PROBE_TIMEOUT
            )
        except Exception as exc:
            logger.warning("scan_ultipro('%s') request failed: %s", slug, exc)
            break

        if skip == 0 and resp.status_code in BOARD_GONE_STATUSES:
            raise BoardGoneError(resp.status_code, slug)
        if resp.status_code != 200:
            if resp.status_code in _auth_block_statuses():
                logger.warning(
                    "scan_ultipro('%s') possible auth/anti-bot wall: HTTP %d",
                    slug,
                    resp.status_code,
                )
            else:
                logger.debug("scan_ultipro('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            payload = resp.json()
        except Exception as exc:
            logger.warning("scan_ultipro('%s') JSON parse error: %s", slug, exc)
            break

        ops = payload.get("opportunities") or []
        if not ops:
            break
        out.extend(ops)

        total = int(payload.get("totalCount") or 0)
        skip += _PAGE_SIZE
        if skip >= total or len(ops) < _PAGE_SIZE:
            break

    return out


def _posting_to_job(posting: dict, slug: str) -> dict | None:
    op_id = posting.get("Id")
    if not op_id:
        return None
    host, tenant, board = _split_slug(slug)
    location = _location(posting)
    full_time = str(posting.get("FullTime")).lower() == "true"

    return {
        "title": posting.get("Title", ""),
        "company_source": "UltiPro",
        "location": location,
        "locations_structured": parse_locations(location),
        "description": posting.get("BriefDescription", "") or "",
        "source_url": (
            f"https://{host}/{tenant}/JobBoard/{board}/OpportunityDetail?opportunityId={op_id}"
        ),
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "source_id": str(op_id),
        "posted_date": (posting.get("PostedDate") or "")[:10] or None,
        "is_remote": _is_remote(posting),
        "employment_type": "Full Time" if full_time else None,
        "department": posting.get("JobCategoryName") or None,
    }


SCANNER = PlatformScanner(
    name="ultipro",
    company_source="UltiPro",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("Title", ""),
    posting_to_job=_posting_to_job,
)

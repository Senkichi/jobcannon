"""ADP Workforce Now platform scanner.

ADP Workforce Now exposes a public, unauthenticated JSON endpoint for job
requisitions::

    GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions
        ?cid={clientId}
        &ccId=19000101_000001
        &lang=en_US
        &locale=en_US

``{clientId}`` is the ADP client ID (UUID format, e.g. ``a6717ebc-f6a8-4a51-856b-f7ebd573645e``).
The ``ccId`` parameter is a constant for the public career center (``19000101_000001``).
The registry ``slug`` is the client ID UUID.

Response shape: ``{"jobRequisitions": [...]}``. Each requisition carries
``itemID``, ``requisitionTitle``, ``postDate`` (ISO datetime), and a
``customFieldGroup`` with ``stringFields`` containing the ``ExternalJobID``.
The list endpoint omits the full job description; ``jd_full`` is filled later
by enrichment from the per-requisition detail endpoint.

Pagination uses OData-style ``$top`` and ``$skip`` parameters. Default page size
is 100 (ADP's default when ``$top`` is not specified). A first-page 404/410
means the client ID stopped resolving → :class:`BoardGoneError`. Any other
error returns ``[]``.

Note: This scanner handles Shape A (ADP Workforce Now with UUID ``cid``).
Shape B (recruiting.adp.com with numeric ``c=``) is a separate endpoint and
is not yet supported.
"""

from __future__ import annotations

import logging
import time

from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_platforms._registry import (
    BOARD_GONE_STATUSES,
    BoardGoneError,
    PlatformScanner,
    coerce_remote_bool,
)
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.location_parser import parse_locations

logger = logging.getLogger(__name__)

_BASE_URL = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
_CC_ID = "19000101_000001"  # Constant for public career center
_PAGE_SIZE = 100
_MAX_RESULTS = 2000
_PAGE_FETCH_SLEEP_S = 0.2
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _job_url(cid: str, external_job_id: str) -> str:
    """Build the job detail URL for a requisition."""
    return f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&ccId={_CC_ID}&job={external_job_id}"


def _extract_external_job_id(posting: dict) -> str | None:
    """Extract the ExternalJobID from customFieldGroup.stringFields."""
    custom_fields = posting.get("customFieldGroup", {})
    string_fields = custom_fields.get("stringFields", [])
    for field in string_fields:
        name_code = field.get("nameCode", {})
        if name_code.get("codeValue") == "ExternalJobID":
            return field.get("stringValue")
    return None


def _location(posting: dict) -> str:
    """Extract the primary location from requisitionLocations."""
    locations = posting.get("requisitionLocations", [])
    if not locations:
        return ""
    first = locations[0] or {}
    # Try to get a readable location string
    name = first.get("nameCode", {}).get("shortName") or first.get("nameCode", {}).get("codeValue")
    if name:
        return name
    # Fallback to itemID if no name
    return first.get("itemID", "")


def _is_remote(posting: dict) -> bool | None:
    """Extract remote indicator from customFieldGroup.indicatorFields."""
    custom_fields = posting.get("customFieldGroup", {})
    indicator_fields = custom_fields.get("indicatorFields", [])
    for field in indicator_fields:
        name_code = field.get("nameCode", {})
        # ADP may use various field names for remote - check common ones
        code = name_code.get("codeValue", "").lower()
        if "remote" in code:
            return field.get("indicatorValue")
    return coerce_remote_bool(None)


def _employment_type(posting: dict) -> str | None:
    """Extract employment type from workLevelCode or custom fields."""
    # Try workLevelCode first
    work_level = posting.get("workLevelCode", {})
    if work_level:
        short_name = work_level.get("shortName")
        if short_name:
            return short_name

    # Fallback to custom fields
    custom_fields = posting.get("customFieldGroup", {})
    code_fields = custom_fields.get("codeFields", [])
    for field in code_fields:
        name_code = field.get("nameCode", {})
        if name_code.get("codeValue") == "SalaryType":
            return field.get("shortName")
    return None


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """Fetch one ADP Workforce Now client, offset-paginated.

    Raises :class:`BoardGoneError` when the FIRST page returns 404/410 (the
    client ID stopped resolving). Returns ``[]`` on any other error so one dead
    tenant cannot crash a multi-company scan.

    Args:
        slug: ADP Workforce Now client ID.
        max_pages: Kept for signature compatibility; not applicable — ADP
            paginates by a fixed ``_MAX_RESULTS`` offset cap instead.
    """
    cid = slug.strip()
    if not cid:
        return []

    out: list[dict] = []
    skip = 0

    while skip < _MAX_RESULTS:
        if skip > 0:
            time.sleep(_PAGE_FETCH_SLEEP_S)

        params = {
            "cid": cid,
            "ccId": _CC_ID,
            "lang": "en_US",
            "locale": "en_US",
            "$top": _PAGE_SIZE,
            "$skip": skip,
        }

        try:
            resp = get_session().get(
                _BASE_URL, params=params, headers=_HEADERS, timeout=_PROBE_TIMEOUT
            )
        except Exception as exc:
            logger.warning("scan_adp('%s') request failed: %s", slug, exc)
            break

        if skip == 0 and resp.status_code in BOARD_GONE_STATUSES:
            raise BoardGoneError(resp.status_code, slug)
        if resp.status_code != 200:
            logger.debug("scan_adp('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            payload = resp.json()
        except Exception as exc:
            logger.warning("scan_adp('%s') JSON parse error: %s", slug, exc)
            break

        reqs = payload.get("jobRequisitions", [])
        if not reqs:
            break
        out.extend(reqs)

        skip += _PAGE_SIZE
        if len(reqs) < _PAGE_SIZE:
            break

    return out


def _posting_to_job(posting: dict, slug: str) -> dict | None:
    item_id = posting.get("itemID")
    if not item_id:
        return None

    external_job_id = _extract_external_job_id(posting)
    if not external_job_id:
        # Fallback to itemID if ExternalJobID is missing
        external_job_id = str(item_id)

    location = _location(posting)
    post_date = posting.get("postDate")

    return {
        "title": posting.get("requisitionTitle", ""),
        "company_source": "ADP",
        "location": location,
        "locations_structured": parse_locations(location),
        # List endpoint only carries basic info; jd_full is filled by
        # enrichment from the per-requisition detail endpoint.
        "description": "",
        "source_url": _job_url(slug, external_job_id),
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "source_id": str(item_id),
        "posted_date": post_date[:10] if post_date else None,
        "is_remote": _is_remote(posting),
        "employment_type": _employment_type(posting),
        "department": None,  # Not exposed in list endpoint
    }


SCANNER = PlatformScanner(
    name="adp",
    company_source="ADP",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("requisitionTitle", ""),
    posting_to_job=_posting_to_job,
)

"""IBM careers API platform scanner.

IBM exposes a bespoke, unauthenticated JSON search API::

    POST https://www-api.ibm.com/search/api/v2
    Content-Type: application/json
    {"appId": "careers", "scopes": ["careers2"], "query": {"bool": {"must": []}},
     "size": 50, "from": 0, "_source": [...]}

Response shape: Elasticsearch-style with ``{"hits": {"hits": [{"_source": {...}}]}}``.
Each hit's ``_source`` carries ``field_text_01`` (jobId), ``field_keyword_05``
(country), ``field_keyword_08`` (job category), ``field_keyword_19`` (city), and
``title``. The list endpoint omits the full job description; ``jd_full``
is filled later by enrichment.

IBM is single-tenant — the slug is a constant (``"ibm"``). Offset pagination
up to ``_MAX_RESULTS``. A first-page 404/410 means the API stopped resolving
→ :class:`BoardGoneError`. Any other error returns ``[]``.
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

_API_URL = "https://www-api.ibm.com/search/api/v2"
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


def _search_body(page: int) -> dict:
    """Build the search API payload for a given page (offset-based)."""
    return {
        "appId": "careers",
        "scopes": ["careers2"],
        "query": {"bool": {"must": []}},
        "size": _PAGE_SIZE,
        "from": page * _PAGE_SIZE,
        "_source": [
            "field_text_01",
            "title",
            "field_keyword_05",
            "field_keyword_08",
            "field_keyword_19",
        ],
    }


def _job_url(job_id: str) -> str:
    return f"https://careers.ibm.com/careers/JobDetail?jobId={job_id}"


def _location(posting: dict) -> str:
    """Build location string from city + country fields."""
    city = posting.get("field_keyword_19") or ""
    country = posting.get("field_keyword_05") or ""
    if city and country:
        return f"{city}, {country}"
    return city or country


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """Fetch IBM careers API postings, offset-paginated.

    Slug is ignored (IBM is single-tenant). Raises :class:`BoardGoneError`
    when the FIRST page returns 404/410 (the API stopped resolving). Returns
    ``[]`` on any other error so one dead tenant cannot crash a multi-company scan.

    Args:
        slug: Ignored — IBM is single-tenant.
        max_pages: Kept for signature compatibility; not applicable — IBM
            paginates by a fixed ``_MAX_RESULTS`` offset cap instead.
    """
    out: list[dict] = []
    page = 0

    while page * _PAGE_SIZE < _MAX_RESULTS:
        if page > 0:
            time.sleep(_PAGE_FETCH_SLEEP_S)

        try:
            resp = get_session().post(
                _API_URL, json=_search_body(page), headers=_HEADERS, timeout=_PROBE_TIMEOUT
            )
        except Exception as exc:
            logger.warning("scan_ibm('%s') request failed: %s", slug, exc)
            break

        if page == 0 and resp.status_code in BOARD_GONE_STATUSES:
            raise BoardGoneError(resp.status_code, slug)
        if resp.status_code != 200:
            if resp.status_code in _auth_block_statuses():
                logger.warning(
                    "scan_ibm('%s') possible auth/anti-bot wall: HTTP %d",
                    slug,
                    resp.status_code,
                )
            else:
                logger.debug("scan_ibm('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            payload = resp.json()
        except Exception as exc:
            logger.warning("scan_ibm('%s') JSON parse error: %s", slug, exc)
            break

        hits = payload.get("hits", {}).get("hits", [])
        if not hits:
            break
        # Extract _source from each hit
        for hit in hits:
            source = hit.get("_source")
            if source:
                out.append(source)

        if len(hits) < _PAGE_SIZE:
            break
        page += 1

    return out


def _posting_to_job(posting: dict, slug: str) -> dict | None:
    job_id = posting.get("field_text_01")
    if not job_id:
        return None
    # field_text_01 is an integer in the API response; coerce to str
    job_id = str(job_id)

    location = _location(posting)

    return {
        "title": posting.get("title", ""),
        "company_source": "IBM",
        "location": location,
        "locations_structured": parse_locations(location),
        # List endpoint does not expose description; jd_full is filled by enrichment.
        "description": "",
        "source_url": _job_url(job_id),
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "source_id": job_id,
        "posted_date": None,  # Not exposed in the list response
        "is_remote": None,  # Not exposed in the list response
        "employment_type": None,  # Not exposed in the list response
        "department": posting.get("field_keyword_08") or None,  # Job category
    }


SCANNER = PlatformScanner(
    name="ibm",
    company_source="IBM",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)

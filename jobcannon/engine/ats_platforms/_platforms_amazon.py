"""Amazon Jobs platform scanner (registry form).

Amazon exposes one GLOBAL public board at ``www.amazon.jobs/en/search.json`` —
no auth, GET, offset pagination. Unlike every per-company ATS, there is no
per-slug board: the registry ``slug`` is used as the ``base_query`` keyword
(empty → most-recent across all of Amazon). Sorted ``recent`` and capped at
:data:`_MAX_RESULTS`, then the driver's title gate filters to target roles.

No completeness / no :class:`BoardGoneError`: a global board never 404s for a
single company, and the only truncation risk is Amazon's 10,000-hit ceiling
(mitigated by the cap + a keyword slug). Returns ``[]`` on any error.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _auth_block_statuses,
    label_or_str,
)
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.description_formatter import html_to_plain_text
from jobcannon.engine.location_parser import parse_locations

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
_BASE = "https://www.amazon.jobs"
_PAGE_SIZE = 100
_MAX_RESULTS = 2000
_PAGE_FETCH_SLEEP_S = 0.2  # www.amazon.jobs is WAF-fronted — pace gently.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_WS_RUN = re.compile(r"\s+")
# Amazon renders posted_date human-formatted; full ("June") and abbreviated
# ("Jun") month names both appear across locales.
_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y")


def _posted_date(value: object) -> str | None:
    """Human-formatted ``"June 22, 2026"`` → ISO ``YYYY-MM-DD`` (else None)."""
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = _WS_RUN.sub(" ", value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _is_remote(posting: dict) -> bool | None:
    """Tri-state remote flag from the posting's normalized location text."""
    text = (posting.get("normalized_location") or posting.get("location") or "").lower()
    if "virtual" in text or "remote" in text:
        return True
    return None


def _fetch_one_query(base_query: str, slug: str) -> list[dict]:
    """GET + paginate ``search.json`` (recent-sorted) for ONE keyword, capped at
    _MAX_RESULTS. Returns ``[]`` on any error — a single global board, so no
    BoardGoneError. ``slug`` is only used for log attribution.
    """
    offset = 0
    out: list[dict] = []

    while offset < _MAX_RESULTS:
        if offset > 0:
            time.sleep(_PAGE_FETCH_SLEEP_S)

        params = {
            "result_limit": _PAGE_SIZE,
            "offset": offset,
            "sort": "recent",
        }
        if base_query:
            params["base_query"] = base_query

        try:
            resp = get_session().get(
                _SEARCH_URL, params=params, headers=_HEADERS, timeout=_PROBE_TIMEOUT
            )
        except Exception as exc:
            logger.warning("scan_amazon('%s') request failed: %s", slug, exc)
            break

        if resp.status_code != 200:
            if resp.status_code in _auth_block_statuses():
                logger.warning(
                    "scan_amazon('%s') possible auth/anti-bot wall: HTTP %d",
                    slug,
                    resp.status_code,
                )
            else:
                logger.debug("scan_amazon('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            payload = resp.json()
        except Exception as exc:
            logger.warning("scan_amazon('%s') JSON parse error: %s", slug, exc)
            break

        if payload.get("error"):
            logger.debug("scan_amazon('%s') API error: %s", slug, payload.get("error"))
            break

        jobs = payload.get("jobs") or []
        if not jobs:
            break

        out.extend(jobs)
        if len(jobs) < _PAGE_SIZE:
            break  # last page
        offset += _PAGE_SIZE

    return out


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """Fetch Amazon's global board for the registry slug.

    The board is keyword-bounded (``base_query``) and capped at
    :data:`_MAX_RESULTS` recent results. A single BROAD keyword (e.g. ``data``)
    hits Amazon's 10k ceiling, and the cap then drowns genuine matches behind
    unrelated ``data*`` noise (data-center, database, metadata) — a real
    coverage gap observed live. So the slug may be a ``|``-delimited set of
    FOCUSED queries (e.g. ``data scientist|data analyst|business intelligence``);
    each is paged independently with its OWN cap, and the union is returned,
    de-duplicated by ``id_icims``. A plain slug (no ``|``) is a single query,
    unchanged. The driver's title gate still does the final filtering.

    Args:
        slug: Amazon base-query keyword, or a ``|``-delimited set of them.
        max_pages: Kept for signature compatibility; not applicable —
            Amazon paginates by a fixed ``_MAX_RESULTS`` offset cap instead.
    """
    queries = [q.strip() for q in (slug or "").split("|") if q.strip()] or [""]
    seen: set[str] = set()
    out: list[dict] = []
    for base_query in queries:
        for posting in _fetch_one_query(base_query, slug):
            sid = posting.get("id_icims")
            key = str(sid) if sid is not None else None
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            out.append(posting)
    return out


def _posting_to_job(posting: dict, slug: str) -> dict:
    location = posting.get("normalized_location") or posting.get("location") or ""

    job_path = posting.get("job_path") or ""
    source_url = f"{_BASE}{job_path}" if job_path else ""

    icims_id = posting.get("id_icims")
    source_id = str(icims_id) if icims_id is not None else None

    return {
        "title": posting.get("title", ""),
        "company_source": "Amazon",
        "location": location,
        "locations_structured": parse_locations(location),
        "description": html_to_plain_text(posting.get("description", "") or ""),
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "source_id": source_id,
        "posted_date": _posted_date(posting.get("posted_date")),
        "is_remote": _is_remote(posting),
        "employment_type": label_or_str(posting.get("job_schedule_type")),
        "department": label_or_str(posting.get("job_category")),
    }


SCANNER = PlatformScanner(
    name="amazon",
    company_source="Amazon",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)

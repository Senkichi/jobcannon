"""Microsoft Careers platform scanner (registry form).

Microsoft's public careers search is a Phenom-backed JSON API at
``apply.careers.microsoft.com/api/pcsx/search`` — no auth, GET, offset
pagination at a hard-fixed page size of 10. The list endpoint omits the JD
body; enrichment fills ``jd_full`` later from the per-position detail endpoint
(``/api/pcsx/position_details``), mirroring SmartRecruiters' deferred-description
pattern.

Single tenant: the board is ``domain=microsoft.com``. The registry slug is the
``domain`` parameter — ``"microsoft.com"`` (a bare ``"microsoft"`` slug falls
back to it). Do NOT target the dead ``gcsservices.careers.microsoft.com`` host.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_platforms._registry import (
    BOARD_GONE_STATUSES,
    BoardGoneError,
    PlatformScanner,
    _auth_block_statuses,
    label_or_str,
)
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.location_parser import parse_locations

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://apply.careers.microsoft.com/api/pcsx/search"
_BASE = "https://apply.careers.microsoft.com"
# Phenom hard-fixes the page at 10 — num/size/limit are ignored.
_PAGE_SIZE = 10
_MAX_RESULTS = 2000
_PAGE_FETCH_SLEEP_S = 0.1
# Some careers CDNs 403 a bare python-requests UA.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": _BASE,
}


def _domain_for(slug: str) -> str:
    """Resolve the ``domain`` query param from the registry slug.

    A slug that looks like a domain (``microsoft.com``) is used verbatim; a bare
    ``"microsoft"`` (or empty) falls back to the canonical tenant domain.
    """
    return slug if (slug and "." in slug) else "microsoft.com"


def _remote_from_option(option: object) -> bool | None:
    """Map Phenom ``workLocationOption`` to a tri-state remote bool.

    ``coerce_remote_bool`` is wrong here — it would coerce the *string*
    ``"onsite"`` to ``True``. Only an explicit "remote" signal yields ``True``,
    an explicit "onsite" yields ``False``, everything else stays unknown.
    """
    if not isinstance(option, str):
        return None
    low = option.lower()
    if "remote" in low:
        return True
    if "onsite" in low or "on-site" in low or "in office" in low:
        return False
    return None


def _posted_date_from_ts(ts: object) -> str | None:
    """Epoch-seconds → naive-UTC ``YYYY-MM-DD`` (``postedTs`` = first posted)."""
    if not isinstance(ts, (int, float, str)):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _fetch_postings_with_completeness(
    slug: str, max_pages: int | None = None
) -> tuple[list[dict], bool]:
    """GET + paginate ``/api/pcsx/search``, tracking completeness.

    Returns ``(postings, complete)``. ``complete`` is ``True`` only when the
    board was fully paged (or is a live empty board). A first-page 404/410 with
    nothing collected raises :class:`BoardGoneError` so the scan path can demote
    a stale hit; a mid-pagination error returns the partial set as incomplete.

    The completeness signal is forward-wiring for the reconciler chain: nothing currently calls this through the
    ``PlatformScanner.fetch_postings_with_completeness`` registry field, and
    Microsoft is not yet one of ``ats_reconciler``'s reconcilable platforms.

    Args:
        slug: Microsoft slug.
        max_pages: Optional page budget (not used by Microsoft, which
            uses a _MAX_RESULTS cap instead). Kept for signature compatibility.
    """
    domain = _domain_for(slug)
    offset = 0
    out: list[dict] = []
    total_fetched = 0
    saw_total = False
    total_found = 0

    while offset < _MAX_RESULTS:
        if offset > 0:
            time.sleep(_PAGE_FETCH_SLEEP_S)

        try:
            resp = get_session().get(
                _SEARCH_URL,
                params={"domain": domain, "start": offset},
                headers=_HEADERS,
                timeout=_PROBE_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("scan_microsoft('%s') request failed: %s", slug, exc)
            break

        if resp.status_code != 200:
            if resp.status_code in BOARD_GONE_STATUSES and total_fetched == 0:
                raise BoardGoneError(resp.status_code, slug)
            if resp.status_code in _auth_block_statuses():
                logger.warning(
                    "scan_microsoft('%s') possible auth/anti-bot wall: HTTP %d",
                    slug,
                    resp.status_code,
                )
            else:
                logger.debug("scan_microsoft('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            payload = resp.json()
        except Exception as exc:
            logger.warning("scan_microsoft('%s') JSON parse error: %s", slug, exc)
            break

        data = payload.get("data") or {}
        # The API always returns an ``error`` object, empty on success
        # (``{"message": "", "body": ""}``) — only a non-empty message is a real
        # error. Checking the dict's truthiness would abort every successful page.
        err = payload.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        if err:
            logger.debug("scan_microsoft('%s') API error: %s", slug, err)
            break

        total_found = data.get("count", 0)
        saw_total = True

        postings = data.get("positions") or []
        if not postings:
            break

        out.extend(postings)
        total_fetched += len(postings)
        offset += _PAGE_SIZE

        if total_fetched >= total_found:
            break

    complete = saw_total and total_fetched >= total_found
    return out, complete


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """Completeness-flag-discarding wrapper used by the standard scan flow.

    Args:
        slug: Microsoft slug.
        max_pages: Optional page budget (not used by Microsoft, which
            uses a _MAX_RESULTS cap instead). Kept for signature compatibility.
    """
    return _fetch_postings_with_completeness(slug, max_pages)[0]


def _posting_to_job(posting: dict, slug: str) -> dict:
    locations = posting.get("locations") or []
    location = locations[0] if locations and isinstance(locations[0], str) else ""

    position_url = posting.get("positionUrl") or ""
    source_url = f"{_BASE}{position_url}" if position_url else ""

    posting_id = posting.get("id")
    source_id = str(posting_id) if posting_id is not None else None

    return {
        "title": posting.get("name", ""),
        "company_source": "Microsoft Careers",
        "location": location,
        "locations_structured": parse_locations(locations or location),
        "description": "",  # list endpoint omits the body; enrichment fills jd_full
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "source_id": source_id,
        "posted_date": _posted_date_from_ts(posting.get("postedTs")),
        "is_remote": _remote_from_option(posting.get("workLocationOption")),
        "employment_type": None,  # detail-endpoint only
        "department": label_or_str(posting.get("department")),
    }


SCANNER = PlatformScanner(
    name="microsoft",
    company_source="Microsoft Careers",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("name", ""),
    posting_to_job=_posting_to_job,
    fetch_postings_with_completeness=_fetch_postings_with_completeness,
)

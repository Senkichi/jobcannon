"""Eightfold (SmartApply) platform scanner (registry form).

Eightfold's public apply API is ``{host}/api/apply/v2/jobs`` — no auth, GET,
offset pagination at a hard-fixed page size of 10. One adapter serves every
Eightfold tenant (Netflix uses the vanity host ``explore.jobs.netflix.net``;
others use ``{tenant}.eightfold.ai``).

Slug encoding: ``"host|domain"`` (e.g. ``"explore.jobs.netflix.net|netflix.com"``)
— a tenant needs BOTH the API host and the ``domain`` query param (the API
returns empty without ``domain``). A bare slug with no ``|`` is treated as an
``eightfold.ai`` tenant.

``posted_date`` is deliberately ``None``: Eightfold resets ``t_create`` on
repost, so any date it exposes is unreliable, and a wrong date is worse than no
date (D-08).
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
    label_or_str,
)
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT
from jobcannon.engine.location_parser import parse_locations

logger = logging.getLogger(__name__)

_PAGE_SIZE = 10
_MAX_RESULTS = 2000
_PAGE_FETCH_SLEEP_S = 0.1
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _parse_slug(slug: str) -> tuple[str, str]:
    """Split a ``"host|domain"`` slug into ``(host, domain)``.

    A bare slug (no ``|``) is assumed to be an ``eightfold.ai`` tenant; the
    tenant name doubles as the ``domain`` param in that fallback.
    """
    if "|" in slug:
        host, domain = slug.split("|", 1)
        return host.strip(), domain.strip()
    return f"{slug}.eightfold.ai", slug


def _remote_from_text(text: object) -> bool | None:
    """Tri-state remote flag derived from the posting's location text.

    Eightfold's ``work_location_option`` is unreliable (a ``"USA - Remote"``
    role returned ``"onsite"``), so we read the location string instead. Only a
    "remote" signal yields ``True``; everything else stays unknown.
    """
    if not isinstance(text, str):
        return None
    return True if "remote" in text.lower() else None


def _fetch_postings_with_completeness(
    slug: str, max_pages: int | None = None
) -> tuple[list[dict], bool]:
    """GET + paginate ``/api/apply/v2/jobs``, tracking completeness.

    Returns ``(postings, complete)``. A first-page 404/410 with nothing
    collected raises :class:`BoardGoneError`; a mid-pagination error returns the
    partial set as incomplete.

    The completeness signal is forward-wiring for the reconciler chain
    (issues #1030-1033): nothing currently calls this through the
    ``PlatformScanner.fetch_postings_with_completeness`` registry field, and
    Eightfold is not yet one of ``ats_reconciler``'s reconcilable platforms.

    Args:
        slug: Eightfold slug.
        max_pages: Optional page budget (not used by Eightfold, which
            uses a _MAX_RESULTS cap instead). Kept for signature compatibility.
    """
    host, domain = _parse_slug(slug)
    url = f"https://{host}/api/apply/v2/jobs"
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
                url,
                params={"domain": domain, "start": offset},
                headers=_HEADERS,
                timeout=_PROBE_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("scan_eightfold('%s') request failed: %s", slug, exc)
            break

        if resp.status_code != 200:
            if resp.status_code in BOARD_GONE_STATUSES and total_fetched == 0:
                raise BoardGoneError(resp.status_code, slug)
            if resp.status_code in _auth_block_statuses():
                logger.warning(
                    "scan_eightfold('%s') possible auth/anti-bot wall: HTTP %d",
                    slug,
                    resp.status_code,
                )
            else:
                logger.debug("scan_eightfold('%s') returned HTTP %d", slug, resp.status_code)
            break

        try:
            payload = resp.json()
        except Exception as exc:
            logger.warning("scan_eightfold('%s') JSON parse error: %s", slug, exc)
            break

        total_found = payload.get("count", 0)
        saw_total = True

        postings = payload.get("positions") or []
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
        slug: Eightfold slug.
        max_pages: Optional page budget (not used by Eightfold, which
            uses a _MAX_RESULTS cap instead). Kept for signature compatibility.
    """
    return _fetch_postings_with_completeness(slug, max_pages)[0]


def _posting_to_job(posting: dict, slug: str) -> dict:
    host, _domain = _parse_slug(slug)
    location = posting.get("location") or ""

    posting_id = posting.get("id")
    source_id = str(posting_id) if posting_id is not None else None

    source_url = posting.get("canonicalPositionUrl") or (
        f"https://{host}/careers/job/{posting_id}" if posting_id is not None else ""
    )

    return {
        "title": posting.get("name", ""),
        "company_source": "Eightfold",
        "location": location,
        "locations_structured": parse_locations(posting.get("locations") or location),
        "description": "",  # list endpoint omits the body; enrichment fills jd_full
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "source_id": source_id,
        "posted_date": None,  # t_create resets on repost — unreliable (D-08)
        "is_remote": _remote_from_text(location),
        "employment_type": None,
        "department": label_or_str(posting.get("department")),
    }


SCANNER = PlatformScanner(
    name="eightfold",
    company_source="Eightfold",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("name", ""),
    posting_to_job=_posting_to_job,
    fetch_postings_with_completeness=_fetch_postings_with_completeness,
)

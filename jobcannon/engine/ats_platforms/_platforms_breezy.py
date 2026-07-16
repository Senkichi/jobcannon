"""Breezy HR platform scanner (registry form).

Public JSON feed at ``https://{slug}.breezy.hr/json``. Some tenants
return a bare list; others return ``{"positions": [...]}`` or
``{"jobs": [...]}``. List endpoint omits the full description (only a
short summary); ``jd_full`` enrichment runs later from ``source_url``.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    data = _http_get_json(
        f"https://{slug}.breezy.hr/json",
        log_label="scan_breezy",
        slug=slug,
    )
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        positions = data.get("positions") or data.get("jobs") or []
        return positions if isinstance(positions, list) else []
    return []


def _posting_to_job(posting: dict, _slug: str) -> dict:
    loc = posting.get("location") or {}
    if isinstance(loc, dict):
        parts = [
            loc.get("city") or loc.get("name") or "",
            loc.get("state") or loc.get("region") or "",
            loc.get("country") or "",
        ]
        location = ", ".join(p for p in parts if isinstance(p, str) and p)
        if not location and loc.get("is_remote"):
            location = "Remote"
    else:
        location = loc if isinstance(loc, str) else ""

    return {
        "title": posting.get("name") or posting.get("title") or "",
        "company_source": "Breezy",
        "location": location,
        # List response usually has no description — enrichment fills jd_full later.
        "description": posting.get("description") or "",
        "source_url": posting.get("url") or "",
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="breezy",
    company_source="Breezy",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("name") or posting.get("title") or "",
    posting_to_job=_posting_to_job,
)

"""Recruitee platform scanner (registry form).

Recruitee public offers API: ``https://{slug}.recruitee.com/api/offers/``
returns ``{"offers": [...]}``. The list response includes the full
description; no detail-page fetch is needed.

Recruitee does not consistently expose salary in the public response;
salary fields are returned as ``None`` unless the tenant has opted in.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)
from jobcannon.engine.description_formatter import strip_html_to_text


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    data = _http_get_json(
        f"https://{slug}.recruitee.com/api/offers/",
        log_label="scan_recruitee",
        slug=slug,
    )
    if not isinstance(data, dict):
        return []
    offers = data.get("offers")
    return offers if isinstance(offers, list) else []


def _location_string(offer: dict) -> str:
    """Best-effort location string from a Recruitee offer.

    Recruitee uses either ``locations`` (list of objects with ``city`` /
    ``country_code``) or flat ``city`` / ``country_code`` fields. Falls
    back to ``location`` (free-form string) if neither is structured.
    """
    locs = offer.get("locations") or []
    if isinstance(locs, list) and locs:
        first = locs[0] if isinstance(locs[0], dict) else {}
        parts = [first.get("city") or "", first.get("country") or first.get("country_code") or ""]
        joined = ", ".join(p for p in parts if p)
        if joined:
            return joined
    parts = [offer.get("city") or "", offer.get("country") or offer.get("country_code") or ""]
    joined = ", ".join(p for p in parts if p)
    if joined:
        return joined
    return offer.get("location") or ""


def _posting_to_job(offer: dict, slug: str) -> dict:
    location = _location_string(offer)
    description_html = offer.get("description") or ""
    description = (
        strip_html_to_text(description_html) if "<" in description_html else description_html
    )
    source_url = (
        offer.get("careers_url")
        or offer.get("careers_apply_url")
        or (f"https://{slug}.recruitee.com/o/{offer.get('slug')}" if offer.get("slug") else "")
    )
    return {
        "title": offer.get("title") or offer.get("position") or "",
        "company_source": "Recruitee",
        "location": location,
        "description": description,
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="recruitee",
    company_source="Recruitee",
    fetch_postings=_fetch_postings,
    title_of=lambda offer: offer.get("title") or offer.get("position") or "",
    posting_to_job=_posting_to_job,
)

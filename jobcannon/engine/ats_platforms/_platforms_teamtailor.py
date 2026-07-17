"""Teamtailor platform scanner (registry form).

Per-tenant public unkeyed JSON:API document at
``https://{slug}.teamtailor.com/api/jobs``:
``{"data": [{"attributes": {...}, "links": {...}}, ...]}``.

The keyed organization-level API at ``api.teamtailor.com/v1/jobs`` is
orthogonal and requires X-Api-Key / X-Api-Version — we do not use it.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)
from jobcannon.engine.description_formatter import strip_html_to_text


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    payload = _http_get_json(
        f"https://{slug}.teamtailor.com/api/jobs",
        log_label="scan_teamtailor",
        slug=slug,
    )
    if not isinstance(payload, dict):
        return []
    items = payload.get("data")
    if not isinstance(items, list):
        return []
    # Filter to well-shaped items; matches the historical scan body.
    return [
        item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("attributes"), dict)
    ]


def _title_of(item: dict) -> str:
    attrs = item.get("attributes") or {}
    return attrs.get("title") or "" if isinstance(attrs, dict) else ""


def _posting_to_job(item: dict, _slug: str) -> dict:
    attrs = item.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}

    body_html = attrs.get("body") or ""
    description = strip_html_to_text(body_html) if "<" in body_html else body_html

    # Teamtailor stores location on a related "location" resource, but
    # city/country are often denormalized into job attrs as well.
    loc_parts = [attrs.get("city") or "", attrs.get("country") or ""]
    location = ", ".join(p for p in loc_parts if p)

    links = item.get("links") or {}
    source_url = ""
    if isinstance(links, dict):
        source_url = (
            links.get("careersite-job-url")
            or links.get("careersite-job-apply-url")
            or links.get("self")
            or ""
        )

    return {
        "title": attrs.get("title") or "",
        "company_source": "Teamtailor",
        "location": location,
        "description": description,
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="teamtailor",
    company_source="Teamtailor",
    fetch_postings=_fetch_postings,
    title_of=_title_of,
    posting_to_job=_posting_to_job,
)

"""Workable ATS platform scanner (registry form).

Workable exposes a public widget endpoint at
``https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true``
returning ``{"name": "...", "description": ..., "jobs": [...]}``.

Each job dict carries the canonical posting fields. Workable's docs are
not fully consistent on the public-feed shape across tenants, so this
scanner defensively pulls a small set of well-known field names with
fallbacks. Tenants without an active careers widget return ``jobs: []``;
that path is treated as a clean "no current postings" miss by the
caller's title-match gate (zero results, no error).

Salary fields are returned only for tenants that opted in to
compensation display; otherwise None.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)
from jobcannon.engine.description_formatter import strip_html_to_text


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    data = _http_get_json(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
        log_label="scan_workable",
        slug=slug,
        params={"details": "true"},
    )
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs")
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def _location_string(job: dict) -> str:
    """Workable serves either a flat `location` string or a structured
    `location: {city, country}` / top-level city/country/country_code.
    Prefer the most specific available."""
    loc = job.get("location")
    if isinstance(loc, str) and loc.strip():
        return loc.strip()
    if isinstance(loc, dict):
        parts = [
            loc.get("city") or "",
            loc.get("region") or loc.get("state") or "",
            loc.get("country") or loc.get("country_code") or "",
        ]
        joined = ", ".join(p for p in parts if isinstance(p, str) and p)
        if joined:
            return joined
    parts = [
        job.get("city") or "",
        job.get("region") or job.get("state") or "",
        job.get("country") or job.get("country_code") or "",
    ]
    return ", ".join(p for p in parts if isinstance(p, str) and p)


def _posting_to_job(job: dict, slug: str) -> dict:
    description_raw = job.get("description") or job.get("full_description") or job.get("body") or ""
    description = (
        strip_html_to_text(description_raw)
        if isinstance(description_raw, str) and "<" in description_raw
        else (description_raw if isinstance(description_raw, str) else "")
    )
    source_url = (
        job.get("application_url")
        or job.get("url")
        or (job.get("shortcode") and f"https://apply.workable.com/{slug}/j/{job.get('shortcode')}")
        or ""
    )
    return {
        "title": job.get("title") or "",
        "company_source": "Workable",
        "location": _location_string(job),
        "description": description,
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="workable",
    company_source="Workable",
    fetch_postings=_fetch_postings,
    title_of=lambda job: job.get("title") or "",
    posting_to_job=_posting_to_job,
)

"""JazzHR platform scanner (registry form).

Public JSON feed at ``https://{slug}.applytojob.com/apply/jobs/feed?json=1``.
Returns either ``{"jobs": [...]}`` or a bare list, depending on tenant
config. Description is included in the feed (sometimes as
``original_description``).
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)
from jobcannon.engine.description_formatter import strip_html_to_text


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    data = _http_get_json(
        f"https://{slug}.applytojob.com/apply/jobs/feed",
        log_label="scan_jazzhr",
        slug=slug,
        params={"json": "1"},
    )
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        jobs = data.get("jobs") or []
        return jobs if isinstance(jobs, list) else []
    return []


def _posting_to_job(job: dict, slug: str) -> dict:
    parts = [job.get("city") or "", job.get("state") or "", job.get("country") or ""]
    location = ", ".join(p for p in parts if p)

    description_raw = job.get("description") or job.get("original_description") or ""
    description = strip_html_to_text(description_raw) if "<" in description_raw else description_raw

    board_code = job.get("board_code") or job.get("id") or ""
    source_url = (
        job.get("apply_url")
        or job.get("link")
        or (f"https://{slug}.applytojob.com/apply/{board_code}" if board_code else "")
    )

    return {
        "title": job.get("title") or job.get("job_title") or "",
        "company_source": "JazzHR",
        "location": location,
        "description": description,
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="jazzhr",
    company_source="JazzHR",
    fetch_postings=_fetch_postings,
    title_of=lambda job: job.get("title") or job.get("job_title") or "",
    posting_to_job=_posting_to_job,
)

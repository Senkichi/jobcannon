"""Paylocity Recruiting public job-feed scanner (registry form).

Paylocity exposes a public job feed at
``https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}``
returning ``{"organization": "...", "jobCount": N, "jobs": [...]}``.

Each item is rich and includes:
- ``jobId`` (int)
- ``title`` (string)
- ``location`` (string)
- ``salaryRange`` (string, e.g. "$91,000 - $95,000 annually")
- ``positionType``
- ``summary`` (plain text)
- ``keyResponsibilities`` / ``requirements`` / ``benefits`` (string arrays)
- ``applyUrl`` (canonical apply link)
- ``publishedDate`` (ISO8601)

The "slug" in Paylocity's world is the tenant GUID extracted from the
careers URL pattern ``recruiting.paylocity.com/recruiting/jobs/All/{guid}/...``
or ``2000recruiting.paylocity.com/Recruiting/Jobs/All/{guid}``. Whoever
sets ``companies.ats_slug`` is responsible for storing the GUID; this
scanner just passes it through.

Salary parsing: Paylocity returns a single string field (``salaryRange``)
that may be a range, a single value, or missing. Parsing that into
``salary_min`` / ``salary_max`` is left to the enrichment pipeline's
salary extractor for consistency with how other scanners handle freeform
salary strings; we store the raw text in description as a fallback.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)


def _fetch_postings(guid: str, max_pages: int | None = None) -> list[dict]:
    """Fetch the full Paylocity job feed for one tenant.

    Args:
        guid: Paylocity tenant GUID.
        max_pages: Unused (Paylocity's feed endpoint returns every posting
            in a single response, no pagination). Accepted for signature
            compatibility with ``run_platform_scan``, which unconditionally
            calls ``scanner.fetch_postings(slug, max_pages=max_pages)``
            (see #1029).
    """
    data = _http_get_json(
        f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{guid}",
        log_label="scan_paylocity",
        slug=guid,
    )
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs")
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def _description_from_sections(job: dict) -> str:
    """Stitch Paylocity's multi-section job content into one flat string.

    Paylocity returns a structured posting with separate `summary`,
    `keyResponsibilities`, `requirements`, `preferredSkills`, and
    `benefits` fields rather than a single description blob. Concatenate
    them with section headers so downstream scoring/matching sees the
    full posting context.
    """
    parts: list[str] = []
    summary = job.get("summary")
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    for label, key in (
        ("Key Responsibilities", "keyResponsibilities"),
        ("Requirements", "requirements"),
        ("Preferred Skills", "preferredSkills"),
        ("Benefits", "benefits"),
    ):
        section = job.get(key)
        if isinstance(section, list) and section:
            bullets = "\n".join(f"- {s}" for s in section if isinstance(s, str))
            if bullets:
                parts.append(f"{label}:\n{bullets}")
        elif isinstance(section, str) and section.strip():
            parts.append(f"{label}: {section.strip()}")
    return "\n\n".join(parts)


def _posting_to_job(job: dict, guid: str) -> dict:
    salary_text = job.get("salaryRange")
    description = _description_from_sections(job)
    if isinstance(salary_text, str) and salary_text.strip():
        description = f"{description}\n\nSalary: {salary_text.strip()}".strip()

    return {
        "title": job.get("title") or "",
        "company_source": "Paylocity",
        "location": job.get("location") or "",
        "description": description,
        "source_url": job.get("applyUrl") or "",
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="paylocity",
    company_source="Paylocity",
    fetch_postings=_fetch_postings,
    title_of=lambda job: job.get("title") or "",
    posting_to_job=_posting_to_job,
)

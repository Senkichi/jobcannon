# PORTED from job_finder/parsers/greenhouse_parser.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Parse Greenhouse job alert emails into Job objects.

Greenhouse sends emails from no-reply@us.greenhouse-jobs.com. The plain-text
format lists jobs as:

    Job Title
    ( https://job-boards.greenhouse.io/{company_slug}/jobs/{job_id}?gh_src=... )

    Company Name
    Department
"""

import logging
import re
from datetime import datetime

from jobcannon.engine.models import Job
from jobcannon.engine.email_parsers._common import is_meta_email

logger = logging.getLogger(__name__)

# Greenhouse job board URL — captures company slug (group 1) and job ID (group 2)
# Excludes parentheses since URLs are wrapped in "( ... )" in plain text.
GREENHOUSE_URL_RE = re.compile(
    r"https://job-boards\.greenhouse\.io/([^\s()]+)/jobs/(\d+)",
    re.IGNORECASE,
)

# Footer markers — stop parsing here
_FOOTER_RE = re.compile(
    r"^(\u00a9|\(c\)|We.ll send your next|Sincerely|Search MyGreenhouse)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_greenhouse_alert(body: str, email_date: datetime | None = None) -> list[Job]:
    """Parse a Greenhouse job alert email body into Job objects.

    Args:
        body: Email body from Gmail API (plain text).
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects (may be empty).
    """
    if not body or not body.strip():
        return []

    if is_meta_email(body[:200]):
        logger.debug("Greenhouse parser: skipping meta-email")
        return []

    # Truncate at footer
    footer_match = _FOOTER_RE.search(body)
    search_body = body[: footer_match.start()] if footer_match else body

    jobs = []
    seen_ids: set[str] = set()

    for url_match in GREENHOUSE_URL_RE.finditer(search_body):
        company_slug = url_match.group(1)
        job_id = url_match.group(2)

        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        # Build clean URL without tracking params
        source_url = f"https://job-boards.greenhouse.io/{company_slug}/jobs/{job_id}"

        # Extract title: look at lines before the URL
        title = _extract_title_before_url(body, url_match.start())

        # Extract company: look at lines after the URL
        company = _extract_company_after_url(body, url_match.end())

        if not title:
            continue

        jobs.append(
            Job(
                title=title,
                company=company or _slug_to_company(company_slug),
                location="Unknown",
                source="greenhouse",
                source_url=source_url,
                source_id=job_id,
                posted_date=email_date,
            )
        )

    if not jobs and GREENHOUSE_URL_RE.search(body):
        logger.warning(
            "Greenhouse parser: URLs found but no jobs extracted. Email format may have changed."
        )
    elif len(body) > 500 and not jobs:
        logger.warning(
            "Greenhouse parser: non-empty body yielded 0 jobs — email format may have changed."
        )

    return jobs


def _extract_title_before_url(body: str, url_start: int) -> str | None:
    """Find the job title in lines preceding the URL."""
    # Get text before the URL, split into lines, work backwards
    before = body[:url_start]
    lines = [line.strip() for line in before.split("\n") if line.strip()]

    for line in reversed(lines):
        # Skip lines that look like URLs or parentheses
        if line.startswith("(") or line.startswith("http"):
            continue
        # Skip very short or very long lines
        if len(line) < 3 or len(line) > 150:
            continue
        # Skip common preamble text
        if any(
            phrase in line.lower()
            for phrase in (
                "here's your",
                "weekly update",
                "new roles",
                "good fit",
                "apply early",
                "stand out",
            )
        ):
            continue
        return line

    return None


def _extract_company_after_url(body: str, url_end: int) -> str | None:
    """Find the company name in lines following the URL."""
    after = body[url_end:]
    lines = [line.strip() for line in after.split("\n") if line.strip()]

    for line in lines:
        # Skip closing paren or URL remnants
        if line.startswith(")") or line.startswith("http"):
            continue
        # Skip very short or very long lines
        if len(line) < 2 or len(line) > 80:
            continue
        # Skip footer-like content
        if any(
            phrase in line.lower()
            for phrase in (
                "we'll send",
                "sincerely",
                "greenhouse",
                "search my",
                "unsubscribe",
                "\u00a9",
            )
        ):
            continue
        return line

    return None


def _slug_to_company(slug: str) -> str:
    """Convert a URL slug to a readable company name (best effort)."""
    return slug.replace("-", " ").replace("_", " ").title()

# PORTED from job_finder/parsers/ziprecruiter_parser.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Parse ZipRecruiter job alert emails into Job objects.

ZipRecruiter sends HTML emails from no-reply@ziprecruiter.com.
This is a best-effort parser -- the HTML structure may change over time.
If parsing fails for any job card, it is skipped and the error is logged.

The parser uses generic HTML heuristics (job title in <h2>/<a> tags, company
in nearby spans, apply links pointing to ziprecruiter.com/jobs/). These were
written without a captured sample, so they are unverified against a real email.

To validate and harden them, drop a sanitized real alert at
``tests/fixtures/emails/ziprecruiter.eml``; the parametrized round-trip test in
``tests/test_imap_parser_roundtrip.py`` then exercises this parser against it
(it currently skips only that one case). Until that fixture lands, a runtime
zero-yield warning fires (see ``parse_ziprecruiter_alert``) when a live email
matches nothing, so a heuristic mismatch is at least observable in the logs.

If the structure is unrecognized, an empty list is returned (graceful degradation).
"""

import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from jobcannon.engine.models import Job
from jobcannon.engine.email_parsers._common import (
    _PLACEHOLDER_STRINGS,
    parse_salary_range,
)
from jobcannon.engine.email_parsers._common import (
    is_meta_email as _is_meta_email,
)
from jobcannon.engine.email_parsers._common import (
    looks_like_salary_text as _looks_like_salary_text,
)

logger = logging.getLogger(__name__)


# Patterns used to identify ZipRecruiter job links
ZIPRECRUITER_JOB_URL_RE = re.compile(
    r"ziprecruiter\.com/(jobs|apply|job|c/[^/]+/Job)/",
    re.IGNORECASE,
)


def parse_ziprecruiter_alert(body: str, email_date: datetime | None = None) -> list[Job]:
    """Parse a ZipRecruiter job alert email body (HTML) into Job objects.

    Best-effort parsing -- if the HTML structure is unrecognized, logs a warning
    and returns an empty list rather than raising an exception.

    Args:
        body: HTML email body from Gmail API.
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects (may be empty if structure is unrecognized).
    """
    if not body or not body.strip():
        return []

    # Reject meta-emails (digest summaries, count notifications) before any parsing.
    # Check raw HTML text preamble for meta patterns.
    if _is_meta_email(body[:200]):
        logger.debug("Skipping meta-email (pollution filter)")
        return []

    try:
        soup = BeautifulSoup(body, "html.parser")
        jobs = _parse_with_link_strategy(soup, email_date)

        if not jobs:
            # Fallback: try generic job card approach
            jobs = _parse_with_card_strategy(soup, email_date)

        if not jobs and body and len(body.strip()) > 500:
            logger.warning(
                "ZipRecruiter parser: no jobs found -- HTML structure may have changed. "
                "Inspect a recent ZipRecruiter email and update ziprecruiter_parser.py."
            )

        return jobs

    except Exception as e:
        logger.warning("ZipRecruiter parser: unexpected error during parsing: %s", e)
        return []


def _parse_with_link_strategy(soup: BeautifulSoup, email_date: datetime | None) -> list[Job]:
    """Strategy 1: Find job links pointing to ziprecruiter.com/jobs/."""
    jobs = []
    seen_urls = set()

    # Find all anchor tags that link to ZipRecruiter job pages
    job_links = soup.find_all("a", href=ZIPRECRUITER_JOB_URL_RE)

    for link in job_links:
        href = link.get("href", "")
        if href in seen_urls:
            continue
        seen_urls.add(href)

        job = _extract_job_from_link(link, href, email_date)
        if job:
            jobs.append(job)

    return jobs


def _parse_with_card_strategy(soup: BeautifulSoup, email_date: datetime | None) -> list[Job]:
    """Strategy 2: Find job cards by common ZipRecruiter class/structure patterns."""
    jobs = []

    # ZipRecruiter commonly uses td or div containers with job data
    # Try to find table rows or divs that contain multiple job-like text patterns
    candidates = soup.find_all(["td", "div"], limit=200)

    seen_titles = set()
    for el in candidates:
        text = el.get_text(strip=True)
        # Skip if too short or too long to be a job card
        if len(text) < 10 or len(text) > 2000:
            continue

        # Look for elements that have a link to ZipRecruiter
        links = el.find_all("a", href=ZIPRECRUITER_JOB_URL_RE)
        if not links:
            continue

        job = _extract_job_from_container(el, links[0].get("href", ""), email_date)
        if job and job.title not in seen_titles:
            seen_titles.add(job.title)
            jobs.append(job)

    return jobs


def _extract_job_from_link(link_tag, href: str, email_date: datetime | None) -> Job | None:
    """Extract job data from a ZipRecruiter job link and its surrounding context."""
    # Try to get text directly from the link
    link_text = link_tag.get_text(strip=True)

    # Look at the parent container for richer context
    parent = link_tag.parent
    if parent:
        container_text = parent.get_text(separator="\n", strip=True)
    else:
        container_text = link_text

    title = _extract_title_from_link(link_tag, link_text)
    if not title:
        return None

    source_id = _extract_job_id(href)
    company = _extract_company(link_tag)
    location = _extract_location(link_tag)
    salary_min, salary_max = _extract_salary(container_text)

    return Job(
        title=title,
        company=company,
        location=location,
        source="ziprecruiter",
        source_url=href,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_job_from_container(container, href: str, email_date: datetime | None) -> Job | None:
    """Extract job data from a container element (fallback strategy)."""
    text = container.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    if len(lines) < 2:
        return None

    # Heuristic: first non-empty line is often the job title
    title = lines[0]
    # Skip very long "titles" (probably navigation text)
    if len(title) > 100:
        return None
    # Skip lines that look like navigation/header text
    if any(kw in title.lower() for kw in ["unsubscribe", "view in browser", "copyright"]):
        return None

    company = lines[1] if len(lines) > 1 else "Unknown"
    location = lines[2] if len(lines) > 2 else "Unknown"

    # Reject HTML template artifacts masquerading as real data. These appear when
    # email rendering engines fail to substitute actual job fields.
    if title.lower() in _PLACEHOLDER_STRINGS or len(title) < 3:
        return None
    if company.lower() in _PLACEHOLDER_STRINGS or len(company) < 2:
        return None

    salary_min, salary_max = _extract_salary(text)
    source_id = _extract_job_id(href)

    return Job(
        title=title,
        company=company,
        location=location,
        source="ziprecruiter",
        source_url=href,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_title_from_link(link_tag, link_text: str) -> str | None:
    """Try to extract the job title from a link element."""
    # If the link text is a plausible job title (not a generic label), use it
    if link_text and len(link_text) > 3 and len(link_text) < 100:
        generic_labels = {"apply", "view", "click here", "apply now", "see more", "learn more"}
        if link_text.lower() not in generic_labels:
            return link_text

    # Look for heading elements inside the link
    for tag in ["h1", "h2", "h3", "h4", "strong", "b"]:
        el = link_tag.find(tag)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) > 3:
                return text

    # Check the aria-label attribute
    aria = link_tag.get("aria-label", "")
    if aria and len(aria) > 3:
        return aria

    return None


def _extract_company(link_tag) -> str:
    """Try to extract company name from nearby elements."""
    # Look for a sibling or parent element that might contain company name
    parent = link_tag.parent
    if not parent:
        return "Unknown"

    # Try to find spans or divs near the link that aren't the title
    for el in parent.find_all(["span", "div", "p"]):
        text = el.get_text(strip=True)
        # Company names are typically 2-50 chars, not salary-like
        if 2 <= len(text) <= 50 and not _looks_like_salary_text(text):
            if text != link_tag.get_text(strip=True):  # not the same as the title
                return text

    return "Unknown"


def _extract_location(link_tag) -> str:
    """Try to extract location from nearby elements."""
    parent = link_tag.parent
    if not parent:
        return "Unknown"

    # Location often contains state abbreviations, "Remote", or city+state patterns
    location_keywords = re.compile(
        r"\b(remote|hybrid|onsite|on-site|[A-Z]{2}|[A-Z][a-z]+,\s*[A-Z]{2})\b"
    )

    for el in parent.find_all(["span", "div", "p"]):
        text = el.get_text(strip=True)
        if location_keywords.search(text) and len(text) < 100:
            return text

    return "Unknown"


def _extract_job_id(url: str) -> str:
    """Extract job ID from a ZipRecruiter URL."""
    # Patterns: /jobs/TITLE-at-COMPANY-XXXXXXXX or /job/XXXXXXXX
    match = re.search(r"[-/]([a-zA-Z0-9]{8,})(?:\?|$)", url)
    if match:
        return match.group(1)

    # Try extracting from query params
    try:
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            return path_parts[-1]
    except Exception:
        logger.debug("ziprecruiter description extraction failed", exc_info=True)

    return ""


def _extract_salary(text: str) -> tuple[int | None, int | None]:
    """Parse salary from text.

    Delegates to the shared ``parse_salary_range`` in ``_common.py``.
    """
    return parse_salary_range(text)

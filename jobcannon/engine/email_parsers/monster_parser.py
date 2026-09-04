# PORTED from job_finder/parsers/monster_parser.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Parse Monster job alert emails into Job objects.

Monster sends HTML emails from monster@notifications.monster.com.

Email structure — each job occupies a <table class="width-100"> block:
  1. Optional logo row: company logo image
  2. Title row: <a class="hdline-2" href="click.monster.com/..."> (tracking redirect)
  3. Company/location row: <td class="hdline-3 left-20"> containing
     alternating <span class="hdline-3"> elements:
       Company (dark), " - " (separator), City, " - " (separator), State
  4. CTA row: QUICK APPLY or VIEW JOB button link

All URLs are click.monster.com tracking redirects; no raw Monster job ID is
exposed in the email, so source_id is left empty.

Note: Monster email CSS class names may change over time. If parsing breaks,
inspect a recent email and update the class constants below.
"""

import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup

from jobcannon.engine.models import Job
from jobcannon.engine.email_parsers._common import is_meta_email as _is_meta_email

logger = logging.getLogger(__name__)

# All job links in Monster emails use click.monster.com tracking redirects
_MONSTER_LINK_RE = re.compile(r"click\.monster\.com")

# CSS class that identifies job title <a> tags (distinct from CTA "button" class)
_TITLE_LINK_CLASS = "hdline-2"

# CSS selector for the company/location <td> (must have BOTH classes)
_COMPANY_CELL_SELECTOR = "td.hdline-3.left-20"

# CSS class for the <span> elements inside the company/location cell
_COMPANY_SPAN_CLASS = "hdline-3"


def parse_monster_alert(body: str, email_date: datetime | None = None) -> list[Job]:
    """Parse a Monster job alert email body (HTML) into Job objects.

    Args:
        body: HTML email body from Gmail API.
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects, or [] for meta-emails or on failure.
    """
    if not body or not body.strip():
        return []

    if _is_meta_email(body[:200]):
        logger.debug("Skipping meta-email (pollution filter)")
        return []

    soup = BeautifulSoup(body, "html.parser")

    # Job title links have class="hdline-2"; CTA buttons use class="button"
    title_links = soup.find_all("a", class_=_TITLE_LINK_CLASS, href=_MONSTER_LINK_RE)

    if not title_links:
        if len(body.strip()) > 500:
            logger.warning(
                "Monster parser: no job title links found — email template may have changed. "
                "Inspect a recent Monster email and update _TITLE_LINK_CLASS in monster_parser.py."
            )
        return []

    jobs = []
    seen_hrefs: set[str] = set()

    for link in title_links:
        href = link.get("href", "")
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        job = _parse_job_card(link, email_date)
        if job:
            jobs.append(job)

    if len(body) > 500 and not jobs:
        logger.warning(
            "Monster parser: non-empty body yielded 0 jobs — email format may have changed."
        )

    return jobs


def _parse_job_card(title_link, email_date: datetime | None) -> Job | None:
    """Parse a single job from its title link anchor element."""
    title = " ".join(title_link.get_text().split())
    if not title:
        return None

    href = title_link.get("href", "")

    # Walk up to the enclosing <table class="width-100"> job card container
    card = _find_ancestor_table(title_link)
    if card is None:
        return None

    # Company/location is in <td class="hdline-3 left-20"> within the card
    company_cell = card.select_one(_COMPANY_CELL_SELECTOR)
    company, location = _parse_company_location(company_cell)

    return Job(
        title=title,
        company=company,
        location=location,
        source="monster",
        source_url=href,
        source_id="",
        posted_date=email_date,
    )


def _find_ancestor_table(tag) -> BeautifulSoup | None:
    """Walk up the DOM to find the nearest <table class="width-100"> ancestor."""
    node = tag.parent
    while node is not None:
        if node.name == "table" and "width-100" in (node.get("class") or []):
            return node
        node = node.parent
    return None


def _parse_company_location(cell) -> tuple[str, str]:
    """Extract company name and location from the company/location table cell.

    The cell contains alternating <span class="hdline-3"> elements:
        Company, " - ", City, " - ", State

    Separator spans have stripped text exactly equal to "-" and are skipped.

    Returns:
        (company, location) tuple. Returns ("Unknown", "Unknown") when absent.
    """
    if cell is None:
        return "Unknown", "Unknown"

    spans = cell.find_all("span", class_=_COMPANY_SPAN_CLASS)
    # Filter out separator spans whose stripped text is exactly "-"
    parts = [s.get_text(strip=True) for s in spans if s.get_text(strip=True) != "-"]

    if not parts:
        return "Unknown", "Unknown"

    company = parts[0]

    if len(parts) >= 3:
        city, state = parts[1], parts[2]
        location = f"{city}, {state}"
    elif len(parts) == 2:
        location = parts[1]
    else:
        location = "Unknown"

    return company, location

# PORTED from job_finder/parsers/glassdoor_parser.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Parse Glassdoor job alert emails into Job objects.

Glassdoor sends HTML emails from noreply@glassdoor.com with job cards.

Three formats are handled:
1. CSS-class format (pre-2026): span.gd-* and p.gd-* elements
2. Positional format (2026 v1): classless spans and p tags in fixed order
3. Table/span format (2026 v2): title in table/td, company/location in spans

Each card is wrapped in an <a> tag linking to glassdoor.com/partner/jobListing.htm
with a jobListingId URL parameter.

CSS-class format:
- Company name in span.gd-628b46d9ce
- Job title in p.gd-6c2846d4dc
- Location in p.gd-28d35bae2f (first occurrence per card)
- Salary in p.gd-28d35bae2f (second occurrence, format: "$XXK - $XXK (Employer est.)")

Positional format (2026 v1):
- Company name in inner <span> (sibling of rating span "X.X ★")
- Job title in first <p> tag
- Location in second <p> tag (if not salary/age)
- Salary in <p> tag containing "$"

Table/span format (2026 v2):
- Title in <table class="gd-10qqdaw..."><tr><td>
- Company in <span class="gd-forujw..."> (trailing ·-· separator stripped)
- Location in <span class="gd-56kyx5...">
- Salary in <table class="gd-1af37x6..."><tr><td>
- Each job has TWO matching <a> tags (logo link + data link); logo link has no title td

Note: Glassdoor CSS class names may change over time. If parsing breaks,
inspect a recent email and update the class constants below.
"""

import logging
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from jobcannon.engine.models import Job
from jobcannon.engine.email_parsers._common import (
    is_meta_email as _is_meta_email,
)
from jobcannon.engine.email_parsers._common import (
    looks_like_salary_range,
    parse_salary_range,
)

logger = logging.getLogger(__name__)

# CSS class names from Glassdoor email templates (pre-2026 format)
# These classes are absent in the 2026+ positional format.
COMPANY_CLASS = "gd-628b46d9ce"
TITLE_CLASS = "gd-6c2846d4dc"
DETAIL_CLASS = "gd-28d35bae2f"  # used for both location and salary

# Table/span format CSS class names (2026 v2)
_TABLE_TITLE_CLASS = "gd-10qqdaw"
_TABLE_COMPANY_CLASS = "gd-forujw"
_TABLE_LOCATION_CLASS = "gd-56kyx5"
_TABLE_SALARY_CLASS = "gd-1af37x6"

# Positional format: rating pattern (e.g. "3.6 ★" or "4.1 ★")
_RATING_RE = re.compile(r"^\s*\d+\.\d+\s*\u2605?\s*$")

# Positional format: age/recency pattern (e.g. "Just posted", "17h", "1d", "5d")
_AGE_RE = re.compile(r"^(Just posted|\d+[hd]?)$", re.IGNORECASE)

# Company-follow / review digest: Glassdoor brand-views pixel URL is unique to these
_BRAND_VIEWS_RE = re.compile(r"glassdoor\.com/brand-views")


def parse_glassdoor_alert(body: str, email_date: datetime | None = None) -> list[Job]:
    """Parse a Glassdoor job alert email body (HTML) into Job objects.

    Args:
        body: HTML email body from Gmail API.
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects, or [] for meta-email digests.
    """
    soup = BeautifulSoup(body, "html.parser")
    jobs = []

    # Each job card is wrapped in an <a> tag linking to glassdoor.com/partner/jobListing.htm
    job_links = soup.find_all("a", href=re.compile(r"glassdoor\.com/partner/jobListing"))

    # If no job cards found, identify why before returning [].
    if not job_links:
        if _BRAND_VIEWS_RE.search(body):
            logger.debug("Skipping company-follow / review digest email (no job listings)")
        elif _is_meta_email(soup.get_text()):
            logger.debug("Skipping meta-email (pollution filter)")
        elif len(body) > 500:
            logger.warning(
                "Glassdoor parser: non-empty body yielded 0 jobs — email format may have changed."
            )
        return []

    for link in job_links:
        job = _parse_job_card(link, email_date)
        if job:
            jobs.append(job)

    # Fallback drift detection: job card links existed but nothing extracted.
    # This means CSS classes have likely changed on Glassdoor's email template.
    if len(jobs) == 0 and job_links:
        logger.warning(
            "Glassdoor parser: found %d job card links but extracted 0 jobs"
            " — CSS classes may have changed. Current classes:"
            " COMPANY=%s, TITLE=%s, DETAIL=%s",
            len(job_links),
            COMPANY_CLASS,
            TITLE_CLASS,
            DETAIL_CLASS,
        )

    return jobs


def _parse_job_card(link_tag, email_date: datetime | None) -> Job | None:
    """Parse a single job card from a Glassdoor alert.

    Tries CSS-class extraction first (pre-2026 format). Falls back to
    positional extraction when CSS classes are absent (2026+ format).
    """
    href = link_tag.get("href", "")

    # Extract job listing ID from URL
    source_id = _extract_listing_id(href)

    # Find company name
    company_el = link_tag.find("span", class_=COMPANY_CLASS)
    company = company_el.get_text(strip=True) if company_el else None

    # Find job title
    title_el = link_tag.find("p", class_=TITLE_CLASS)
    title = title_el.get_text(strip=True) if title_el else None

    if not title:
        # Try table/span format (2026 v2) before positional.
        if link_tag.find("td", class_=_TABLE_TITLE_CLASS):
            return _parse_job_card_table_span(link_tag, email_date)
        # Fall back to positional extraction (2026 v1 classless format).
        return _parse_job_card_positional(link_tag, email_date)

    if not company:
        # Title CSS class hit but company CSS class missed — Glassdoor
        # randomizes class names independently per field, so this hybrid
        # state is common after each redesign. The positional extractor
        # finds the company via a span-text heuristic that survives
        # class-name churn, so prefer it over returning None (which
        # silently dropped 100% of cards for any user on a fixture where
        # COMPANY_CLASS had aged out — see test_eml_fixture_round_trips_to_jobs
        # against glassdoor_2.eml).
        return _parse_job_card_positional(link_tag, email_date)

    # Find location and salary (both use DETAIL_CLASS)
    detail_els = link_tag.find_all("p", class_=DETAIL_CLASS)
    location = "Unknown"
    salary_min = None
    salary_max = None

    for el in detail_els:
        text = el.get_text(strip=True)
        if looks_like_salary_range(text):
            salary_min, salary_max = _parse_salary(text)
        elif text and not looks_like_salary_range(text):
            # First non-salary detail is location
            if location == "Unknown":
                location = text

    # Build clean Glassdoor URL
    clean_url = f"https://www.glassdoor.com/job-listing/j?jl={source_id}" if source_id else href

    return Job(
        title=title,
        company=company,
        location=location,
        source="glassdoor",
        source_url=clean_url,
        # source_id not persisted: the Glassdoor listing ID is used only to build
        # a clean URL above; it is not a per-job-stable platform ID (I-11).
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _parse_job_card_positional(link_tag, email_date: datetime | None) -> Job | None:
    """Parse a single job card using positional extraction (2026+ classless format).

    In the new Glassdoor format, all CSS classes are absent. Company name and
    rating are in nested <span> tags; job fields are in sequential <p> tags.

    Company extraction: iterate all <span> tags in the card, collect those
    whose .string (direct text, no children) is non-empty. Filter out spans
    whose text matches the rating pattern (e.g. "3.6 ★"). Take first remaining.

    Field extraction from <p> tags:
    - First p that is neither salary nor age = title
    - Among remaining: first non-salary non-age = location
    - First p containing "$" with salary range = salary
    """
    href = link_tag.get("href", "")
    source_id = _extract_listing_id(href)

    # --- Company ---
    company = None
    for span in link_tag.find_all("span"):
        direct_text = span.string
        if direct_text is None:
            continue
        direct_text = direct_text.strip()
        if not direct_text:
            continue
        if _RATING_RE.match(direct_text):
            continue
        company = direct_text
        break

    # --- Title, location, salary from <p> tags ---
    all_ps = link_tag.find_all("p")
    title = None
    location = "Unknown"
    salary_min = None
    salary_max = None

    # Find title: first p that is not a salary and not an age marker
    title_idx = None
    for i, p in enumerate(all_ps):
        text = p.get_text(strip=True)
        if not text:
            continue
        if looks_like_salary_range(text):
            continue
        if _AGE_RE.match(text):
            continue
        title = text
        title_idx = i
        break

    if not title or not company or title_idx is None:
        return None

    # Among remaining p tags after title: classify location, salary, age
    for p in all_ps[title_idx + 1 :]:
        text = p.get_text(strip=True)
        if not text:
            continue
        if _AGE_RE.match(text):
            continue
        if looks_like_salary_range(text):
            salary_min, salary_max = _parse_salary(text)
        elif location == "Unknown":
            location = text

    # Build clean Glassdoor URL
    clean_url = f"https://www.glassdoor.com/job-listing/j?jl={source_id}" if source_id else href

    return Job(
        title=title,
        company=company,
        location=location,
        source="glassdoor",
        source_url=clean_url,
        # source_id not persisted: the Glassdoor listing ID is used only to build
        # a clean URL above; it is not a per-job-stable platform ID (I-11).
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _parse_job_card_table_span(link_tag, email_date: datetime | None) -> Job | None:
    """Parse a single job card using table/span extraction (2026 v2 format).

    In this format each job has two matching <a> tags: a logo link (img only)
    and a data link. The logo link has no title td and returns None. The data
    link structure:
    - Title: <table class="gd-10qqdaw..."><tr><td>title</td></tr></table>
    - Company: <span class="gd-forujw...">Company·-·</span> (separator stripped)
    - Location: <span class="gd-56kyx5...">Location</span>
    - Salary: <table class="gd-1af37x6..."><tr><td>salary</td></tr></table>
    """
    href = link_tag.get("href", "")
    source_id = _extract_listing_id(href)

    title_td = link_tag.find("td", class_=_TABLE_TITLE_CLASS)
    if not title_td:
        return None
    title = title_td.get_text(strip=True)
    if not title:
        return None

    company_span = link_tag.find("span", class_=_TABLE_COMPANY_CLASS)
    if not company_span:
        return None
    raw = company_span.get_text(strip=True)
    company = re.sub(r"[·–—\-\s]+$", "", raw).strip()
    if not company:
        return None

    location_span = link_tag.find("span", class_=_TABLE_LOCATION_CLASS)
    location = location_span.get_text(strip=True) if location_span else "Unknown"

    salary_min = None
    salary_max = None
    salary_td = link_tag.find("td", class_=_TABLE_SALARY_CLASS)
    if salary_td:
        salary_text = salary_td.get_text(strip=True)
        if looks_like_salary_range(salary_text):
            salary_min, salary_max = _parse_salary(salary_text)

    clean_url = f"https://www.glassdoor.com/job-listing/j?jl={source_id}" if source_id else href
    return Job(
        title=title,
        company=company,
        location=location,
        source="glassdoor",
        source_url=clean_url,
        # source_id not persisted: the Glassdoor listing ID is used only to build
        # a clean URL above; it is not a per-job-stable platform ID (I-11).
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_listing_id(url: str) -> str:
    """Extract jobListingId from Glassdoor URL params."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        ids = params.get("jobListingId", [])
        # Also check if it's embedded in the path via &amp; encoding
        if not ids:
            match = re.search(r"jobListingId=(\d+)", url)
            if match:
                return match.group(1)
        return ids[0] if ids else ""
    except Exception:
        logger.debug("glassdoor description extraction failed", exc_info=True)
        return ""


def _parse_salary(text: str) -> tuple[int | None, int | None]:
    """Parse salary from Glassdoor format: '$178K - $250K (Employer est.)'

    Delegates to the shared ``parse_salary_range`` in ``_common.py``.
    """
    return parse_salary_range(text)

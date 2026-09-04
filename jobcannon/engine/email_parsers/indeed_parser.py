# PORTED from job_finder/parsers/indeed_parser.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Parse Indeed job alert emails into Job objects.

Handles two distinct Indeed email types:

1. **Alert emails** (alert@indeed.com): plain-text format with
   engage.indeed.com tracking redirect URLs. Falls back to HTML strategies.
   Entry point: ``parse_indeed_alert``

2. **Match emails** (donotreply@match.indeed.com): "smart match"
   recommendations in plain-text. Two sub-formats:
   - Single-job: intro sentence with cts.indeed.com URL
   - Multi-job: job blocks delimited by indeed.com/pagead/clk/dl URLs
   Entry point: ``parse_indeed_match_alert``

Both share the same block-parsing logic (_parse_plaintext_job_block) and
salary extraction (_extract_salary_from_text).
"""

import logging
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from jobcannon.engine.models import Job
from jobcannon.engine.email_parsers._common import is_meta_email, looks_like_salary_text

logger = logging.getLogger(__name__)

# Matches Indeed job page URLs (not just the domain) — used by HTML fallback
INDEED_JOB_URL_RE = re.compile(
    r"indeed\.com/(viewjob|rc/clk|job/)",
    re.IGNORECASE,
)

# Matches engage.indeed.com tracking redirect URLs (plain-text format)
INDEED_ENGAGE_URL_RE = re.compile(
    r"https://engage\.indeed\.com/f/a/\S+",
    re.IGNORECASE,
)

# Matches indeed.com/rc/clk/dl tracking redirect URLs (new plain-text format, 2026+)
INDEED_RC_CLK_URL_RE = re.compile(
    r"https://www\.indeed\.com/rc/clk/dl\?\S+",
    re.IGNORECASE,
)

# Hourly rate: "$25/hr" or "$25.50 / hour" (Indeed-specific fallback)
HOURLY_RE = re.compile(r"\$(\d[\d.]+)\s*(?:\/\s*(?:hr|hour))", re.IGNORECASE)

# Hourly single rate: "$95 an hour" / "$95 per hour". Single amounts are out of
# scope for the shared range parser (salary_normalizer.parse_salary_text handles
# ranges only), so they are detected here and routed through the normalizer as a
# single-value hourly observation (the \u00d72080 annualization is the normalizer's, D-2).
HOURLY_SINGLE_RE = re.compile(
    r"\$(\d[\d,.]+)\s*(?:an?\s+hour|per\s+hour)",
    re.IGNORECASE,
)

# Match email: indeed.com/pagead/clk/dl tracking URLs (multi-job format)
INDEED_MATCH_URL_RE = re.compile(
    r"https://www\.indeed\.com/pagead/clk/dl\?\S+",
    re.IGNORECASE,
)

# Match email: cts.indeed.com tracking URLs (single-job format)
INDEED_CTS_URL_RE = re.compile(
    r"https://cts\.indeed\.com/v3/\S+",
    re.IGNORECASE,
)

# Match email: single-job intro sentence extraction (legacy format)
_SINGLE_MATCH_INTRO_RE = re.compile(
    r"job for an?\s+(.+?)\s+at\s+(.+?)\s+in\s+(.+?)\s+paying\s+(.+?)\s+would",
    re.IGNORECASE | re.DOTALL,
)

# Match email: single-job intro without salary (legacy format)
_SINGLE_MATCH_INTRO_NO_PAY_RE = re.compile(
    r"job for an?\s+(.+?)\s+at\s+(.+?)\s+in\s+(.+?)\s+would",
    re.IGNORECASE | re.DOTALL,
)

# Match email (2026-04+ format): "View job: <url>" anchor line.
# Newer single-job format puts the job URL on its own "View job:" line below
# a structured block of title/company/location/Salary/Job type.
_SINGLE_MATCH_VIEW_JOB_RE = re.compile(
    r"^View job:\s*(https://cts\.indeed\.com/\S+)",
    re.IGNORECASE | re.MULTILINE,
)

# Structured-block field labels (skip when extracting title/company/location)
_SINGLE_MATCH_FIELD_LABELS = ("salary:", "job type:", "work setting:")

# Intro-prose detector: multi-sentence text contains ". <Capital>" or "! <Capital>".
# Structured-block lines (titles, companies, locations) are single-phrase and
# never multi-sentence.
_MULTI_SENTENCE_RE = re.compile(r"[.!]\s+[A-Z]")

# Match email: footer markers (superset of alert footer)
_MATCH_FOOTER_RE = re.compile(
    r"^(\u00a9|\(c\)|Indeed Tower|Salaries estimated)",
    re.IGNORECASE | re.MULTILINE,
)

# Match email: preamble end marker — line containing the footnote superscript
# "Jobs are based on your preferences, profile, and activity on Indeed ¹"
_MATCH_PREAMBLE_END_RE = re.compile(
    r"^Jobs are based on.*Indeed\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Indeed-specific meta-email patterns (checked in addition to BASE_META_PATTERNS).
# IMPORTANT: Do NOT filter on "new jobs" or "N new jobs" — those ARE real alerts.
# Only filter administrative / digest emails with NO individual job cards.
# Note: "job alert digest|weekly digest" is already in BASE_META_PATTERNS.
_INDEED_META_PATTERNS = [
    re.compile(r"confirm.*email.*(?:address|subscription)", re.IGNORECASE),
    re.compile(r"unsubscribe from.*alerts?$", re.IGNORECASE | re.MULTILINE),
]

# Generic link texts that are NOT job titles
_GENERIC_LINK_TEXTS = frozenset(
    {
        "apply",
        "view",
        "click here",
        "apply now",
        "see more",
        "learn more",
        "view job",
        "apply for job",
        "see all jobs",
        "view all jobs",
    }
)

# Plain-text: header line separating preamble from job listings
# e.g. "10+ new analytics manager jobs in San Francisco Bay Area, CA"
# e.g. "1 new lead data analyst job in San Francisco Bay Area, CA"
_PLAINTEXT_HEADER_RE = re.compile(
    r"^\d+\+?\s+new\s+.+\s+jobs?\s+in\s+",
    re.IGNORECASE | re.MULTILINE,
)

# Plain-text: noise lines to skip when extracting job fields
_AGE_LINE_RE = re.compile(
    r"^(Just posted|\d+\+?\s+days?\s+ago)$",
    re.IGNORECASE,
)

# Plain-text: summary count lines (e.g. "Jobs 1-2 of 2 new jobs") that appear
# in some email formats between the header and the actual job listings.
_SUMMARY_COUNT_RE = re.compile(
    r"^Jobs\s+\d",
    re.IGNORECASE,
)

# Plain-text: preamble navigation lines (e.g. "See matching results on Indeed: URL")
_SEE_MATCHING_RE = re.compile(
    r"^See matching results",
    re.IGNORECASE,
)

# Plain-text: footer start marker — stop processing job blocks here
_FOOTER_RE = re.compile(
    r"^(\u00a9|\(c\)|Indeed Tower)",
    re.IGNORECASE | re.MULTILINE,
)

# Location pattern: state abbreviations, "Remote", "Hybrid", city+state patterns
_LOCATION_RE = re.compile(
    r"\b(remote|hybrid|onsite|on-site|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s*[A-Z]{2}|[A-Z]{2}\s*\d{5})\b"
)


def _looks_like_html(body: str) -> bool:
    """Return True if the body contains HTML tags (indicating HTML format)."""
    return bool(re.search(r"<(?:a|table|div|tr|td|span|html|body)\b", body, re.IGNORECASE))


def _extract_job_id_from_engage_url(url: str) -> str:
    """Extract the encoded job ID from an engage.indeed.com/f/a/... URL.

    Returns the path segment immediately after /f/a/ as the source_id.
    For  https://engage.indeed.com/f/a/JOB1_ENCODED_URL_STRING  this returns
    JOB1_ENCODED_URL_STRING.
    """
    try:
        parsed = urlparse(url)
        # Path looks like /f/a/ENCODED_SEGMENT
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "f" and parts[1] == "a":
            return parts[2]
        if parts:
            return parts[-1]
    except Exception:
        logger.debug("indeed field extraction failed", exc_info=True)
    return ""


def _parse_plaintext(body: str, email_date: datetime | None) -> list[Job]:
    """Parse a plain-text Indeed alert email body into Job objects.

    Algorithm:
    1. Find the job count header line ("N+ new X jobs in Y") to locate where
       job listings start. Discard everything before it (preamble).
    2. Stop at the footer marker (copyright line).
    3. Find all engage.indeed.com URLs in the job section. Each URL is the
       trailing link of a job block.
    4. For each URL, the job block is the text between the previous URL end
       and the start of this URL.
    5. Parse each block: title (line 0), company-location (line 1), salary
       (any line with $), ignoring noise lines (age, "Easily apply").
    """
    # Find header line; use its end as the start of job content
    header_match = _PLAINTEXT_HEADER_RE.search(body)
    if header_match:
        # Start parsing from end of header line
        header_line_end = body.index("\n", header_match.start())
        job_section_start = header_line_end + 1
    else:
        job_section_start = 0

    # Truncate at footer marker
    footer_match = _FOOTER_RE.search(body, job_section_start)
    if footer_match:
        job_section = body[job_section_start : footer_match.start()]
    else:
        job_section = body[job_section_start:]

    # Try engage.indeed.com URLs first (legacy format)
    url_matches = list(INDEED_ENGAGE_URL_RE.finditer(job_section))
    id_fn = None  # default: _extract_job_id_from_engage_url

    # Fall back to rc/clk/dl URLs (2026+ format)
    if not url_matches:
        url_matches = list(INDEED_RC_CLK_URL_RE.finditer(job_section))
        id_fn = _extract_job_id  # uses jk= param extraction

    if not url_matches:
        return []

    jobs = []
    prev_end = 0  # tracks end of previous URL match within job_section

    for url_match in url_matches:
        url = url_match.group(0)
        block_text = job_section[prev_end : url_match.start()]
        prev_end = url_match.end()

        job = _parse_plaintext_job_block(block_text, url, email_date, extract_id_fn=id_fn)
        if job:
            jobs.append(job)

    return jobs


def _parse_plaintext_job_block(
    block_text: str,
    url: str,
    email_date: datetime | None,
    extract_id_fn=None,
) -> Job | None:
    """Parse a single job block (text preceding a delimiter URL).

    Shared by both alert (engage.indeed.com) and match (indeed.com/pagead)
    email formats — the block structure is identical.

    Returns a Job if the block looks like a real job listing (has title +
    company-location), or None if it's a preamble/footer link block.
    """
    # Split into non-empty lines, filtering noise
    raw_lines = [line.strip() for line in block_text.split("\n") if line.strip()]
    content_lines = []
    salary_line = None

    for line in raw_lines:
        # Skip age lines ("Just posted", "1 day ago", etc.)
        if _AGE_LINE_RE.match(line):
            continue
        # Skip noise labels
        if line.lower() in ("easily apply", "responsive employer"):
            continue
        # Skip summary count lines ("Jobs 1-2 of 2 new jobs")
        if _SUMMARY_COUNT_RE.match(line):
            continue
        # Skip "See matching results on Indeed: ..." navigation lines
        if _SEE_MATCHING_RE.match(line):
            continue
        # Check for salary before adding to content lines
        if "$" in line and _extract_salary_from_text(line) != (None, None):
            salary_line = line
            continue
        content_lines.append(line)

    # Need at least title + company-location to qualify as a job block
    if len(content_lines) < 2:
        return None

    title = content_lines[0]

    # Skip lines that look like URLs (preamble browse/unsubscribe blocks)
    if title.startswith("http"):
        return None

    # Skip very long titles (description snippets, not titles)
    if len(title) > 150:
        return None

    # Parse company and location from "Company - City, ST" format
    company_location = content_lines[1]
    if " - " in company_location:
        # Use rfind to handle company names containing dashes (e.g., "Turn/River - SF, CA")
        dash_idx = company_location.rfind(" - ")
        company = company_location[:dash_idx].strip()
        location = company_location[dash_idx + 3 :].strip()
    else:
        company = company_location
        location = "Unknown"

    # Extract salary from salary line if present
    # fmt: off
    # PORT-SEAM: fmt:off preserves the private wrap (line-length 99) — see
    # email_parsers/_common.py's fmt:off PORT-SEAM note. Content unchanged.
    salary_min, salary_max = (
        _extract_salary_from_text(salary_line) if salary_line else (None, None)
    )
    # fmt: on  # PORT-SEAM: end fmt:off block

    # Extract source_id using the appropriate extractor
    id_fn = extract_id_fn or _extract_job_id_from_engage_url
    source_id = id_fn(url)

    return Job(
        title=title,
        company=company,
        location=location,
        source="indeed",
        source_url=url,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def parse_indeed_alert(body: str, email_date: datetime | None = None) -> list[Job]:
    """Parse an Indeed job alert email body into Job objects.

    Uses plain-text parsing as the primary strategy (real Indeed emails arrive
    as plain text via Gmail API). Falls back to HTML strategies for legacy or
    fabricated HTML content.

    Meta-emails (digests, subscription confirmations without jobs) are filtered.

    Args:
        body: Email body from Gmail API. May be None or HTML or plain text.
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects (may be empty if no jobs found).
    """
    if not body or not body.strip():
        return []

    if is_meta_email(body[:200], extra_patterns=_INDEED_META_PATTERNS):
        logger.debug("Indeed parser: skipping meta-email (digest/admin)")
        return []

    # Plain-text strategy (primary): real Indeed emails are plain text
    if not _looks_like_html(body):
        jobs = _parse_plaintext(body, email_date)
        if jobs:
            return jobs

    # HTML strategy (fallback): legacy HTML format or future changes
    soup = BeautifulSoup(body, "html.parser")

    jobs = _parse_with_link_strategy(soup, email_date)

    if not jobs:
        jobs = _parse_with_card_strategy(soup, email_date)

    if not jobs:
        logger.warning(
            "Indeed parser: no jobs found -- email format may have changed. "
            "Archive the raw email and update indeed_parser.py."
        )

    return jobs


def _parse_with_link_strategy(soup: BeautifulSoup, email_date: datetime | None) -> list[Job]:
    """Strategy 1: Find all anchor tags with Indeed job URLs."""
    jobs = []
    seen_urls: set[str] = set()

    job_links = soup.find_all("a", href=INDEED_JOB_URL_RE)

    for link in job_links:
        href = link.get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        job = _extract_job_from_link(link, href, email_date)
        if job:
            jobs.append(job)

    return jobs


def _parse_with_card_strategy(soup: BeautifulSoup, email_date: datetime | None) -> list[Job]:
    """Strategy 2 (fallback): Find td/div containers with Indeed job links."""
    jobs = []
    seen_urls: set[str] = set()

    candidates = soup.find_all(["td", "div"], limit=200)

    for el in candidates:
        links = el.find_all("a", href=INDEED_JOB_URL_RE)
        if not links:
            continue

        href = links[0].get("href", "")
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)

        text = el.get_text(separator="\n", strip=True)
        if len(text) < 5 or len(text) > 3000:
            continue

        job = _extract_job_from_container(el, links[0], href, email_date)
        if job:
            jobs.append(job)

    return jobs


def _extract_job_from_link(link_tag, href: str, email_date: datetime | None) -> Job | None:
    """Extract job data from a job link and its surrounding context."""
    title = _extract_title_from_link(link_tag)
    if not title:
        return None

    # Find best container for context extraction
    container = (
        link_tag.find_parent("tr")
        or link_tag.find_parent("td")
        or link_tag.find_parent("div")
        or link_tag.parent
    )

    company = _extract_company_from_context(container, title) if container else "Unknown"
    location = _extract_location_from_context(container) if container else "Unknown"
    salary_min, salary_max = _extract_salary_from_text(
        container.get_text(separator=" ", strip=True) if container else ""
    )
    source_id = _extract_job_id(href)

    return Job(
        title=title,
        company=company,
        location=location,
        source="indeed",
        source_url=href,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_job_from_container(
    container, link_tag, href: str, email_date: datetime | None
) -> Job | None:
    """Extract job data from a container element (card strategy)."""
    title = _extract_title_from_link(link_tag)
    if not title:
        # Try text-based extraction from container
        lines = [
            line.strip()
            for line in container.get_text(separator="\n", strip=True).split("\n")
            if line.strip() and len(line.strip()) > 3
        ]
        if lines:
            title = lines[0]
        if not title or len(title) > 120:
            return None

    company = _extract_company_from_context(container, title)
    location = _extract_location_from_context(container)
    salary_min, salary_max = _extract_salary_from_text(
        container.get_text(separator=" ", strip=True)
    )
    source_id = _extract_job_id(href)

    return Job(
        title=title,
        company=company,
        location=location,
        source="indeed",
        source_url=href,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_title_from_link(link_tag) -> str | None:
    """Extract job title from an anchor tag."""
    # Check heading elements inside the link first
    for tag in ["h1", "h2", "h3", "h4", "strong", "b"]:
        el = link_tag.find(tag)
        if el:
            text = el.get_text(strip=True)
            if text and 3 <= len(text) <= 120:
                return text

    # Try link text directly
    link_text = link_tag.get_text(strip=True)
    if link_text and 3 <= len(link_text) <= 120:
        if link_text.lower() not in _GENERIC_LINK_TEXTS:
            return link_text

    # Check aria-label
    aria = link_tag.get("aria-label", "")
    if aria and 3 <= len(aria) <= 120:
        if aria.lower() not in _GENERIC_LINK_TEXTS:
            return aria

    return None


def _extract_company_from_context(container, title_text: str) -> str:
    """Find company name in elements near the job link.

    Indeed typically puts company name in a nearby span/td/div element.
    """
    if container is None:
        return "Unknown"

    title_lower = title_text.lower()

    # Search immediate children and descendants (spans, divs, tds, ps)
    for el in container.find_all(["span", "td", "div", "p"]):
        text = el.get_text(strip=True)
        # Company name heuristics: 2-60 chars, not the title, not salary-like
        if not text or len(text) < 2 or len(text) > 60:
            continue
        if text.lower() == title_lower:
            continue
        if looks_like_salary_text(text):
            continue
        if _looks_like_location(text):
            continue
        # Skip generic navigation text
        if text.lower() in {"view job", "apply", "apply now", "see more", "click here"}:
            continue
        # Skip if it looks like a URL
        if "http" in text or "www." in text:
            continue
        return text

    return "Unknown"


def _extract_location_from_context(container) -> str:
    """Find location text in elements near the job link."""
    if container is None:
        return "Unknown"

    for el in container.find_all(["span", "td", "div", "p"]):
        text = el.get_text(strip=True)
        if text and len(text) < 100 and _looks_like_location(text):
            return text

    return "Unknown"


def _looks_like_location(text: str) -> bool:
    """Return True if text matches common location patterns."""
    return bool(_LOCATION_RE.search(text))


def _extract_salary_from_text(text: str) -> tuple[int | None, int | None]:
    """Parse salary range from Indeed email text. Returns (salary_min, salary_max).

    P1.4 (D-2/D-3): the bespoke ×2080 annualization (plan §1.2 item 5, indeed
    addendum) is gone. Ranges — annual ("$120K - $150K") and hourly ("$70 - $80
    an hour") alike — delegate to ``salary_normalizer.parse_salary_text``, which
    detects the period cue and annualizes hourly ranges via the salvage ladder.
    Single hourly amounts ("$95 an hour", "$25/hr") are out of the shared range
    parser's scope, so they are detected here and routed through the normalizer
    as a single-value hourly observation — the ×2080 stays the normalizer's job.
    Implausible / sub-floor values quarantine to (None, None).
    """
    from jobcannon.engine.salary_normalizer import (
        RESOLVED_RESOLUTIONS,
        SalaryObservation,
        normalize_observation,
        parse_salary_text,
    )

    obs = parse_salary_text(text, provenance="email_snippet")
    if obs is None:
        # Single hourly amount: build a single-value hourly observation so the
        # normalizer (not this parser) does the unit math.
        single = HOURLY_SINGLE_RE.search(text) or HOURLY_RE.search(text)
        if not single:
            return None, None
        try:
            rate = float(single.group(1).replace(",", ""))
        except ValueError:
            return None, None
        obs = SalaryObservation(
            min_value=rate,
            max_value=rate,
            period="hourly",
            currency="USD",
            provenance="email_snippet",
            raw_text=text,
        )

    normalized = normalize_observation(obs)
    if normalized.resolution not in RESOLVED_RESOLUTIONS:
        return None, None
    return normalized.salary_min, normalized.salary_max


def _extract_job_id(url: str) -> str:
    """Extract the Indeed job key from a job URL.

    Tries jk= query parameter first (viewjob?jk=XXXX), then rc/clk?jk=XXXX,
    then falls back to the last path segment.
    """
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "jk" in qs:
            return qs["jk"][0]
        # Try path-based job ID
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            return path_parts[-1]
    except Exception:
        logger.debug("indeed field extraction failed", exc_info=True)

    return ""


# ---------------------------------------------------------------------------
# Indeed Match emails (donotreply@match.indeed.com)
# ---------------------------------------------------------------------------


def _extract_match_source_id(url: str) -> str:
    """Extract a source_id from an indeed.com/pagead/clk/dl URL.

    Uses the jrtk query param (unique per job tracking token).
    Falls back to jsa param, then last path segment.
    """
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "jrtk" in qs:
            return qs["jrtk"][0]
        if "jsa" in qs:
            return qs["jsa"][0]
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts:
            return path_parts[-1]
    except Exception:
        logger.debug("match source_id extraction failed", exc_info=True)
    return ""


def _extract_cts_source_id(url: str) -> str:
    """Extract a source_id from a cts.indeed.com/v3/... URL.

    Returns the first path segment after /v3/ (the encoded job payload).
    """
    try:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        # Path: /v3/ENCODED_SEGMENT/OPTIONAL_EXTRA
        if len(parts) >= 2 and parts[0] == "v3":
            return parts[1][:64]  # cap length for sanity
        if parts:
            return parts[-1][:64]
    except Exception:
        logger.debug("cts source_id extraction failed", exc_info=True)
    return ""


def _parse_match_multi_job(body: str, email_date: datetime | None) -> list[Job]:
    """Parse a multi-job Indeed match email.

    Same algorithm as _parse_plaintext but uses indeed.com/pagead/clk/dl URLs
    as delimiters. Skips the personalized intro paragraph by finding the
    "Jobs are based on..." preamble end marker.
    """
    # Skip preamble ("Hi FIRSTNAME... Jobs are based on your preferences...")
    preamble_match = _MATCH_PREAMBLE_END_RE.search(body)
    start = preamble_match.end() if preamble_match else 0

    # Truncate at footer
    footer_match = _MATCH_FOOTER_RE.search(body, start)
    job_section = body[start : footer_match.start()] if footer_match else body[start:]

    url_matches = list(INDEED_MATCH_URL_RE.finditer(job_section))
    if not url_matches:
        return []

    jobs = []
    prev_end = 0

    for url_match in url_matches:
        url = url_match.group(0)
        block_text = job_section[prev_end : url_match.start()]
        prev_end = url_match.end()

        job = _parse_plaintext_job_block(
            block_text,
            url,
            email_date,
            extract_id_fn=_extract_match_source_id,
        )
        if job:
            jobs.append(job)

    return jobs


def _parse_single_match_structured_block(
    body: str, url: str, source_id: str, email_date: datetime | None
) -> Job | None:
    """Parse the 2026-04+ Indeed match single-job format.

    Format::

        Hi FIRSTNAME,

        <personalized intro paragraph>

        <TITLE>
        <COMPANY>
        <LOCATION>
        Salary: <range>
        Job type: <type>
        Work setting: <setting>

        Benefits:
          - X
          - Y

        View job: <cts.indeed.com URL>

    Algorithm: anchor on the "View job:" line, then walk backwards through
    blank-line-separated paragraphs (skipping any Benefits paragraph) to find
    the structured block. The first three non-label lines of that block are
    title / company / location; "Salary:" yields the pay range.

    Returns None if the body doesn't have a "View job:" anchor line or the
    structured block can't be located (caller should fall through to the
    legacy intro-sentence parser).
    """
    view_match = _SINGLE_MATCH_VIEW_JOB_RE.search(body)
    if not view_match:
        return None

    # Real Indeed emails double-space content (`\n\n` between every line),
    # so paragraph-based grouping doesn't separate intro from structured block.
    # Instead, walk lines backwards from "View job:", classifying each:
    #   - skip blanks / benefits items / field labels (Job type / Work setting)
    #   - extract "Salary: ..." then skip
    #   - stop on intro-prose markers (long line, multi-sentence, greeting)
    #   - otherwise it's a content line — prepend to keep forward order
    before_view = body[: view_match.start()]
    content_lines: list[str] = []
    salary_min: int | None = None
    salary_max: int | None = None

    INTRO_LEN_THRESHOLD = 150

    for raw_line in reversed(before_view.splitlines()):
        stripped = raw_line.strip()
        if not stripped:
            continue

        # Intro detector: stop walking once we hit personalized prose
        low = stripped.lower()
        if (
            len(stripped) > INTRO_LEN_THRESHOLD
            or _MULTI_SENTENCE_RE.search(stripped)
            or (low.startswith("hi ") and stripped.endswith(","))
        ):
            break

        # Benefits items (start with a dash bullet after stripping leading ws)
        if stripped.startswith("-") or stripped.startswith("•"):
            continue
        if low == "benefits:":
            continue

        # Salary line: extract pay then skip
        if low.startswith("salary:"):
            salary_text = stripped.split(":", 1)[1].strip()
            salary_min, salary_max = _extract_salary_from_text(salary_text)
            continue

        # Other field labels: skip without inclusion
        if any(low.startswith(label) for label in _SINGLE_MATCH_FIELD_LABELS):
            continue

        # Content line: prepend to preserve source order (title/company/location)
        content_lines.insert(0, stripped)

    if len(content_lines) < 3:
        return None

    title = content_lines[0]
    company = content_lines[1]
    location = content_lines[2]

    # Sanity: reject obviously-wrong title/company lengths
    if len(title) > 150 or len(company) > 100:
        return None

    return Job(
        title=title,
        company=company,
        location=location,
        source="indeed",
        source_url=url,
        source_id=source_id,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _parse_single_match(body: str, email_date: datetime | None) -> list[Job]:
    """Parse a single-job Indeed match email.

    Tries two formats in order:

    1. **2026-04+ structured block**: intro paragraph followed by a
       title/company/location/Salary/Job type block, anchored by a
       "View job: <url>" line. See ``_parse_single_match_structured_block``.
    2. **Legacy intro sentence**: "We thought this job for a {title} at
       {company} in {location} paying {salary} would be a good fit. Check
       out the job at {url}" (with a no-pay variant).
    """
    # Find the CTS URL first (we need it regardless)
    url_match = INDEED_CTS_URL_RE.search(body)
    if not url_match:
        return []

    # First non-unsubscribe CTS URL anchors the job
    url = url_match.group(0)
    source_id = _extract_cts_source_id(url)

    # Primary: 2026-04+ structured block format
    job = _parse_single_match_structured_block(body, url, source_id, email_date)
    if job is not None:
        return [job]

    # Legacy: intro sentence with salary
    intro_match = _SINGLE_MATCH_INTRO_RE.search(body)
    if intro_match:
        title = intro_match.group(1).strip()
        company = intro_match.group(2).strip()
        location = intro_match.group(3).strip()
        salary_text = intro_match.group(4).strip()
        salary_min, salary_max = _extract_salary_from_text(salary_text)

        return [
            Job(
                title=title,
                company=company,
                location=location,
                source="indeed",
                source_url=url,
                source_id=source_id,
                salary_min=salary_min,
                salary_max=salary_max,
                posted_date=email_date,
            )
        ]

    # Legacy: intro sentence without salary
    no_pay_match = _SINGLE_MATCH_INTRO_NO_PAY_RE.search(body)
    if no_pay_match:
        title = no_pay_match.group(1).strip()
        company = no_pay_match.group(2).strip()
        location = no_pay_match.group(3).strip()

        return [
            Job(
                title=title,
                company=company,
                location=location,
                source="indeed",
                source_url=url,
                source_id=source_id,
                posted_date=email_date,
            )
        ]

    # Last resort: we have a URL but can't parse any known format
    logger.warning(
        "Indeed match parser: single-job format not recognized. "
        "Archive the raw email and update indeed_parser.py."
    )
    return []


def parse_indeed_match_alert(body: str, email_date: datetime | None = None) -> list[Job]:
    """Parse an Indeed match/recommendation email into Job objects.

    Handles two formats from donotreply@match.indeed.com:
      - Single-job: intro sentence with cts.indeed.com URL
      - Multi-job: job blocks delimited by indeed.com/pagead/clk/dl URLs

    Args:
        body: Email body from Gmail API (plain text).
        email_date: When the email was sent.

    Returns:
        List of parsed Job objects (may be empty).
    """
    if not body or not body.strip():
        return []

    # Multi-job: has indeed.com/pagead/clk/dl URLs
    if INDEED_MATCH_URL_RE.search(body):
        return _parse_match_multi_job(body, email_date)

    # Single-job: has cts.indeed.com URL
    if INDEED_CTS_URL_RE.search(body):
        return _parse_single_match(body, email_date)

    if len(body) > 500:
        logger.warning(
            "Indeed match parser: non-empty body yielded 0 jobs — email format may have changed."
        )

    return []

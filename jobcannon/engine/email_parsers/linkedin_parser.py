# PORTED from job_finder/parsers/linkedin_parser.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Parse LinkedIn job alert emails into Job objects.

LinkedIn sends two types of alert emails:
1. jobalerts-noreply@linkedin.com - Job Alert digests (structured, ~6 jobs per email)
2. jobs-noreply@linkedin.com - "Explore new jobs" recommendations

Both use plain text format with a consistent pattern:
    Title
    Company
    Location

    [metadata line like "1 school alum" or "This company is actively hiring"]
    View job: https://www.linkedin.com/comm/jobs/view/{JOB_ID}/...
    ---------------------------------------------------------

"""

import logging
import re
from datetime import datetime

from jobcannon.engine.models import Job
from jobcannon.engine.email_parsers._common import is_meta_email, parse_salary_range

logger = logging.getLogger(__name__)

# Regex to extract LinkedIn job ID from the tracking URL
LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)/")

# LinkedIn-specific meta-email pattern (supplements the shared base set).
_LINKEDIN_EXTRA_PATTERNS = [
    re.compile(r"you.ll receive notifications", re.IGNORECASE),
]

# Detects actual job listing URLs in a LinkedIn email body.
_VIEW_JOB_RE = re.compile(r"View job:\s*https://", re.IGNORECASE)


def _is_meta_email(body: str) -> bool:
    """Return True if the email preamble matches known meta-email patterns.

    LinkedIn sends two formats that contain "View job:" URLs:
    1. Meta digests: first line IS the count/digest pattern (e.g. "30+ new jobs").
       These are meta emails even with "View job:" URLs — single job examples.
    2. New AI-powered digests: first line is "Your job alert for ..." then a
       count line appears later. These are real job alerts — not meta.

    Strategy: check meta patterns first. If the preamble IS a meta pattern,
    it's meta regardless of "View job:" URLs. If not, it's a real alert.
    """
    preamble_is_meta = is_meta_email(body, extra_patterns=_LINKEDIN_EXTRA_PATTERNS)
    if not preamble_is_meta:
        return False
    # Preamble matched a meta pattern. But if the FIRST non-empty line is a
    # normal "Your job alert for" preamble, the meta pattern matched on a
    # secondary line — treat as real alert.
    first_line = ""
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped:
            first_line = stripped.lower()
            break
    return not first_line.startswith("your job alert for")


def parse_linkedin_alert(body: str, email_date: datetime | None = None) -> list[Job]:
    """Parse a LinkedIn job alert email body into Job objects.

    Args:
        body: Plain text email body from Gmail API.
        email_date: When the email was sent (used as approximate posting date).

    Returns:
        List of parsed Job objects, or [] for meta-email digests.
    """
    # Reject meta-emails (digest summaries, count notifications) before any parsing.
    # These are not individual job listings and would produce garbage job rows.
    if _is_meta_email(body):
        logger.debug("Skipping meta-email (pollution filter)")
        return []

    jobs = []

    # Split on the separator line
    blocks = re.split(r"-{10,}", body)

    for block in blocks:
        job = _parse_block(block.strip(), email_date)
        if job:
            jobs.append(job)

    if len(body) > 500 and not jobs:
        logger.warning(
            "LinkedIn parser: non-empty body yielded 0 jobs — email format may have changed."
        )

    return jobs


def _parse_block(block: str, email_date: datetime | None) -> Job | None:
    """Parse a single job block from a LinkedIn alert."""
    if not block:
        return None

    # Find the "View job:" URL
    url_match = re.search(r"View job:\s*(https://\S+)", block)
    if not url_match:
        return None

    raw_url = url_match.group(1)

    # Extract the LinkedIn job ID
    id_match = LINKEDIN_JOB_ID_RE.search(raw_url)
    source_id = id_match.group(1) if id_match else ""

    # Build a clean direct URL (strips tracking params)
    clean_url = f"https://www.linkedin.com/jobs/view/{source_id}/" if source_id else raw_url

    # Everything before "View job:" is the job info
    info_text = block[: url_match.start()].strip()

    # Split into non-empty lines
    lines = [line.strip() for line in info_text.split("\n") if line.strip()]

    if len(lines) < 2:
        return None

    # Filter out metadata lines (alumni counts, "actively hiring", salary snippets),
    # preamble lines from the new LinkedIn digest format (count line, section headers),
    # HTML tags LinkedIn embeds in plain-text bodies, and navigation/footer noise.
    content_lines = []
    for line in lines:
        # --- HTML tags embedded in text/plain body ---
        # LinkedIn's "New jobs from your other alerts" sections inject raw HTML
        # (e.g. <strong class="font-bold">senior data scientist</strong>) into
        # the plain-text body as section headers.  Strip them entirely.
        if "<" in line and ("</strong>" in line or "</a>" in line or "style=" in line):
            continue
        # Also catch standalone opening HTML tags that may appear on their own line
        if re.match(r"^\s*<\w+[\s>]", line):
            continue
        # --- Legacy metadata ---
        if re.match(r"^\d+ school alum", line, re.IGNORECASE):
            continue
        if re.match(r"^\d+ connection", line, re.IGNORECASE):
            continue
        if "actively hiring" in line.lower():
            continue
        if "apply with resume" in line.lower():
            continue
        # --- Preamble lines ---
        if re.match(r"^Your job alert", line, re.IGNORECASE):
            continue
        # Count lines: "30+ new jobs match…" / "New jobs match…"
        if re.match(r"^\d+\+?\s+new\s+jobs?", line, re.IGNORECASE):
            continue
        if re.match(r"^New jobs", line, re.IGNORECASE):
            continue
        # --- Section headers from "Expand your search" emails ---
        # Category labels like "Medical jobs", "AI/ML jobs", "Remote jobs"
        # that appear above job blocks in jobs-noreply emails.
        if re.match(r"^(Medical|AI/ML|Remote|Hybrid|Recent)\s+jobs?\b", line, re.IGNORECASE):
            continue
        # Generic pattern: "<topic> jobs" as a standalone section header
        if re.match(r"^[\w/]+\s+jobs$", line, re.IGNORECASE):
            continue
        # Broader fallback: "X jobs" or "X Devices jobs" (e.g. "Medical Devices jobs")
        if re.match(r"^[\w\s/&-]+\s+jobs$", line, re.IGNORECASE) and len(line) < 40:
            continue
        # --- Navigation/footer noise ---
        if re.match(r"^Manage\b", line, re.IGNORECASE):
            continue
        if re.match(r"^Results from\b", line, re.IGNORECASE):
            continue
        if re.match(r"^Expand your search", line, re.IGNORECASE):
            continue
        if re.match(r"^Recommendations based on", line, re.IGNORECASE):
            continue
        if re.match(r"^See all jobs", line, re.IGNORECASE):
            continue
        if re.match(r"^View all jobs", line, re.IGNORECASE):
            continue
        if re.match(r"^Edit alert\b", line, re.IGNORECASE):
            continue
        # "Jobs similar to X at Y" recommendations — navigation, not job listings
        if re.match(r"^Jobs similar to\b", line, re.IGNORECASE):
            continue
        # "You'll receive notifications" — confirmation noise
        if re.match(r"^You.ll receive", line, re.IGNORECASE):
            continue
        # URLs on their own line
        if re.match(r"^https?://", line):
            continue
        # "Try Premium" / upsell lines
        if re.match(r"^(Try Premium|Unlock personalized)", line, re.IGNORECASE):
            continue
        # Section transitions: "New jobs from your other alerts"
        if re.match(r"^New jobs from\b", line, re.IGNORECASE):
            continue
        # Footer identification lines
        if re.match(r"^This email was intended for\b", line, re.IGNORECASE):
            continue
        if re.match(r"^(Unsubscribe|Help|Learn why)\b", line, re.IGNORECASE):
            continue
        if re.match(r"^Jobs where you", line, re.IGNORECASE):
            continue
        if re.match(r"^Based on your profile", line, re.IGNORECASE):
            continue
        content_lines.append(line)

    if len(content_lines) < 2:
        return None

    # Pattern: Title, Company, Location (in that order)
    title = content_lines[0]
    company = content_lines[1]
    location = content_lines[2] if len(content_lines) >= 3 else "Unknown"

    # Sanity checks — reject blocks where the title is clearly not a real job title
    # URL in title: navigation line survived the filter
    if "https://" in title or "http://" in title:
        return None
    # HTML tags in title: LinkedIn plain-text body corruption
    if "<" in title and (">" in title or "style=" in title):
        return None
    # Title is a LinkedIn section header (category labels that survived filters)
    # fmt: off
    # PORT-SEAM: fmt:off preserves the private wrap (line-length 99) — see
    # email_parsers/_common.py's fmt:off PORT-SEAM note. Content unchanged.
    if re.match(
        r"^(See all|View all|Jobs similar|Expand|Manage|Edit alert)", title, re.IGNORECASE
    ):
        # fmt: on  # PORT-SEAM: end fmt:off block
        return None

    # Try to extract salary from the email snippet/subject if present
    salary_min, salary_max = _extract_salary(block)

    return Job(
        title=title,
        company=company,
        location=location,
        source="linkedin",
        source_url=clean_url,
        # source_id not persisted: the LinkedIn job ID is used only to build a
        # clean URL above. In practice it is not per-job-stable here (the same ID
        # has been observed across distinct postings), so it is not a valid
        # contract source_id (I-11).
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=email_date,
    )


def _extract_salary(text: str) -> tuple[int | None, int | None]:
    """Try to extract salary range from text.

    Delegates to the shared ``parse_salary_range`` in ``_common.py``.
    Kept as a thin wrapper so internal callers and tests can continue
    importing ``_extract_salary`` from this module.
    """
    return parse_salary_range(text)

# PORTED from job_finder/parsers/_positional_fallback.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Generic URL-anchored positional fallback for email parsers.

Scans an email body for recognized job-board / ATS URLs and emits a Job per
URL using nearby lines as title/company.  Used only when the primary parser
yields nothing on a job-bearing email (gated by extract_with_fallback).

The fallback is intentionally conservative:
- Returns ``[]`` unless at least one recognized job URL is found.
- Every emitted Job must have a non-empty title AND company (not a placeholder).
- ``Job`` construction is wrapped in try/except so a bad row is dropped, not raised.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from jobcannon.engine.models import Job
from jobcannon.engine.email_parsers._common import _PLACEHOLDER_STRINGS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL recognition
# ---------------------------------------------------------------------------

# Matches URLs for recognized job boards / ATS platforms.
# Pattern groups (all non-capturing, alternated):
#   linkedin  — /jobs/view/
#   indeed    — query param jk=
#   greenhouse — job-boards.greenhouse.io
#   lever     — jobs.lever.co
#   ashby     — jobs.ashbyhq.com
#   ziprecruiter — ziprecruiter.com/jobs|apply|job|c/.../Job
#   glassdoor partner — glassdoor.com/partner/jobListing
#   workday   — myworkdayjobs.com
#
# NOTE: jobright.ai URLs are deliberately NOT listed here. This generic fallback
# infers title/company from the LINES around a URL, which mis-attributes
# JobRight's plaintext layout (it emitted a location as the title and the next
# job's title as the company). Since the HTML jobright_parser is the real path
# and "ingest nothing + a zero-yield WARNING" beats ingesting garbage, JobRight
# plaintext bodies are left to fall through to [] rather than this fallback.
_JOB_URL_RE = re.compile(
    r"https?://\S*linkedin\.com/jobs/view/\S*"
    r"|https?://\S*indeed\.com/\S*[?&]jk=\S*"
    r"|https?://job-boards\.greenhouse\.io/\S+"
    r"|https?://jobs\.lever\.co/\S+"
    r"|https?://jobs\.ashbyhq\.com/\S+"
    r"|https?://\S*ziprecruiter\.com/(?:jobs|apply|job|c/[^/\s]+/Job)/\S*"
    r"|https?://\S*glassdoor\.com/partner/jobListing\S*"
    r"|https?://\S*myworkdayjobs\.com/\S+",
    re.IGNORECASE,
)

# Trailing punctuation characters that may be attached to a URL in plain text.
_URL_TRAILING_GARBAGE = re.compile(r'[.,;)\]>"\']+$')

# ---------------------------------------------------------------------------
# Line filtering helpers
# ---------------------------------------------------------------------------

# Common preamble / footer phrases that are never job titles or company names.
_SKIP_PHRASES = (
    "here's your",
    "weekly update",
    "new roles",
    "good fit",
    "apply early",
    "stand out",
    "unsubscribe",
    "view in browser",
    "copyright",
    "privacy policy",
    "terms of service",
    "email preferences",
    "manage your",
    "you received",
    "you're receiving",
    "click here",
)

_MIN_LINE_LEN = 3
_MAX_LINE_LEN = 150

# Window size (lines) to search before/after a URL for title/company
_WINDOW = 10


def _is_candidate_line(line: str) -> bool:
    """Return True if *line* could be a job title or company name."""
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) < _MIN_LINE_LEN or len(stripped) > _MAX_LINE_LEN:
        return False
    # Skip raw URLs
    if stripped.startswith("http") or stripped.startswith("(http"):
        return False
    # Skip parenthesised URLs (Greenhouse wraps in "( url )")
    if stripped.startswith("(") and "http" in stripped:
        return False
    lower = stripped.lower()
    return not any(phrase in lower for phrase in _SKIP_PHRASES)


def _is_placeholder(text: str) -> bool:
    """Return True if *text* is a known HTML-template placeholder string."""
    return text.lower().strip() in _PLACEHOLDER_STRINGS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def has_job_urls(body: str) -> bool:
    """Return True if *body* contains at least one recognized job board URL."""
    return bool(_JOB_URL_RE.search(body))


def positional_fallback(body: str, email_date: datetime | None = None) -> list[Job]:
    """Extract jobs from an email body using URL-anchored positional heuristics.

    For each recognized job URL found in *body*, inspects the surrounding lines
    to extract a title (lines before) and company (lines after) and emits a
    ``Job`` with ``source="email_fallback"``.

    This is the *last resort* strategy: it runs only when the primary parser
    returned ``[]`` on a body that contains at least one recognised job URL
    (enforced by the caller, :func:`extract_with_fallback`).

    Args:
        body: Email body text (plain text or lightly HTML-stripped).
        email_date: When the email was sent; stored as ``posted_date``.

    Returns:
        List of ``Job`` objects extracted from recognised URLs, possibly empty.
    """
    if not body:
        return []

    jobs: list[Job] = []
    seen_urls: set[str] = set()

    for match in _JOB_URL_RE.finditer(body):
        # Clean trailing punctuation from the matched URL
        url = _URL_TRAILING_GARBAGE.sub("", match.group(0))

        if url in seen_urls:
            continue
        seen_urls.add(url)

        title, company = _extract_title_and_company(body, match.start(), match.end())

        if not title or not company:
            logger.debug("positional_fallback: no title/company for URL %.80s", url)
            continue

        try:
            job = Job(
                title=title,
                company=company,
                location="",
                source="email_fallback",
                source_url=url,
                posted_date=email_date,
            )
            jobs.append(job)
        except ValueError as exc:
            logger.debug("positional_fallback: Job() rejected for URL %.80s: %s", url, exc)

    return jobs


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_title_and_company(
    body: str, url_start: int, url_end: int
) -> tuple[str | None, str | None]:
    """Return (title, company) extracted from lines surrounding a URL.

    - **Title**: the nearest valid non-placeholder line *before* the URL.
    - **Company**: the nearest valid non-placeholder line *after* the URL.
    """
    title = _find_title_before(body, url_start)
    company = _find_company_after(body, url_end)
    return title, company


def _find_title_before(body: str, url_start: int) -> str | None:
    """Walk backwards through lines before *url_start* to find a title."""
    before = body[:url_start]
    lines = [ln.strip() for ln in before.split("\n") if ln.strip()]
    # Inspect only the last WINDOW lines (nearest to the URL first)
    for line in reversed(lines[-_WINDOW:]):
        if _is_candidate_line(line) and not _is_placeholder(line):
            return line
    return None


def _find_company_after(body: str, url_end: int) -> str | None:
    """Walk forwards through lines after *url_end* to find a company name."""
    after = body[url_end:]
    lines = [ln.strip() for ln in after.split("\n") if ln.strip()]
    for line in lines[:_WINDOW]:
        # Skip closing parentheses left by Greenhouse-style "( url )" wrapping
        if line.startswith(")") or line.startswith("("):
            continue
        if _is_candidate_line(line) and not _is_placeholder(line):
            return line
    return None

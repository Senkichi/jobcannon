"""Single chokepoint: turn a job-posting URL + raw HTML into clean ``jd_full``.

JD Extraction Layer 2, step 2 ("platform-scoped + chrome strip"). This is the
ONE place that decides how a fetched page becomes ``jd_full``, so platform
scoping (e.g. LinkedIn's JD container) and page-chrome removal live in a single
enforced location instead of being re-derived per fetch tier.

Why it exists (2026-06-22 Penguin AI investigation): the generic fetch tiers
(``fetch_direct_jd``, ``fetch_ddg_jds``, the agentic Playwright fallback) ran
``html_to_clean_text`` over the WHOLE page. For LinkedIn guest pages that
captured the JD PLUS page chrome — "Similar jobs", "People also viewed",
"Explore top content on LinkedIn", the seniority/employment-type footer — and a
scrambled company-pitch span-soup. A scoped extractor (``fetch_linkedin_jd``)
already existed but was only wired into SOME tiers, so a LinkedIn URL arriving
via ``source_urls`` (the free tier's direct fetch) bypassed it and stored the
whole page. This module makes platform-scoped extraction the default for every
tier, with a chrome denylist as defense-in-depth.

Pipeline (``extract_clean_jd``):
  1. Detect the platform from the URL host/path.
  2. If the platform declares a JD container, extract from that subtree ONLY;
     fall back to whole-page extraction when the container is absent/empty.
  3. Unknown hosts → whole-page extraction (unchanged behavior).
  4. ``strip_trailing_chrome`` — remove known page-chrome blocks that survive
     recall-mode extraction (also the lever the corpus heal uses on already
     stored bodies, where re-fetching the raw HTML is not possible).

Pure function: no network, no DB, no config. Length capping stays the caller's
responsibility (callers already apply ``[:JD_STORAGE_MAX_CHARS]``).
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from jobcannon.engine.html_extract import html_to_clean_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform-scoped container selectors
# ---------------------------------------------------------------------------
# Map a detected platform key to the CSS selectors whose subtree holds ONLY the
# job description (no page chrome). Selectors are tried in order; the first that
# matches wins. Extend this dict to scope additional platforms — adding a key
# here is the entire surface area for teaching the pipeline a new platform.
_PLATFORM_SELECTORS: dict[str, tuple[str, ...]] = {
    # LinkedIn guest job pages: the JD lives in this container; everything else
    # on the page (similar jobs, "people also viewed", explore-content rail,
    # the seniority/employment footer) is chrome.
    "linkedin": ("div.show-more-less-html__markup", "div.description__text"),
}

# Scoped extraction shorter than this is treated as a miss (container absent,
# auth wall, or a markup variant we do not recognise) and we fall back to
# whole-page extraction. Mirrors enrichment_tiers._MIN_VALID_JD_CHARS.
_SCOPED_MIN_CHARS = 200


def detect_platform(url: str | None) -> str | None:
    """Return the platform key for *url*, or None when unrecognised.

    Host/path based and deliberately conservative: only URL shapes we have a
    scoped selector for return a key. Everything else falls through to
    whole-page extraction.
    """
    if not url:
        return None
    u = url.lower()
    if "linkedin.com" in u and "/jobs/" in u:
        return "linkedin"
    return None


# ---------------------------------------------------------------------------
# Page-chrome truncation (defense-in-depth + corpus heal lever)
# ---------------------------------------------------------------------------
# These markers denote the end of real JD content and the start of page chrome.
# Each is anchored tightly enough (markdown heading prefix or a verbatim,
# platform-specific phrase) that a false hit inside genuine JD prose is
# implausible. The EARLIEST marker found truncates the text — everything from
# that point on is chrome.
_CHROME_MARKERS: tuple[str, ...] = (
    r"^#{1,6}\s*Similar jobs\b",
    r"^#{1,6}\s*People also viewed\b",
    r"^#{1,6}\s*Similar Searches\b",
    r"^#{1,6}\s*Explore top content on LinkedIn\b",
    r"^#{1,6}\s*Seniority level\b",
    r"^#{1,6}\s*Employment type\b",
    r"Referrals increase your chances of interviewing",
    r"^Get notified about new\b.*\bjobs\b",
)

_CHROME_PATTERN = re.compile("|".join(_CHROME_MARKERS), re.IGNORECASE | re.MULTILINE)


def strip_trailing_chrome(text: str | None) -> str | None:
    """Truncate *text* at the first page-chrome marker, dropping the chrome tail.

    Returns the cleaned text (whitespace- and dangling-bullet-trimmed), or None
    when nothing usable remains. Idempotent: text with no chrome is returned
    essentially unchanged. Safe to run on already stored ``jd_full`` bodies (the
    corpus heal does exactly that — re-fetching the raw HTML is not possible).
    """
    if not text:
        return None
    m = _CHROME_PATTERN.search(text)
    if m is None:
        return text
    head = text[: m.start()]
    # The chrome block is typically preceded by a lone markdown bullet ("-") and
    # blank lines; strip those so the body does not end on a dangling separator.
    head = re.sub(r"[\s\-]+\Z", "", head)
    return head or None


def _extract_scoped(html: str, selectors: tuple[str, ...]) -> str | None:
    """Extract clean text from the FIRST matching selector subtree, else None."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:  # pragma: no cover - bs4 is very tolerant
        logger.debug("platform_extractor: BeautifulSoup parse failed: %s", exc)
        return None
    for selector in selectors:
        el = soup.select_one(selector)
        if el is not None:
            text = html_to_clean_text(str(el))
            if text and text.strip():
                return text
    return None


def extract_clean_jd(url: str | None, html: str | None) -> str | None:
    """Convert raw page *html* into clean JD text, scoped by *url*'s platform.

    The single chokepoint every fetch tier routes through. Returns clean,
    chrome-stripped markdown text, or None when no usable JD could be recovered.

    Known platform (LinkedIn, …): extraction is STRICT — only the scoped JD
    container is trusted. If that container is absent or yields too little, the
    function returns None rather than whole-page extracting, because the rest of
    a known platform's page IS chrome (the exact regression this module prevents);
    the caller's tier cascade then escalates to another source. Unknown hosts keep
    the legacy whole-page behavior (+ a chrome strip as defense-in-depth).
    """
    if not html or not html.strip():
        return None

    selectors = _PLATFORM_SELECTORS.get(detect_platform(url) or "")
    if selectors:
        text = _extract_scoped(html, selectors)
        # Strict: a missing / near-empty container on a known platform means this
        # is not the JD page we expect — fail closed (None), never whole-page.
        if not text or len(text.strip()) < _SCOPED_MIN_CHARS:
            return None
        return strip_trailing_chrome(text)

    text = html_to_clean_text(html)
    if not text or not text.strip():
        return None
    return strip_trailing_chrome(text)

# PORTED from job_finder/web/enrichment_tiers.py @ 0a4c33c5af7cd4055e539672158cb301b7bdc407 (private job-cannon). Ledger L-0178.
"""Shared direct-URL JD fetch helper for the enrichment tiers.

Split out of the private ``enrichment_tiers.py`` (design note PR-4): this is
the piece every tier that fetches a URL directly shares (the free tier's
sub-tier A, the SerpAPI apply-URL fetch, and the DDG web-search tier's
generic-URL branch).

# PORT-SEAM: JD_STORAGE_MAX_CHARS is the svc.jd_storage_max_chars seam
# (careers_scraper.py / _scraper_extract.py precedent, #369), read fresh at
# each truncation site rather than cached as a module constant.
"""

from __future__ import annotations

import logging

import requests

from jobcannon.engine._http_constants import _HEADERS, _REQUEST_TIMEOUT
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.platform_extractor import extract_clean_jd
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)

# Auth-wall signatures: if page text contains any of these (case-insensitive),
# the fetched page is a login/CAPTCHA wall, not a real JD. Return None.
_AUTH_WALL_SIGNATURES = [
    "we're signing you in",
    "sign in or join",
    "please verify you are a human",
    "access denied",
]

# Minimum text length for fetch_direct_jd to consider a fetch result a real JD.
# JS-rendered SPA shells (e.g., Workday at malformed URLs) leave only the page
# <title> after stripping <script>/<style>, producing single-token results like
# "Workday" that get persisted as fake JDs. Real JDs are far longer than this.
_MIN_VALID_JD_CHARS = 200


def is_short_auth_page(text: str) -> bool:
    """Return True if text looks like a short auth-wall or CAPTCHA page.

    Detection: page is under 2000 chars AND the first 500 chars contain
    an auth/bot signal keyword.
    """
    if not text or len(text) >= 2000:
        return False
    prefix = text[:500].lower()
    signals = [
        "sign in",
        "log in",
        "login",
        "captcha",
        "just a moment",
        "access denied",
        "verify you are human",
        "verify you are a human",
    ]
    return any(s in prefix for s in signals)


def fetch_direct_jd(url: str) -> str | None:
    """Attempt a direct HTTP GET and return cleaned job description text.

    Strips noisy HTML tags and returns cleaned text capped at
    ``svc.jd_storage_max_chars``.

    Args:
        url: The job URL to fetch.

    Returns:
        Cleaned text, or None on any error.
    """
    try:
        response = fetch_with_deadline(
            url, getter=requests.get, headers=_HEADERS, timeout=_REQUEST_TIMEOUT
        )
        response.raise_for_status()

        # Single chokepoint (JD Layer 2): platform-scoped extraction (e.g.
        # LinkedIn's JD container) + page-chrome strip, falling back to
        # whole-page extraction for unknown hosts. Routing the direct-URL fetch
        # through here is what stops a LinkedIn source_url from storing the
        # whole guest page (the 2026-06-22 Penguin AI regression).
        text = extract_clean_jd(url, response.text)
        if not text:
            logger.debug("fetch_direct_jd('%s'): no extractable text", url)
            return None

        # Reject auth-wall / CAPTCHA pages that return login HTML instead of JD
        text_lower = text.lower()
        if any(sig in text_lower for sig in _AUTH_WALL_SIGNATURES):
            logger.debug("Auth-wall detected for '%s', rejecting", url)
            return None

        stripped = text.strip()
        if len(stripped) < _MIN_VALID_JD_CHARS:
            logger.debug(
                "fetch_direct_jd('%s'): result %d chars (< %d), rejecting "
                "as SPA-shell or other empty page",
                url,
                len(stripped),
                _MIN_VALID_JD_CHARS,
            )
            return None

        svc = get_services()  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars
        return text[: svc.jd_storage_max_chars]

    except Exception as e:
        logger.debug("Direct fetch failed for '%s': %s", url, e)
        return None

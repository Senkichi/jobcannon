# PORTED from job_finder/web/enrichment_tiers.py @ 0a4c33c5af7cd4055e539672158cb301b7bdc407 (private job-cannon). Ledger L-0178.
"""DuckDuckGo web search tier + LinkedIn JD extraction + page-content helpers.

Split out of the private ``enrichment_tiers.py`` (design note PR-4). Binds to
``ScanServices.search_ddg_web`` / ``ScanServices.fetch_ddg_jds``.
``company_tokens`` / ``company_name_in_text`` have no ScanServices seam (no
caller in the already-landed data_enricher.py, L-0174, uses them) — ported
faithfully alongside their section-mates, unwired, matching the "port
standalone" precedent already applied elsewhere in this unit.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import requests
from ddgs import DDGS

from jobcannon.engine._enrichment_jd_fetch import fetch_direct_jd
from jobcannon.engine._http_constants import _REQUEST_TIMEOUT, _TIMEOUT
from jobcannon.engine.domain_policy import domain_priority, is_blocked_domain
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.platform_extractor import extract_clean_jd
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)

# Browser-like headers for sites that block bot UAs
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Chrome/login page detection signals
_CHROME_SIGNALS = [
    "download google chrome",
    "update your browser",
    "browser not supported",
    "enable cookies",
    "cookies are disabled",
    "accept cookies to continue",
]

_LOGIN_PAGE_SIGNALS = [
    "create your free account",
    "sign up for free",
    "start your free trial",
    "register to view",
    "join now to view",
]

# Delay between DDG web search queries (rate limiting)
_DDG_SEARCH_DELAY_S = 1.0

# Bounded retry / deadline constants for outbound DDG calls (issue #1277).
_DDG_CALL_TIMEOUT_S = 20.0
_DDG_MAX_RETRIES = 1
_DDG_RETRY_DELAY_S = 0.5

_COMPANY_STOP_WORDS = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "corp",
        "co",
        "the",
        "and",
        "group",
        "holdings",
        "international",
        "services",
        "solutions",
        "technologies",
    }
)


def company_tokens(company_name: str) -> list[str]:
    """Extract meaningful tokens from a company name, filtering stop words.

    Returns lowercase tokens that are >= 2 chars and not in the stop list.
    """
    if not company_name:
        return []
    raw_tokens = re.split(r"[\s.,;:!?&/|()]+", company_name.lower())
    return [t for t in raw_tokens if len(t) >= 2 and t not in _COMPANY_STOP_WORDS]


def company_name_in_text(company_name: str, text: str) -> bool:
    """Check whether any meaningful company token appears in the text."""
    tokens = company_tokens(company_name)
    if not tokens:
        return False
    text_lower = text.lower()
    return any(t in text_lower for t in tokens)


def is_chrome_or_login_page(text: str) -> bool:
    """Return True if text looks like a browser upgrade or login/signup page.

    Checks for Chrome download prompts, browser upgrade notices, cookie
    consent walls, and generic signup gates.

    Args:
        text: Cleaned page text to check.

    Returns:
        True if the page is a Chrome/browser page or login gate.
    """
    if not text:
        return False

    text_lower = text[:2000].lower()
    if any(sig in text_lower for sig in _CHROME_SIGNALS):
        return True
    return bool(any(sig in text_lower for sig in _LOGIN_PAGE_SIGNALS))


def fetch_linkedin_jd(url: str) -> str | None:
    """Extract job description from a LinkedIn guest job page.

    LinkedIn guest pages serve full JD content inside a specific container
    even though the surrounding page chrome contains login prompts that
    trip the generic auth-wall detector.

    Args:
        url: A LinkedIn job URL (e.g. linkedin.com/jobs/view/...).

    Returns:
        Cleaned JD text, or None if extraction fails.
    """
    try:
        response = fetch_with_deadline(
            url, getter=requests.get, headers=_BROWSER_HEADERS, timeout=_REQUEST_TIMEOUT
        )
        response.raise_for_status()

        # LinkedIn scoping now lives in the single chokepoint (extract_clean_jd
        # selects div.show-more-less-html__markup / div.description__text and
        # strips page chrome). This function stays as the LinkedIn-specific
        # entry point — browser headers + the existing callers (DDG tier, the
        # agentic Playwright shortcut) — but delegates the actual extraction so
        # there is exactly one definition of "what a LinkedIn JD looks like".
        text = extract_clean_jd(url, response.text)
        if not text or not text.strip():
            logger.debug("LinkedIn JD container not found for '%s'", url)
            return None

        svc = get_services()  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars
        return text[: svc.jd_storage_max_chars]

    except Exception as e:
        logger.debug("LinkedIn JD fetch failed for '%s': %s", url, e)
        return None


def _ddg_text_with_deadline(
    ddgs: Any,
    query: str,
    *,
    max_results: int = 5,
    deadline_s: float = _DDG_CALL_TIMEOUT_S,
) -> list[dict]:
    """Call ddgs.text() on a daemon worker with a hard wall-clock deadline.

    DDGS's own timeout only sets the underlying HTTP client's scalar timeout,
    which does not prevent a stalled TLS/connect from hanging the caller. This
    wrapper abandons the worker after ``deadline_s`` so the scheduler thread is
    never blocked indefinitely.
    """
    box: dict = {}
    done = threading.Event()

    def _work() -> None:
        try:
            box["result"] = list(ddgs.text(query, max_results=max_results))
        except BaseException as exc:
            box["exc"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_work, name="ddgs-text-deadline", daemon=True)
    worker.start()

    if not done.wait(timeout=deadline_s):
        raise TimeoutError(
            f"DDGS text search exceeded hard deadline of {deadline_s}s for query '{query[:60]}'"
        )

    if "exc" in box:
        raise box["exc"]
    return box["result"]


def search_ddg_web(title: str, company: str) -> dict:
    """Search DuckDuckGo web search for job description URLs and snippets.

    Generates two search queries, collects up to 8 candidate URLs, filters
    blocked domains, and sorts by domain priority.

    Args:
        title: Job title.
        company: Company name.

    Returns:
        Dict with keys:
        - "ddg_urls": list[str] of discovered URLs (up to 8)
        - "ddg_snippet": str concatenation of result body text
    """
    queries = [
        f'"{company}" "{title}" job description',
        f"{company} careers {title}",
    ]

    all_results: list[dict] = []
    seen_urls: set[str] = set()

    for i, query in enumerate(queries):
        results: list[dict] = []
        try:
            with DDGS(timeout=_TIMEOUT) as ddgs:
                for attempt in range(_DDG_MAX_RETRIES + 1):
                    try:
                        results = _ddg_text_with_deadline(
                            ddgs,
                            query,
                            max_results=5,
                            deadline_s=_DDG_CALL_TIMEOUT_S,
                        )
                        break
                    except Exception as exc:
                        if attempt < _DDG_MAX_RETRIES:
                            logger.debug(
                                "DDG web search failed for '%s' (attempt %d), retrying: %s",
                                query[:60],
                                attempt + 1,
                                exc,
                            )
                            time.sleep(_DDG_RETRY_DELAY_S)
                        else:
                            logger.debug(
                                "DDG web search failed for '%s' (attempt %d): %s",
                                query[:60],
                                attempt + 1,
                                exc,
                            )
        except Exception as exc:
            logger.debug("DDG web search failed for '%s': %s", query[:60], exc)

        # Empty result without exception = all engines exhausted for this query.
        # INFO not WARNING — the pipeline degrades gracefully to other search
        # backends, so this is not actionable for the operator.
        if not results:
            logger.info("DDGS: all engines returned empty for query '%s'", query[:80])

        for r in results:
            href = r.get("href", "")
            if href and href not in seen_urls:
                seen_urls.add(href)
                all_results.append(r)

        if i < len(queries) - 1:
            time.sleep(_DDG_SEARCH_DELAY_S)

    filtered_urls: list[str] = []
    for r in all_results:
        href = r.get("href", "")
        if href and not is_blocked_domain(href):
            filtered_urls.append(href)

    filtered_urls.sort(key=domain_priority)
    filtered_urls = filtered_urls[:8]

    snippets = [r.get("body", "") for r in all_results if r.get("body")]
    ddg_snippet = "\n\n".join(snippets) if snippets else ""

    return {
        "ddg_urls": filtered_urls,
        "ddg_snippet": ddg_snippet,
    }


def fetch_ddg_jds(urls: list[str]) -> tuple[str | None, str | None]:
    """Fetch job descriptions from DDG search result URLs.

    Tries each URL (up to 4 attempts), routing LinkedIn URLs through the
    specialized extractor and others through the generic fetcher.

    Args:
        urls: List of candidate URLs from DDG web search.

    Returns:
        2-tuple of (jd_text, source_url):
        - jd_text: First successful JD text (>= 200 chars), or None
        - source_url: The URL that yielded the JD, or None
    """
    for url in urls[:4]:
        try:
            if "linkedin.com/jobs/" in url:
                jd_text = fetch_linkedin_jd(url)
            elif is_blocked_domain(url):
                continue
            else:
                jd_text = fetch_direct_jd(url)

            if jd_text and len(jd_text) >= 200 and not is_chrome_or_login_page(jd_text):
                logger.debug("DDG URL fetch success: %s (%d chars)", url[:80], len(jd_text))
                return jd_text, url
        except Exception as exc:
            logger.debug("DDG URL fetch failed for %s: %s", url[:80], exc)

    return None, None

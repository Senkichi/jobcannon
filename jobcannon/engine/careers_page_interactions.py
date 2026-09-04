# PORTED from job_finder/web/careers_page_interactions.py @ e218953fefd532d071482e3ff859610a46dd179d (private job-cannon). Ledger L-0166.
"""Playwright interaction helpers for active careers page crawling.

Provides programmatic exploration of careers pages: URL parameter probing,
load-more clicking, infinite scroll, pagination following, search form
submission, and API endpoint discovery.

Each function takes a Playwright page object but does NOT own the page
lifecycle — the caller (careers_crawler.py) manages open/close.

Architecture:
- Zero API cost — all interactions are mechanical
- Designed for resilience: every function swallows exceptions and returns
  a safe default (empty list, False) on failure
- Intra-company rate limiting via _INTERACTION_DELAY_S between requests
"""

import json
import logging
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT
from jobcannon.engine.ats_platforms import _title_matches
from jobcannon.engine.ats_registry import REDIRECT_DOMAINS
from jobcannon.engine.http_fetch import fetch_with_deadline

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INTERACTION_DELAY_S = 0.5  # Delay between intra-company requests
_INTERACTION_WAIT_MS = 1500  # Wait after click/scroll for DOM update
_MAX_PAGINATION_PAGES = 5
_MAX_LOAD_MORE_CLICKS = 5
_MAX_SCROLLS = 5

# Button text patterns for "load more" detection (case-insensitive)
_LOAD_MORE_PATTERNS = re.compile(
    r"^(load\s+more|show\s+more|show\s+all|view\s+all|see\s+all|"
    r"more\s+jobs|more\s+openings|view\s+all\s+jobs|view\s+all\s+openings|"
    r"see\s+more\s+jobs|load\s+more\s+jobs|show\s+all\s+jobs)$",
    re.IGNORECASE,
)

# CSS selectors for search inputs (tried in order)
_SEARCH_INPUT_SELECTORS = [
    'input[type="search"]',
    'input[placeholder*="search" i]',
    'input[name*="search" i]',
    'input[aria-label*="search" i]',
    'input[id*="search" i]',
    'input[placeholder*="keyword" i]',
    'input[name*="keyword" i]',
]

# URL parameter names to try for keyword search
_SEARCH_PARAM_NAMES = ["q", "search", "query", "keywords", "keyword", "title"]

# Patterns in XHR/fetch URLs that indicate a jobs API
_API_PATTERNS = [
    "/api/jobs",
    "/api/positions",
    "/api/openings",
    "/api/careers",
    "/api/v1/jobs",
    "/api/v2/jobs",
    "/jobs.json",
    "/positions.json",
    "/openings.json",
    "/careers.json",
    "/wday/cxs/",  # Workday REST API
]

# Field-alias helpers are defined in _field_alias and re-exported here
# under the private names that existing callers in this module use.
from jobcannon.engine._field_alias import (  # noqa: E402
    JOB_TITLE_FIELDS as _JOB_TITLE_FIELDS,
)
from jobcannon.engine._field_alias import (  # noqa: E402
    JOB_URL_FIELDS as _JOB_URL_FIELDS,
)
from jobcannon.engine._field_alias import (  # noqa: E402
    extract_field as _extract_field,
)
from jobcannon.engine._field_alias import (  # noqa: E402
    find_job_array as _find_job_array,
)

# Navigation path prefixes to skip (not job links)
_NAV_PATH_PREFIXES = (
    "/about",
    "/contact",
    "/blog",
    "/news",
    "/press",
    "/privacy",
    "/terms",
    "/legal",
    "/login",
    "/signup",
    "/register",
    "/faq",
    "/help",
    "/support",
    "/accessibility",
    "/sitemap",
    "/cookie",
    "/search",
    "/events",
)


# ---------------------------------------------------------------------------
# URL parameter probing (zero cost, no Playwright)
# ---------------------------------------------------------------------------


def probe_url_params(
    careers_url: str,
    keywords: list[str],
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict]:
    """Try appending search params to careers URL — no Playwright needed.

    For each keyword, tries common parameter names (?q=, ?search=, etc.).
    Short-circuits on first param name that yields results for any keyword.

    Args:
        careers_url: Base careers page URL.
        keywords: Deduplicated search keywords derived from target_titles.
        target_titles: For post-extraction filtering.
        exclusions: For post-extraction filtering.

    Returns:
        List of job dicts with title, url, description keys. Empty if no
        param name yielded results.
    """
    # Lazy import to avoid circular dependency
    from jobcannon.engine.careers_crawler import _extract_jobs_from_soup

    parsed = urlparse(careers_url)
    existing_params = parse_qs(parsed.query)

    all_jobs: list[dict] = []
    seen_urls: set[str] = set()
    winning_param: str | None = None

    for param_name in _SEARCH_PARAM_NAMES:
        if winning_param is not None:
            break  # Already found a param that works

        for keyword in keywords:
            # Build URL with search parameter
            params = dict(existing_params)
            params[param_name] = [keyword]
            query_string = urlencode(params, doseq=True)
            search_url = urlunparse(parsed._replace(query=query_string))

            try:
                resp = fetch_with_deadline(
                    search_url,
                    getter=requests.get,
                    timeout=_TIMEOUT,
                    headers=_HEADERS,
                )
                if resp.status_code >= 400:
                    continue

                # Check if response is JSON (API endpoint)
                content_type = resp.headers.get("Content-Type", "")
                if "json" in content_type:
                    try:
                        json_data = resp.json()
                        jobs = parse_api_response(
                            json_data,
                            target_titles,
                            exclusions,
                            careers_url,
                        )
                        if jobs:
                            winning_param = param_name
                            for job in jobs:
                                if job["url"] not in seen_urls:
                                    seen_urls.add(job["url"])
                                    all_jobs.append(job)
                            continue
                    except (json.JSONDecodeError, ValueError):
                        pass

                # Try HTML extraction
                soup = BeautifulSoup(resp.text, "html.parser")
                jobs = _extract_jobs_from_soup(
                    soup,
                    search_url,
                    target_titles,
                    exclusions,
                )

                if jobs:
                    winning_param = param_name
                    for job in jobs:
                        url = job.get("url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            all_jobs.append(job)

            except Exception as e:
                logger.debug(
                    "probe_url_params failed for '%s': %s",
                    search_url,
                    e,
                )

            time.sleep(_INTERACTION_DELAY_S)

    if all_jobs:
        logger.info(
            "probe_url_params('%s'): %d jobs via ?%s= param",
            careers_url,
            len(all_jobs),
            winning_param,
        )

    return all_jobs


# ---------------------------------------------------------------------------
# Playwright interaction helpers
# ---------------------------------------------------------------------------


def setup_api_capture(page) -> list[str]:
    """Register request listener BEFORE page.goto().

    Returns a mutable list that accumulates intercepted API URLs during
    page load and subsequent interactions.
    """
    captured: list[str] = []

    def _on_request(request):
        url = request.url
        if request.resource_type not in ("xhr", "fetch"):
            return
        url_lower = url.lower()
        if any(pattern in url_lower for pattern in _API_PATTERNS):
            captured.append(url)

    page.on("request", _on_request)
    return captured


def click_load_more(page, max_clicks: int = _MAX_LOAD_MORE_CLICKS) -> bool:
    """Click 'Load more' / 'Show all' buttons on a rendered page.

    Scans for buttons and links whose visible text matches load-more
    patterns. Clicks each occurrence, waits for DOM update, repeats
    up to max_clicks times.

    Args:
        page: Playwright page (already navigated and rendered).
        max_clicks: Maximum number of button clicks.

    Returns:
        True if at least one button was clicked.
    """
    clicked_any = False

    for _ in range(max_clicks):
        # Find all clickable elements with load-more text
        clicked_this_round = False
        for selector in ["button", "a", '[role="button"]']:
            try:
                elements = page.query_selector_all(selector)
                for el in elements:
                    try:
                        text = (el.text_content() or "").strip()
                        if text and _LOAD_MORE_PATTERNS.match(text):
                            el.click()
                            page.wait_for_timeout(_INTERACTION_WAIT_MS)
                            clicked_any = True
                            clicked_this_round = True
                            break  # Re-scan after click (DOM may have changed)
                    except Exception:
                        continue
            except Exception:
                continue

            if clicked_this_round:
                break

        if not clicked_this_round:
            break  # No more load-more buttons found

    if clicked_any:
        logger.debug("click_load_more: clicked load-more buttons")

    return clicked_any


def scroll_for_content(page, max_scrolls: int = _MAX_SCROLLS) -> bool:
    """Scroll to bottom to trigger infinite scroll loading.

    Scrolls to document bottom, waits for content to load, checks if
    page height grew. Stops when height stabilizes or max_scrolls reached.

    Returns:
        True if new content appeared during scrolling.
    """
    grew = False

    try:
        prev_height = page.evaluate("document.body.scrollHeight")

        for _ in range(max_scrolls):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(_INTERACTION_WAIT_MS)
            new_height = page.evaluate("document.body.scrollHeight")

            if new_height > prev_height:
                grew = True
                prev_height = new_height
            else:
                break  # Height stabilized

    except Exception as e:
        logger.debug("scroll_for_content error: %s", e)

    if grew:
        logger.debug("scroll_for_content: page grew during scrolling")

    return grew


def follow_pagination(
    page,
    base_url: str,
    max_pages: int = _MAX_PAGINATION_PAGES,
) -> list[str]:
    """Find pagination links on the current page.

    Looks for next-page links via rel="next", aria-label, and numbered
    pagination patterns. Returns absolute URLs to fetch.

    Args:
        page: Playwright page (already navigated).
        base_url: Base URL for resolving relative hrefs.
        max_pages: Maximum pagination URLs to return.

    Returns:
        List of absolute URLs for subsequent pages.
    """
    urls: list[str] = []
    seen: set[str] = set()
    seen.add(base_url)

    try:
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Strategy 1: rel="next" link
        next_link = soup.find("a", rel="next")
        if next_link and next_link.get("href"):
            url = urljoin(base_url, next_link["href"])
            if url not in seen and not _is_ats_url(url):
                urls.append(url)
                seen.add(url)

        # Strategy 2: aria-label containing "next"
        for tag in soup.find_all("a", attrs={"aria-label": True}):
            label = tag.get("aria-label", "").lower()
            if "next" in label and tag.get("href"):
                url = urljoin(base_url, tag["href"])
                if url not in seen and not _is_ats_url(url):
                    urls.append(url)
                    seen.add(url)

        # Strategy 3: Numbered pagination links (2, 3, 4...)
        for tag in soup.find_all("a", href=True):
            text = tag.get_text(strip=True)
            if text.isdigit() and int(text) > 1:
                url = urljoin(base_url, tag["href"])
                if url not in seen and not _is_ats_url(url):
                    urls.append(url)
                    seen.add(url)

    except Exception as e:
        logger.debug("follow_pagination error: %s", e)

    return urls[:max_pages]


def submit_search_form(page, keyword: str) -> bool:
    """Find a search input on the page and submit a keyword query.

    Tries multiple CSS selectors for search inputs. If found, clears
    the field, types the keyword, and presses Enter.

    Args:
        page: Playwright page (already navigated and rendered).
        keyword: Search keyword to submit.

    Returns:
        True if a search input was found and submitted.
    """
    for selector in _SEARCH_INPUT_SELECTORS:
        try:
            element = page.query_selector(selector)
            if element and element.is_visible():
                element.fill("")
                element.fill(keyword)
                page.keyboard.press("Enter")
                page.wait_for_timeout(_INTERACTION_WAIT_MS)
                logger.debug(
                    "submit_search_form: submitted '%s' via %s",
                    keyword,
                    selector,
                )
                return True
        except Exception:
            continue

    return False


# ---------------------------------------------------------------------------
# API response parsing
# ---------------------------------------------------------------------------


def parse_api_response(
    json_data,
    target_titles: list[str],
    exclusions: list[str],
    base_url: str = "",
) -> list[dict]:
    """Parse a JSON API response for job-like objects.

    Handles common shapes: {jobs: [...]}, {results: [...]}, {data: [...]},
    or bare arrays. Extracts title and URL from each job object using
    common field name patterns.

    Args:
        json_data: Parsed JSON data (dict or list).
        target_titles: For keyword filtering.
        exclusions: For exclusion filtering.
        base_url: For resolving relative URLs.

    Returns:
        List of filtered job dicts with title, url, description keys.
    """
    job_array = _find_job_array(json_data)
    if not job_array:
        return []

    results: list[dict] = []
    seen_urls: set[str] = set()

    for item in job_array:
        if not isinstance(item, dict):
            continue

        title = _extract_field(item, _JOB_TITLE_FIELDS)
        if not title or not isinstance(title, str):
            continue

        if not _title_matches(title, target_titles, exclusions):
            continue

        url = _extract_field(item, _JOB_URL_FIELDS) or ""
        if isinstance(url, str) and url.startswith("/") and base_url:
            url = urljoin(base_url, url)

        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)

        results.append({"title": title, "url": url, "description": ""})

    return results


# ---------------------------------------------------------------------------
# Keyword deduplication
# ---------------------------------------------------------------------------


def deduplicate_keywords(target_titles: list[str]) -> list[str]:
    """Deduplicate target_titles by containment for search use.

    Drops titles that are superstrings of shorter titles (e.g.,
    "Senior Data Scientist" is dropped when "Data Scientist" is present).
    Returns up to 3 unique search terms.

    Args:
        target_titles: Raw target title keywords from config.

    Returns:
        Deduplicated list of search keywords, max 3.
    """
    if not target_titles:
        return []

    # Sort by length ascending so shorter strings come first
    sorted_titles = sorted(target_titles, key=len)
    unique: list[str] = []

    for title in sorted_titles:
        title_lower = title.lower()
        # Skip if this title is a superstring of an already-included keyword
        if any(kept.lower() in title_lower for kept in unique):
            continue
        unique.append(title)

    return unique[:3]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_ats_url(url: str) -> bool:
    """Check if a URL points to a known ATS domain.

    Host-boundary matched (exact or subdomain) against the PARSED hostname —
    never a substring of netloc — so a look-alike host
    ("boards.greenhouse.io.evil.example") is not misdetected as an ATS URL
    (py/incomplete-url-substring-sanitization guard)."""
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in REDIRECT_DOMAINS)


# _find_job_array and _extract_field are imported from jobcannon.engine._field_alias
# above — they are not re-defined here.  Any internal callers reference them
# via the ``_find_job_array`` / ``_extract_field`` aliases established in the
# import block so no call-site changes are needed.

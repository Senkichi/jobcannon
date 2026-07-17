"""Tesla platform scanner — Playwright-based with cua-api interception.

Tesla careers (https://www.tesla.com/careers) is a JS-rendered SPA that fetches
job data from an internal API endpoint (cua-api/apps/careers/state). This endpoint
returns 403 to direct HTTP requests (anti-bot protection), so we use Playwright
to load the page in a real browser context and intercept the XHR/fetch response.

The scanner:
1. Loads the Tesla careers page in Playwright
2. Intercepts the cua-api/apps/careers/state XHR/fetch response
3. Parses the JSON: listings carry abbreviated keys (t/dp/l/y) that resolve to
   human-readable values via the ``lookup.{departments,locations,types}`` tables
4. Returns normalized job records with resolved human-readable values

Fallback: If API interception fails, DOM extraction is attempted as a secondary path.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from jobcannon.engine.ats_platforms._platforms_icims import PlaywrightPlatformScanner

logger = logging.getLogger(__name__)

_COMPANY_SOURCE = "Tesla"

# Tesla careers page URL
_CAREERS_URL = "https://www.tesla.com/careers"

# cua-api state endpoint pattern (for interception)
_CUA_API_PATTERN = re.compile(r"cua-api/apps/careers/state", re.IGNORECASE)

# Playwright timing
_PLAYWRIGHT_TIMEOUT_MS = 30000  # Tesla SPA may take longer to load
_JS_SETTLE_MS = 3000  # Wait for JS to finish rendering


def _fetch_postings(browser, slug: str, *, max_load_more: int = 0) -> list[dict]:
    """Fetch Tesla job postings via Playwright with cua-api interception.

    Args:
        browser: Playwright Browser instance (already launched by the orchestrator).
        slug: Ignored for Tesla (single-tenant platform).
        max_load_more: Ignored (Tesla loads all jobs via the API).

    Returns:
        Raw posting dicts with title, location, department, source_url, source_id.
        Empty on fetch error or no postings.
    """
    page = None
    api_response = None

    try:
        page = browser.new_page()

        # Set up response interception for the cua-api endpoint
        def handle_response(response):
            nonlocal api_response
            if _CUA_API_PATTERN.search(response.url):
                try:
                    api_response = response.json()
                except Exception:
                    # If JSON parsing fails, we'll fall back to DOM extraction
                    pass

        page.on("response", handle_response)

        # Navigate to the careers page
        page.goto(_CAREERS_URL, timeout=_PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(_JS_SETTLE_MS)

        # If we captured the API response, parse it
        if api_response:
            return _parse_cua_api_response(api_response)

        # Fallback: DOM extraction
        logger.debug("Tesla cua-api interception failed, falling back to DOM extraction")
        return _extract_postings_from_dom(page.content(), _CAREERS_URL)

    except Exception as exc:
        logger.debug("scan_tesla() failed: %s", exc)
        return []
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def _parse_cua_api_response(api_data: dict) -> list[dict]:
    """Parse the Tesla cua-api JSON response into raw posting dicts.

    Real response shape (verified against a live cua-api/apps/careers/state capture):

        {
            "listings": [
                {"id": "224501", "t": "AI Engineer...", "dp": "5", "l": "401022",
                 "y": 1, "f": "74", "sp": 1, "pu": null},
                ...
            ],
            "lookup": {
                "departments": {"5": "Tesla AI", ...},                    # dp -> name
                "locations":   {"401022": "Palo Alto, California", ...},  # l  -> "City, State"
                "types":       {"1": "fulltime", "3": "intern", ...},     # y  -> name
                "regions":     {"5": "North America", ...},
                "sites":       {"US": "United States", ...},
            },
            "departments": {...},  # parent->subteam hierarchy (unused here)
            "geo": [...],          # region->site->state->city tree (unused here)
        }

    Listings carry ABBREVIATED keys (``t``/``dp``/``l``/``y``); the human-readable
    names live under ``lookup.*`` — NOT as top-level ``locations``/``regions`` maps.
    ``lookup.locations`` already yields a fully-formed "City, State" string. ``pu`` is
    a date, not a URL, so the apply URL is constructed from the numeric ``id``.

    Args:
        api_data: The JSON response from cua-api/apps/careers/state.

    Returns:
        Raw posting dicts with department/location/type resolved to human-readable
        values. A listing whose location id does not resolve gets an EMPTY location
        (a raw numeric id is worse than none), never a bogus value.
    """
    listings = api_data.get("listings", []) or []
    lookup = api_data.get("lookup", {}) or {}
    departments = lookup.get("departments", {}) or {}
    locations = lookup.get("locations", {}) or {}
    types = lookup.get("types", {}) or {}

    postings = []
    for listing in listings:
        job_id = listing.get("id")
        dept_id = listing.get("dp")
        loc_id = listing.get("l")
        type_id = listing.get("y")

        department = departments.get(str(dept_id), "") if dept_id is not None else ""
        # lookup.locations already yields "City, State"; leave empty (not the raw id)
        # when unresolved so downstream never sees a meaningless numeric location.
        location = locations.get(str(loc_id), "") if loc_id is not None else ""
        employment_type = types.get(str(type_id), "") if type_id is not None else ""

        # pu is a date field, not a URL — construct the apply URL from the numeric id.
        source_url = (
            f"https://www.tesla.com/careers/search/job/{job_id}" if job_id else _CAREERS_URL
        )

        postings.append(
            {
                "title": listing.get("t") or "",
                "source_url": source_url,
                "source_id": str(job_id) if job_id else None,
                "location": location,
                "department": department,
                "employment_type": employment_type,
            }
        )

    logger.debug("Tesla cua-api parsed %d postings", len(postings))
    return postings


def _extract_postings_from_dom(html: str, base_url: str) -> list[dict]:
    """Fallback DOM extraction when API interception fails.

    This is a best-effort fallback that looks for job cards in the rendered DOM.
    It may not capture all metadata (department/region) but provides basic job data.

    Args:
        html: Rendered HTML from the careers page.
        base_url: Base URL for resolving relative links.

    Returns:
        Raw posting dicts with limited metadata.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    postings = []

    # Try to find job cards - Tesla uses various class names that may change
    # This is a generic fallback pattern
    job_selectors = [
        "a[href*='/careers/job/']",
        "[data-testid*='job']",
        ".job-card",
        ".career-card",
    ]

    for selector in job_selectors:
        for element in soup.select(selector):
            href = element.get("href", "")
            if not href or "/careers/job/" not in href:
                continue

            # Extract title from the element text
            title = element.get_text(" ", strip=True)
            if not title:
                continue

            source_url = urljoin(base_url, href)
            # Extract job ID from URL
            job_id_match = re.search(r"/job/(\d+)", source_url)
            job_id = job_id_match.group(1) if job_id_match else None

            postings.append(
                {
                    "title": title,
                    "source_url": source_url,
                    "source_id": job_id,
                    "location": "",  # Not available in DOM fallback
                    "department": "",  # Not available in DOM fallback
                }
            )

    if postings:
        logger.debug("Tesla DOM fallback extracted %d postings", len(postings))
    else:
        logger.debug("Tesla DOM fallback found no postings")

    return postings


def _posting_to_job(posting: dict, slug: str) -> dict:
    """Map one raw Tesla posting to the canonical job dict.

    ``description`` is empty — the cua-api response exposes only title +
    location/department metadata, so ``jd_full`` is filled later by enrichment.
    ``posted_date`` is ``None``: Tesla API does not reliably expose a
    first-posted timestamp, and a wrong date is worse than no date (D-08).
    """
    return {
        "title": posting.get("title", ""),
        "company_source": _COMPANY_SOURCE,
        "location": posting.get("location") or "",
        "locations_structured": [],
        "description": "",
        "source_url": posting.get("source_url") or "",
        "source_id": posting.get("source_id") or None,
        "posted_date": None,
        "posted_date_precision": None,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlaywrightPlatformScanner(
    name="tesla",
    company_source=_COMPANY_SOURCE,
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)

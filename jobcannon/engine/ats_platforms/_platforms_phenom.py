"""Phenom platform scanner — sitemap-based (no public JSON API).

Phenom-hosted career portals do not expose a public unauthenticated JSON endpoint
for job listings. Job URLs are exposed through sitemaps at
``https://{host}/{locale}/sitemap_index.xml`` which reference sitemapN.xml files
containing individual job URLs in the pattern:
``https://{host}/{locale}/job/{job_id}/{title_slug}``

The scanner:
1. Fetches the sitemap index to discover sitemapN.xml files
2. Parses each sitemap to extract job URLs
3. Derives candidate titles from URL slugs (cheap)
4. Returns jobs with URL-derived data only (no Playwright fetches)

Full detail (jd_full) is backfilled later by enrichment for jobs that pass
the title gate. This avoids expensive Playwright fetches for jobs that would
be filtered out anyway. The slug is the Phenom tenant's careers host
(e.g. ``"careers.conduent.com"``).
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_platforms._platforms_icims import PlaywrightPlatformScanner
from jobcannon.engine.ats_platforms._registry import _auth_block_statuses
from jobcannon.engine.careers_crawler._title_filters import clean_title

logger = logging.getLogger(__name__)

_COMPANY_SOURCE = "Phenom"

# Phenom job URL pattern: /job/{job_id}/{title_slug} (locale-agnostic)
# Job ID can be numeric or alphanumeric (e.g., 12345, R260012977, JR-12345)
_JOB_URL_RE = re.compile(r"/job/([^/]+)/", re.IGNORECASE)


def _sitemap_has_jobs(sitemap_url: str) -> bool:
    """Check if a sitemap (index or flat) contains job URLs.

    This is used to select the best sitemap when robots.txt lists multiple.
    Returns True if the sitemap is a flat urlset with job URLs or an index
    that references sub-sitemaps containing job URLs.
    """
    try:
        resp = get_session().get(sitemap_url, timeout=5)
        if resp.status_code != 200:
            return False

        soup = BeautifulSoup(resp.text, "xml")
        all_locs = [loc.get_text(strip=True) for loc in soup.find_all("loc")]

        # Check for direct job URLs (flat urlset)
        if any("/job/" in url for url in all_locs):
            return True

        # Check for sub-sitemaps (index) - sample the first one
        sub_sitemaps = [url for url in all_locs if "sitemap" in url.lower()]
        if sub_sitemaps:
            try:
                sample_resp = get_session().get(sub_sitemaps[0], timeout=5)
                if sample_resp.status_code == 200:
                    sample_soup = BeautifulSoup(sample_resp.text, "xml")
                    sample_locs = [loc.get_text(strip=True) for loc in sample_soup.find_all("loc")]
                    return any("/job/" in url for url in sample_locs)
            except Exception:
                pass

        return False
    except Exception:
        return False


def _sitemap_index_url(slug: str, careers_url: str | None = None) -> str:
    """Build the sitemap index URL for a Phenom tenant.

    ``slug`` is the careers host (e.g. ``"careers.conduent.com"``).
    ``careers_url`` is the full careers URL if known (e.g. ``"https://careers.bmo.com/ca/en"``).

    Strategy:
    1. Try robots.txt first (canonical, locale-independent discovery)
    2. If careers_url is provided, extract locale path from it
    3. Fall back to minimal common paths (us/en, bare-root) as last resort

    Returns the sitemap index URL.
    """
    host = slug.strip().replace("https://", "").replace("http://", "").split("/")[0]
    base_url = f"https://{host}"

    # Strategy 1: Try robots.txt for Sitemap directive (canonical, locale-independent)
    try:
        robots_url = f"{base_url}/robots.txt"
        resp = get_session().get(robots_url, timeout=5)
        if resp.status_code == 200:
            sitemap_urls = []
            for line in resp.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if "sitemap" in sitemap_url.lower():
                        sitemap_urls.append(sitemap_url)
            # If multiple sitemaps, try them and return the first that yields job URLs
            if sitemap_urls:
                # Prefer sitemap_index.xml files (they contain references to other sitemaps)
                index_sitemaps = [url for url in sitemap_urls if "sitemap_index.xml" in url.lower()]
                if index_sitemaps:
                    # Try us/en sitemap index first if available
                    for url in index_sitemaps:
                        if "/us/en/" in url.lower():
                            if _sitemap_has_jobs(url):
                                return url
                    # Try other index sitemaps
                    for url in index_sitemaps:
                        if _sitemap_has_jobs(url):
                            return url
                # Try all sitemaps (including non-index ones)
                for url in sitemap_urls:
                    if _sitemap_has_jobs(url):
                        return url
                # If none yield jobs, return the first one (will be handled as empty later)
                return sitemap_urls[0]
    except Exception:
        pass

    # Strategy 2: Extract locale from careers_url if provided
    if careers_url:
        # Parse the URL to extract the locale path (e.g., /ca/en, /global/en, /en-us)
        from urllib.parse import urlparse

        parsed = urlparse(careers_url)
        path_parts = [p for p in parsed.path.split("/") if p]
        # Check if the path looks like a locale (2-3 parts like ca/en, global/en, en-us)
        if len(path_parts) >= 2:
            # Try the locale from careers_url
            locale_path = "/".join(path_parts[:2])
            candidate = f"{base_url}/{locale_path}/sitemap_index.xml"
            try:
                resp = get_session().head(candidate, timeout=5, allow_redirects=True)
                if resp.status_code == 200:
                    return candidate
            except Exception:
                pass

    # Strategy 3: Try minimal common paths as last resort
    common_paths = [
        "/us/en/sitemap_index.xml",
        "/sitemap_index.xml",
    ]

    for path in common_paths:
        candidate = f"{base_url}{path}"
        try:
            resp = get_session().head(candidate, timeout=5, allow_redirects=True)
            if resp.status_code == 200:
                return candidate
        except Exception:
            continue

    # Fall back to default
    return f"{base_url}/us/en/sitemap_index.xml"


def _extract_sitemap_urls(html: str) -> list[str]:
    """Extract sitemap URLs from a sitemap index."""
    soup = BeautifulSoup(html, "xml")
    sitemaps = []
    for loc in soup.find_all("loc"):
        url = loc.get_text(strip=True)
        if url and "sitemap" in url.lower():
            sitemaps.append(url)
    return sitemaps


def _extract_job_urls(html: str) -> list[str]:
    """Extract job URLs from a sitemap."""
    soup = BeautifulSoup(html, "xml")
    job_urls = []
    for loc in soup.find_all("loc"):
        url = loc.get_text(strip=True)
        if url and "/job/" in url:
            job_urls.append(url)
    return job_urls


def _extract_job_id(url: str) -> str | None:
    """Extract job ID from a Phenom job URL."""
    match = _JOB_URL_RE.search(url)
    return match.group(1) if match else None


def _extract_title_from_url(url: str) -> str:
    """Extract a candidate title from the Phenom job URL slug.

    Phenom URLs follow the pattern: /{locale...}/job/{job_id}/{title_slug}
    The slug contains a hyphen-separated title (e.g., "Human-Resources-Business-Partner").
    This is a cheap pre-filter before the expensive Playwright detail fetch.

    This is shape-independent: it locates the /job/ segment and extracts the
    title slug after the job ID, regardless of how many locale segments precede it.
    """
    # Find the /job/ segment and extract the title slug after the job ID
    parts = url.rstrip("/").split("/")
    try:
        job_index = parts.index("job")
        # URL pattern: .../job/{job_id}/{title_slug}
        # We need at least 2 segments after /job/: job_id and title_slug
        if len(parts) > job_index + 2:
            slug = parts[job_index + 2]  # title_slug is after job_id
            # If the slug is just a number (job ID with no title slug), return empty
            if slug.isdigit():
                return ""
            # Convert hyphens to spaces and capitalize words
            title = " ".join(word.capitalize() for word in slug.split("-"))
            return title
    except (ValueError, IndexError):
        pass
    return ""


def fetch_postings(
    browser, slug: str, *, careers_url: str | None = None, max_load_more: int = 0
) -> list[dict]:
    """Fetch Phenom job postings via sitemap (no Playwright detail fetches).

    The scanner:
    1. Fetches the discovered sitemap (index or flat urlset)
    2. If it's a sitemap index, recurses into sub-sitemaps
    3. If it's a flat urlset, extracts job URLs directly
    4. Derives candidate titles from URL slugs (cheap)
    5. Returns jobs with URL-derived data only (no Playwright fetches)

    Full detail (jd_full) is backfilled later by enrichment for jobs that pass
    the title gate. This avoids expensive Playwright fetches for jobs that would
    be filtered out anyway.

    Args:
        browser: Playwright Browser instance (unused, kept for contract compatibility).
        slug: Phenom careers host (e.g. "careers.conduent.com").
        careers_url: Optional full careers URL for locale discovery.
        max_load_more: Ignored (Phenom uses sitemap pagination, not load-more clicks).

    Returns:
        Raw posting dicts with URL-derived titles. Empty on fetch error or no postings.
    """
    try:
        # Step 1: Fetch sitemap via requests (faster than Playwright)
        sitemap_url = _sitemap_index_url(slug, careers_url)
        try:
            resp = get_session().get(sitemap_url, timeout=10)
            if resp.status_code != 200:
                if resp.status_code in _auth_block_statuses():
                    logger.warning(
                        "scan_phenom('%s') possible auth/anti-bot wall: HTTP %d",
                        slug,
                        resp.status_code,
                    )
                else:
                    logger.debug(
                        "scan_phenom('%s'): sitemap returned HTTP %d", slug, resp.status_code
                    )
                return []
        except Exception as exc:
            logger.debug("scan_phenom('%s'): sitemap fetch failed: %s", slug, exc)
            return []

        # Step 2: Detect sitemap type and handle both index and flat urlset
        # Collect all <loc> entries from the fetched document
        soup = BeautifulSoup(resp.text, "xml")
        all_locs = [loc.get_text(strip=True) for loc in soup.find_all("loc")]

        # Partition into sub-sitemaps vs job URLs
        sub_sitemap_urls = [url for url in all_locs if "sitemap" in url.lower()]
        direct_job_urls = [url for url in all_locs if "/job/" in url]

        job_urls = []

        # Case A: Sitemap index - recurse into sub-sitemaps
        if sub_sitemap_urls:
            for sitemap_url in sub_sitemap_urls:
                try:
                    resp = get_session().get(sitemap_url, timeout=10)
                    if resp.status_code == 200:
                        urls = _extract_job_urls(resp.text)
                        job_urls.extend(urls)
                except Exception as exc:
                    logger.debug(
                        "scan_phenom('%s'): sub-sitemap fetch failed for %s: %s",
                        slug,
                        sitemap_url,
                        exc,
                    )
                    continue

        # Case B: Flat urlset - use job URLs directly
        if direct_job_urls:
            job_urls.extend(direct_job_urls)

        if not job_urls:
            logger.debug("scan_phenom('%s'): no job URLs found", slug)
            return []

        # Step 3: Build postings from URL-derived data (no Playwright fetches)
        # The title gate is applied by the orchestrator, so we return all jobs.
        # Enrichment will backfill jd_full for jobs that pass the title gate.
        postings = []
        for job_url in job_urls:
            job_id = _extract_job_id(job_url)
            if not job_id:
                continue

            # Derive title from URL slug (cheap pre-filter)
            title = _extract_title_from_url(job_url)

            # Apply title isolation to remove location/date glued to title
            if title:
                title = clean_title(title)

            if not title:
                continue

            postings.append(
                {
                    "title": title,
                    "source_url": job_url,
                    "source_id": job_id,
                    "location": "",  # Not available from URL slug
                    "description": "",  # Will be backfilled by enrichment
                }
            )

        logger.debug(
            "scan_phenom('%s'): extracted %d job URLs from sitemaps",
            slug,
            len(postings),
        )
        return postings
    except Exception as exc:
        logger.debug("scan_phenom('%s') failed: %s", slug, exc)
        return []


def _posting_to_job(posting: dict, slug: str) -> dict:
    """Map one raw Phenom posting to the canonical job dict."""
    return {
        "title": posting.get("title", ""),
        "company_source": _COMPANY_SOURCE,
        "location": posting.get("location") or "",
        "locations_structured": [],
        "description": posting.get("description", ""),
        "jd_full": posting.get("description", ""),
        "source_url": posting.get("source_url") or "",
        "source_id": posting.get("source_id") or None,
        "posted_date": None,
        "posted_date_precision": None,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlaywrightPlatformScanner(
    name="phenom",
    company_source=_COMPANY_SOURCE,
    fetch_postings=fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)

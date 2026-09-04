# PORTED from job_finder/web/careers_scraper.py @ c2793bb9d3d6e10b9e99958d9a0a05a33e66f2c1 (private job-cannon). Ledger L-0167.
"""HTML careers page scraper for companies without recognized ATS platforms.

Provides:
- find_careers_url: detect careers page URL from company homepage HTML
- scrape_careers_page: extract keyword-matched job listings from static HTML

Wired to the engine's callers via the ScanServices seam
(``svc.find_careers_url`` / ``svc.scrape_careers_page``) -- see
``jobcannon.engine.ats_scanner._run_html`` and
``jobcannon.engine.expiry_checker`` for the two existing engine-side
consumers, and ``jobcannon/engine/services.py`` for the field contract.

Architecture:
- Static HTML only -- JS-rendered pages return empty list (expected limitation)
- Uses _title_matches from ats_platforms for keyword filtering (shared utility)
- Research Pitfall 6: After fetching, check r.url for ATS domain redirect before scraping

ATS URL redirect detection (Research Pitfall 6):
- If homepage redirects to jobs.lever.co, boards.greenhouse.io, or jobs.ashbyhq.com,
  return None and let caller extract slug from r.url instead of scraping HTML.

# PORT-SEAM: the call_model-touching low-tier fallback (quick-tier URL /
# job-listing extraction) is split into ``_scraper_extract.py`` (design note
# PR-4 "call_model threading"). ``call_model`` is threaded through
# find_careers_url / scrape_careers_page as an OPTIONAL keyword-only
# parameter, mirroring the existing conn/config optionality in this module:
# the low-tier fallback only fires when conn, config, AND call_model are all
# supplied.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT
from jobcannon.engine._scraper_extract import (
    _extract_jobs_with_low_tier,
    _fetch_job_description,
    _find_careers_url_with_low_tier,
)
from jobcannon.engine.ats_platforms import _title_matches
from jobcannon.engine.ats_registry import REDIRECT_DOMAINS
from jobcannon.engine.careers_crawler._title_filters import clean_title
from jobcannon.engine.domain_policy import is_aggregator_or_job_board
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.source_registry import is_opaque_redirect_host

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CAREERS_PATTERNS = ["/careers", "/jobs", "/join", "/join-us", "/work-with-us", "/openings"]

# Aggregator / blog-repost registrable domains that masquerade as employer
# careers pages. Jobs scraped from these carry the BLOG's brand as the company
# ("Jobflarely" <- jobflarely.liveblog365.com) and recycle reposts whose cards
# glue title + date + "View Job ->" CTA together. We never scrape them -- a code
# blocklist is the durable backstop (config-yaml denylists rot + drift, per the
# dual-copy CI test). Suffix-matched against the URL host, so any subdomain is
# covered. Extend this list, not config, when a new repost host surfaces.
_BLOCKLISTED_SCRAPE_HOSTS: frozenset[str] = frozenset(
    {
        "liveblog365.com",
        "nerdleveltech.com",
        "tryapplynow.com",
    }
)

_JD_DELAY = 1.0  # seconds between job page fetches (rate limiting)

# Class names that suggest a child element contains a location (city/region)
_LOCATION_CLASSES = {"location", "city", "geo", "place", "region", "department-location"}

# Subdomains that indicate a careers site (checked after ATS exclusion)
_CAREERS_SUBDOMAINS = ("careers.", "jobs.", "work.", "apply.")


def _host_matches_any(host: str | None, domains) -> bool:
    """True iff *host* IS one of *domains* or a subdomain of one (label-boundary match).

    Boundary-anchored on purpose: a bare substring test (``domain in netloc``) is
    the ``py/incomplete-url-substring-sanitization`` anti-pattern -- it matches a
    domain appearing in a path/query or a look-alike host
    (``boards.greenhouse.io.evil.com``, ``notboards.greenhouse.io``).
    """
    if not host:
        return False
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _is_blocklisted_scrape_host(url: str) -> bool:
    """True if *url*'s host is (a subdomain of) a blocklisted aggregator domain."""
    if not url:
        return False
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    return _host_matches_any(host, _BLOCKLISTED_SCRAPE_HOSTS)


def _is_ats_redirect_host(host: str | None) -> bool:
    """True iff *host* is (a subdomain of) a known ATS redirect domain
    (REDIRECT_DOMAINS, e.g. ``jobs.lever.co``, ``boards.greenhouse.io``).

    Host-boundary matched against the URL's PARSED hostname rather than a
    substring of ``netloc``, so a look-alike host or a domain embedded
    elsewhere in the URL cannot be misdetected as an ATS redirect (Research
    Pitfall 6's exclusion check)."""
    return _host_matches_any(host, REDIRECT_DOMAINS)


def _is_disqualified_careers_host(url: str | None, config: dict | None = None) -> bool:
    """True if *url* points at a third-party aggregator / job board / opaque-
    redirect host that must never be persisted as a company's careers_url.

    A company homepage routinely footer-links its OWN LinkedIn / Glassdoor /
    BuiltIn / Indeed "jobs" page, and those URLs carry a careers pattern
    (``/jobs``) in the path. Returning one from find_careers_url writes a
    multi-employer listing host into ``companies.careers_url`` -- the role.com
    aggregator-pollution class -- which then misdirects the careers crawler, ATS
    discovery, and the Apply-button target alike. This is the single negative
    gate every find_careers_url result is filtered through (both write paths,
    homepage_discoverer and enrichment_tiers, persist that return value
    verbatim, so gating here protects every downstream consumer at once).

    Composes the codebase's EXISTING host predicates -- no new hardcoded list:
      * domain_policy.is_aggregator_or_job_board -- BLOCKED_DOMAINS (glassdoor,
        indeed, ziprecruiter, dice, role.com, ...) plus the non-ATS job boards
        (linkedin, builtin, ...) that is_blocked_domain deliberately omits.
      * source_registry.is_opaque_redirect_host -- the config-driven republisher
        registry (jooble, adzuna, ...). Only fires when a config is available;
        find_careers_url's two write-path callers pass none, in which case the
        aggregator/job-board check alone already covers every named host.
    """
    if not url:
        return False
    if is_aggregator_or_job_board(url):
        return True
    try:
        host = urlparse(url).hostname
    except (ValueError, AttributeError):
        return False
    return is_opaque_redirect_host(host, config)


def _is_careers_subdomain(host: str | None) -> bool:
    """True iff *host* starts with a careers-indicating subdomain label
    (``_CAREERS_SUBDOMAINS`` -- e.g. ``careers.``, ``jobs.``).

    Checked against the PARSED hostname (which strips userinfo/port), not
    ``netloc`` directly -- ``netloc`` can carry a ``user:pass@`` prefix that
    would make a prefix check on it misreport the real host."""
    if not host:
        return False
    return host.startswith(_CAREERS_SUBDOMAINS)


def _extract_base_domain(url: str) -> str | None:
    """Extract registrable domain from URL, stripping www. prefix.

    Returns e.g. 'google.com' from 'https://www.google.com/'.
    """
    netloc = urlparse(url).netloc
    if not netloc:
        return None
    return netloc.removeprefix("www.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_careers_url(
    homepage_url: str,
    conn: sqlite3.Connection | None = None,
    config: dict | None = None,
    *,
    call_model: Callable[..., Any] | None = None,
) -> str | None:
    """Detect a company's careers page URL from its homepage.

    Thin enforcement wrapper over _find_careers_url_raw: whatever discovery
    branch produced the candidate (redirect landing, meta-refresh, <a> scan,
    proactive subdomain probe, or quick-tier fallback), the result is passed
    through _is_disqualified_careers_host, so an aggregator / job board /
    opaque-redirect host can never be surfaced -- and therefore never persisted
    into companies.careers_url by either write path (homepage_discoverer,
    enrichment_tiers). THE single point of enforcement for that invariant; do
    not scatter the check across the individual branches or the write sites.

    Also rejects _BLOCKLISTED_SCRAPE_HOSTS (#1622): a blocklisted aggregator
    domain (tryapplynow.com, liveblog365.com, ...) must never be persisted as
    a company's careers_url, or a recreated company row resurrects the
    aggregator. The scrape-time gates in scrape_careers_page and the ATS
    HTML-fallback scan, plus the careers_crawler per-company boundary, are
    the load-bearing enforcement; rejecting at discovery prevents the
    polluted row from being written at all.

    Args:
        call_model: Optional injected model-dispatch callable. When conn,
            config, AND call_model are all supplied, a quick-tier fallback
            analyzes the truncated homepage HTML if heuristic link-finding
            fails (see _scraper_extract._find_careers_url_with_low_tier).

    See _find_careers_url_raw for the discovery-strategy details and args.
    """
    candidate = _find_careers_url_raw(homepage_url, conn=conn, config=config, call_model=call_model)
    if candidate and _is_disqualified_careers_host(candidate, config):
        logger.debug(
            "find_careers_url('%s'): rejecting aggregator/job-board careers host %s",
            homepage_url,
            candidate,
        )
        return None
    if candidate and _is_blocklisted_scrape_host(candidate):
        logger.debug(
            "find_careers_url('%s'): rejecting blocklisted scrape host %s",
            homepage_url,
            candidate,
        )
        return None
    return candidate


def _find_careers_url_raw(
    homepage_url: str,
    conn: sqlite3.Connection | None = None,
    config: dict | None = None,
    *,
    call_model: Callable[..., Any] | None = None,
) -> str | None:
    """Detect careers page URL from company homepage (pre-filter).

    Fetches homepage with requests.get and searches for links matching
    known careers URL patterns (/careers, /jobs, /join, etc.).

    NOTE: the public entry point is find_careers_url, which additionally
    rejects aggregator / job-board / opaque-redirect hosts. This raw form may
    still emit such a host from a non-loop branch (redirect / meta-refresh /
    quick-tier fallback); callers must go through find_careers_url.

    IMPORTANT (Research Pitfall 6): Checks the final URL after redirect.
    If the homepage redirects to an ATS domain (Lever, Greenhouse, Ashby),
    returns None so caller can extract slug from the redirect URL instead.

    When heuristic link-finding returns nothing AND conn/config/call_model
    are all provided, falls back to a quick-tier model analysis of the
    truncated homepage HTML.

    Args:
        homepage_url: Company homepage URL to scan.
        conn: Optional SQLite connection for cost recording (quick-tier fallback).
        config: Optional application config dict (quick-tier fallback).
        call_model: Optional injected model-dispatch callable (quick-tier fallback).

    Returns:
        Absolute URL to the careers page, or None if not found / ATS redirect.
    """
    try:
        resp = fetch_with_deadline(homepage_url, timeout=_TIMEOUT, headers=_HEADERS)
    except Exception as e:
        logger.debug("find_careers_url('%s') request failed: %s", homepage_url, e)
        return None

    # Research Pitfall 6: check final URL for ATS redirect
    final_url = resp.url
    parsed = urlparse(final_url)
    if _is_ats_redirect_host(parsed.hostname):
        logger.debug(
            "find_careers_url('%s'): redirected to ATS domain '%s' -- returning None",
            homepage_url,
            parsed.netloc,
        )
        return None

    # Check if HTTP redirect landed on a careers/jobs/work subdomain
    if _is_careers_subdomain(parsed.hostname):
        logger.debug(
            "find_careers_url('%s'): redirected to careers subdomain '%s'",
            homepage_url,
            final_url,
        )
        return final_url

    # Parse homepage HTML for careers links
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.debug("find_careers_url('%s') HTML parse error: %s", homepage_url, e)
        return None

    # Detect <meta http-equiv="refresh"> redirects to careers URLs
    meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)})
    if meta_refresh:
        content = meta_refresh.get("content", "")
        # Extract URL from content like "0; url=https://..." or "0;url=/careers"
        match = re.search(r"url\s*=\s*(.+)", content, re.I)
        if match:
            refresh_url = match.group(1).strip().strip("'\"")
            # Resolve relative URL
            refresh_url = urljoin(homepage_url, refresh_url)
            refresh_parsed = urlparse(refresh_url)
            # Check for ATS domain -- don't follow
            if _is_ats_redirect_host(refresh_parsed.hostname):
                logger.debug(
                    "find_careers_url('%s'): meta-refresh to ATS domain '%s' -- returning None",
                    homepage_url,
                    refresh_parsed.netloc,
                )
                return None
            # Check if refresh target is a careers subdomain or careers path
            if _is_careers_subdomain(refresh_parsed.hostname):
                logger.debug(
                    "find_careers_url('%s'): meta-refresh to careers subdomain '%s'",
                    homepage_url,
                    refresh_url,
                )
                return refresh_url
            if any(pattern in refresh_parsed.path for pattern in _CAREERS_PATTERNS):
                logger.debug(
                    "find_careers_url('%s'): meta-refresh to careers path '%s'",
                    homepage_url,
                    refresh_url,
                )
                return refresh_url

    # Search all <a href="..."> for careers-pattern matches
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue

        # Skip links to a third-party aggregator / job board / opaque-redirect
        # host and KEEP scanning -- a company's own LinkedIn/Glassdoor/BuiltIn
        # "jobs" page carries a careers pattern in its path and would otherwise
        # be returned as careers_url, but a later <a> may be the real employer
        # careers link. Only absolute hrefs can escape the homepage's own host;
        # relative hrefs resolve against homepage_url below, so they're safe.
        if href.lower().startswith("http") and _is_disqualified_careers_host(href, config):
            continue

        # Check absolute URLs pointing to careers subdomains
        if href.lower().startswith("http"):
            href_parsed = urlparse(href)
            if _is_careers_subdomain(href_parsed.hostname):
                # Verify it's not an ATS domain
                if not _is_ats_redirect_host(href_parsed.hostname):
                    logger.debug(
                        "find_careers_url('%s'): found link to careers subdomain '%s'",
                        homepage_url,
                        href,
                    )
                    return href

        # Check if href matches any careers pattern
        href_lower = href.lower()
        for pattern in _CAREERS_PATTERNS:
            # Match: href starts with pattern OR contains the pattern as a path segment
            if (
                href_lower == pattern
                or href_lower.startswith(pattern + "/")
                or href_lower.startswith(pattern + "?")
            ):
                # Resolve relative URL to absolute
                absolute_url = urljoin(homepage_url, href)
                logger.debug(
                    "find_careers_url('%s'): found careers link '%s'",
                    homepage_url,
                    absolute_url,
                )
                return absolute_url

            # Also match absolute URLs that contain the pattern in path
            if href_lower.startswith("http") and pattern in urlparse(href_lower).path:
                logger.debug(
                    "find_careers_url('%s'): found absolute careers link '%s'",
                    homepage_url,
                    href,
                )
                return href

    logger.debug("find_careers_url('%s'): no careers link found in HTML", homepage_url)

    # Proactive subdomain probe: try careers.{domain}, jobs.{domain}, etc.
    base_domain = _extract_base_domain(homepage_url)
    if base_domain:
        for prefix in _CAREERS_SUBDOMAINS:
            candidate = f"https://{prefix}{base_domain}/"
            try:
                probe = fetch_with_deadline(
                    candidate,
                    total_deadline_s=5.0,
                    getter=requests.head,
                    timeout=3,
                    headers=_HEADERS,
                    allow_redirects=True,
                )
                if probe.status_code >= 400:
                    continue
                final = urlparse(probe.url)
                if _is_ats_redirect_host(final.hostname):
                    continue
                # Validate final URL still looks like a careers page --
                # reject if redirect bounced back to main site
                if _is_careers_subdomain(final.hostname):
                    logger.debug(
                        "find_careers_url('%s'): subdomain probe hit '%s'",
                        homepage_url,
                        probe.url,
                    )
                    return probe.url
                if any(p in final.path for p in _CAREERS_PATTERNS):
                    logger.debug(
                        "find_careers_url('%s'): subdomain probe hit '%s' (path match)",
                        homepage_url,
                        probe.url,
                    )
                    return probe.url
            except Exception:
                continue

    # low-tier fallback: if heuristic found nothing and a call_model is available
    if conn is not None and config is not None and call_model is not None:
        logger.debug("find_careers_url('%s'): trying low-tier fallback", homepage_url)
        return _find_careers_url_with_low_tier(
            homepage_url, resp.text, conn, config, call_model=call_model
        )

    return None


def scrape_careers_page(
    careers_url: str,
    target_titles: list[str],
    exclusions: list[str],
    conn: sqlite3.Connection | None = None,
    config: dict | None = None,
    *,
    call_model: Callable[..., Any] | None = None,
) -> tuple[list[dict], int]:
    """Extract keyword-matched job listings from a static careers page.

    Fetches the careers page and looks for <a> tags whose text matches
    target_titles (using _title_matches from ats_platforms). This approach
    only works on static HTML pages -- JavaScript-rendered pages will return
    an empty list (expected limitation documented in Research).

    For each matched job, follows the job URL to fetch the full job description
    text (rate-limited at _JD_DELAY seconds between fetches). Auth-wall pages
    return empty description. Descriptions capped at svc.jd_storage_max_chars.

    When HTML parsing finds 0 matching jobs AND conn/config/call_model are
    provided, falls back to a quick-tier model extraction via
    _extract_jobs_with_low_tier.

    Args:
        careers_url: URL of the careers page to scrape.
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.
        conn: Optional SQLite connection for cost recording (quick-tier fallback).
        config: Optional application config dict (quick-tier fallback).
        call_model: Optional injected model-dispatch callable (quick-tier fallback).

    Returns:
        Tuple of (matched_job_dicts, skipped_count) where skipped_count is
        the number of job links filtered by title exclusions. Empty list on
        error or if no matching jobs found (including JS-rendered pages).
    """
    # Aggregator/blog repost hosts produce brand-as-company junk -- never scrape
    # them. Checked on the requested URL up front (cheap, no fetch).
    if _is_blocklisted_scrape_host(careers_url):
        logger.debug("scrape_careers_page: skipping blocklisted aggregator host %s", careers_url)
        return [], 0

    try:
        resp = fetch_with_deadline(careers_url, timeout=_TIMEOUT, headers=_HEADERS)
    except Exception as e:
        logger.debug("scrape_careers_page('%s') request failed: %s", careers_url, e)
        return [], 0

    # Re-check after redirects: a benign-looking URL may 30x to a repost host.
    if _is_blocklisted_scrape_host(resp.url):
        logger.debug(
            "scrape_careers_page: host %s redirected to blocklisted aggregator", careers_url
        )
        return [], 0

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        logger.debug("scrape_careers_page('%s') HTML parse error: %s", careers_url, e)
        return [], 0

    results = []
    seen_urls = set()
    skipped_count = 0

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()

        # Whitespace-normalize the card text (join adjacent text nodes with a
        # space so the Blue State shape "Principal Analyst (Evergreen)NY, DC" is
        # split, and the sibling <span class="location"> stays extractable below),
        # THEN run the string-level title repair. clean_title strips the trailing
        # date/CTA card chrome ("Data Scientist / IA Engineer Jun 15, 2026 View
        # Job ->" -> "Data Scientist / IA Engineer") so both the relevance match
        # and the persisted value are the clean title. We deliberately use the
        # string variant, NOT the tag-aware _clean_title: its heading/first-child
        # strategies would grab the location <span> as the title on this markup.
        # ParsedJob.from_job re-runs the same contract at the universal chokepoint.
        raw_title = " ".join(tag.stripped_strings)

        # Skip empty links, navigation-only links without text
        if not href or not raw_title:
            continue

        # Apply the keyword/exclusion filter on the RAW card text: an exclusion
        # keyword (e.g. "Intern") must match the original title even if cleaning
        # would later strip it as a trailing qualifier. (Running the filter on the
        # cleaned title let excluded jobs leak through once "- Intern" was removed.)
        if not _title_matches(raw_title, target_titles, exclusions):
            skipped_count += 1
            continue

        # Persist the CLEANED title -- strips the trailing date/CTA card chrome.
        title = clean_title(raw_title)
        if not title:
            continue

        # Resolve relative URL
        absolute_url = urljoin(careers_url, href)

        # Deduplicate by URL
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)

        # Extract location from the same DOM area as the title.
        # Priority 1: child element with a location-indicative class or <small>.
        # Priority 2: sibling text/elements in the parent container.
        location = ""
        loc_tag = tag.find(
            lambda t: (
                t.name in ("span", "small", "div", "p", "em", "strong")
                and bool(set(t.get("class") or []) & _LOCATION_CLASSES)
            )
        )
        if loc_tag:
            location = loc_tag.get_text(strip=True)
        else:
            parent = tag.parent
            if parent is not None:
                sibling_texts = []
                for child in parent.children:
                    if child is tag:
                        continue
                    if hasattr(child, "get_text"):
                        text = child.get_text(strip=True)
                        if text:
                            sibling_texts.append(text)
                    else:
                        text = str(child).strip()
                        if text:
                            sibling_texts.append(text)
                location = " ".join(sibling_texts)

        results.append(
            {
                "title": title,
                "url": absolute_url,
                "location": location,
            }
        )

    logger.debug(
        "scrape_careers_page('%s'): %d matching jobs found, %d skipped by title filter",
        careers_url,
        len(results),
        skipped_count,
    )

    # Fetch full JD for each matched job (rate-limited)
    for i, job in enumerate(results):
        if job.get("url"):
            job["description"] = _fetch_job_description(job["url"])
            if i < len(results) - 1:  # No delay after last job
                time.sleep(_JD_DELAY)
        else:
            job["description"] = ""

    # low-tier fallback when HTML parsing found no matching jobs
    if not results and conn is not None and config is not None and call_model is not None:
        logger.debug("scrape_careers_page('%s'): trying low-tier fallback", careers_url)
        results = _extract_jobs_with_low_tier(
            careers_url, resp.text, target_titles, exclusions, conn, config, call_model=call_model
        )

    return results, skipped_count

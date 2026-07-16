"""iCIMS platform scanner — Playwright-based (JS-rendered, no public API).

iCIMS-hosted career portals are 100% JS-rendered with no public
unauthenticated JSON endpoint, so they cannot ride the requests-only
``PlatformScanner`` registry (whose ``fetch_postings`` is typed
``slug -> list`` with no room for a ``Browser`` argument). This module
defines a **parallel** value object — ``PlaywrightPlatformScanner`` — whose
``fetch_postings`` takes the Playwright ``Browser`` as its explicit first
parameter, mirroring ``careers_crawler/_playwright_tier.py``. The browser's
lifetime is owned by the orchestrator (the ``sync_playwright()`` block in
``ats_scanner/_run_playwright.py``), not by this module.

The render path is deliberately thin: navigate to the tenant's
``/jobs/search`` board, settle the JS, then DOM-extract anchors that match
the canonical iCIMS job-detail href shape (``/jobs/{id}/{slug}/job``).
Descriptions are NOT pulled here — the list page exposes only titles +
locations, so ``jd_full`` is filled asynchronously by enrichment, the same
deferral pattern as the Breezy / Rippling / BambooHR registry scanners.

iCIMS has no public API; this is the locked rationale (issue #454). Do not
reverse-engineer a JSON endpoint as the primary path.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_COMPANY_SOURCE = "iCIMS"

# Page-render timing, mirrored from careers_crawler/_playwright_tier.py so the
# two Playwright paths settle JS the same way.
_PLAYWRIGHT_TIMEOUT_MS = 15000  # Page load timeout
_JS_SETTLE_MS = 2000  # Wait for JS to finish rendering

# Default load-more click budget — overridable per-deployment via
# ``config.ats.icims_max_load_more_clicks`` (threaded in by the orchestrator).
_DEFAULT_MAX_LOAD_MORE = 5

# Canonical iCIMS job-detail href shape: ``/jobs/{numeric-id}/{slug}/job``.
# Robust across the classic ``iCIMS_JobsTable`` markup and the newer React
# portal because both link each posting through this same path.
_JOB_HREF_RE = re.compile(r"/jobs/(\d+)/[^/?#]+/job", re.IGNORECASE)

# Class fragment used to locate a posting's location text near its anchor.
_LOC_CLASS_RE = re.compile(r"location", re.IGNORECASE)

# "Load more" control on the search-results board. ``query_selector`` returns
# ``None`` when absent, which cleanly terminates the pagination loop.
_LOAD_MORE_SELECTOR = "a.iCIMS_LoadMoreJobs, button.iCIMS_LoadMoreJobs, .iCIMS_MoreLink a"


@dataclass(frozen=True, slots=True)
class PlaywrightPlatformScanner:
    """Per-platform contract for the Playwright-class scan driver.

    Parallel architecture to ``_registry.PlatformScanner`` — the distinction
    is ``fetch_postings`` takes the Playwright ``Browser`` as an explicit
    first parameter (no requests-only ``slug -> list`` contract). The driver
    (``ats_scanner/_run_playwright.run_playwright_platform_scan``) owns the
    title-match gate and the result-count log line; the orchestrator owns the
    ``sync_playwright()`` lifecycle.

    Attributes:
        name: Lowercase platform key matching ``companies.ats_platform``
            (``"icims"``). Used in log messages.
        company_source: Display-cased platform name written into the
            ``company_source`` field of each job dict (``"iCIMS"``).
        fetch_postings: ``(browser, slug, *, max_load_more) -> list[dict]``.
            Owns the page render + pagination/load-more + DOM extraction.
            Must catch its own exceptions and return ``[]`` on any error so
            one tenant's render failure cannot crash a whole batch.
        title_of: ``posting -> str``. Pulls the title out of one raw posting
            for the title-match gate.
        posting_to_job: ``(posting, slug) -> dict | None``. Builds the
            canonical job dict for one posting; ``None`` skips it.
    """

    name: str
    company_source: str
    fetch_postings: Callable[..., list[dict]]
    title_of: Callable[[dict], str]
    posting_to_job: Callable[[dict, str], dict | None]


def _board_url(slug: str) -> str:
    """Build the iCIMS search-results board URL for a tenant slug.

    ``slug`` is normally the bare tenant subdomain (e.g. ``"acme"``), which
    resolves to ``https://careers-acme.icims.com/jobs/search``. Tenants
    served on the ``jobs-`` host (or any explicit host) can store the full
    host as the slug — if the slug already contains ``icims.com`` it is used
    verbatim rather than wrapped in the ``careers-`` prefix.
    """
    s = slug.strip()
    if "icims.com" in s:
        host = s.replace("https://", "").replace("http://", "").split("/")[0]
        return f"https://{host}/jobs/search?ss=1"
    return f"https://careers-{s}.icims.com/jobs/search?ss=1"


def _extract_location(anchor) -> str:
    """Best-effort location string from elements near a job anchor.

    Walks up to four ancestors looking for a descendant whose class hints at
    a location (``iCIMS_JobLocation``, ``location``, …). Returns ``""`` when
    no location element is found — the raw string is unstructured
    (``"US-CA-San Francisco"``) and structuring is left to downstream
    enrichment.
    """
    node = anchor
    for _ in range(4):
        node = getattr(node, "parent", None)
        if node is None or not hasattr(node, "find"):
            break
        loc = node.find(class_=_LOC_CLASS_RE)
        if loc is not None and loc is not anchor:
            text = loc.get_text(" ", strip=True)
            if text:
                return text
    return ""


def _extract_postings(html: str, base_url: str) -> list[dict]:
    """Extract raw iCIMS postings from rendered board HTML.

    Returns a list of raw posting dicts (``title`` / ``source_url`` /
    ``source_id`` / ``location``); ``_posting_to_job`` maps each to the
    canonical job shape. Anchors are de-duplicated by absolute URL so a
    posting linked twice on the page (title + tile) counts once.
    """
    soup = BeautifulSoup(html, "html.parser")
    postings: list[dict] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        match = _JOB_HREF_RE.search(href)
        if not match:
            continue
        title = anchor.get_text(" ", strip=True)
        if not title:
            continue
        source_url = urljoin(base_url, href)
        if source_url in seen:
            continue
        seen.add(source_url)
        postings.append(
            {
                "title": title,
                "source_url": source_url,
                "source_id": match.group(1),
                "location": _extract_location(anchor),
            }
        )
    return postings


def _click_load_more(page) -> bool:
    """Click the board's "load more" control if present. Fully defensive.

    Returns ``True`` when a control was found and clicked (caller should
    re-extract), ``False`` when absent or on any error (caller stops
    paginating). Never raises.
    """
    try:
        element = page.query_selector(_LOAD_MORE_SELECTOR)
        if element is None:
            return False
        element.click()
        return True
    except Exception:
        return False


def _fetch_postings(
    browser, slug: str, *, max_load_more: int = _DEFAULT_MAX_LOAD_MORE
) -> list[dict]:
    """Render the iCIMS board with Playwright + DOM-extract matched postings.

    Owns the full page lifecycle for one tenant: open a page on the
    orchestrator-supplied ``browser``, navigate to the search board, settle
    the JS, extract, then click "load more" up to ``max_load_more`` times
    accumulating new postings. Catches its own exceptions and returns ``[]``
    on any error (same resilience contract as the requests registry).

    Args:
        browser: Playwright ``Browser`` (already launched by the orchestrator).
        slug: Tenant subdomain (or full host — see ``_board_url``).
        max_load_more: Maximum "load more" clicks before stopping.

    Returns:
        Raw posting dicts. Empty on render error or no postings.
    """
    page = None
    try:
        url = _board_url(slug)
        page = browser.new_page()
        page.goto(url, timeout=_PLAYWRIGHT_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(_JS_SETTLE_MS)

        postings = _extract_postings(page.content(), url)
        seen = {p["source_url"] for p in postings}

        clicks = 0
        while clicks < max_load_more:
            if not _click_load_more(page):
                break
            page.wait_for_timeout(_JS_SETTLE_MS)
            clicks += 1
            for posting in _extract_postings(page.content(), url):
                if posting["source_url"] in seen:
                    continue
                seen.add(posting["source_url"])
                postings.append(posting)

        return postings
    except Exception as exc:
        logger.debug("scan_icims('%s') render failed: %s", slug, exc)
        return []
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def _posting_to_job(posting: dict, slug: str) -> dict:
    """Map one raw iCIMS posting to the canonical job dict.

    ``description`` is empty — the board list page exposes only title +
    location, so ``jd_full`` is filled later by enrichment. ``posted_date``
    is ``None``: iCIMS search results do not reliably expose a first-posted
    timestamp, and a wrong date is worse than no date (D-08).
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
    name="icims",
    company_source=_COMPANY_SOURCE,
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)

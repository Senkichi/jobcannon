"""BambooHR platform scanner (registry form).

The historical ``/careers/list`` JSON endpoint was deprecated in 2024 —
every tenant now serves the embedded careers widget at
``/jobs/embed2.php`` as static HTML. Jobs are
``<li class="BambooHR-ATS-Jobs-Item">`` elements with a nested anchor for
title + href and a ``.BambooHR-ATS-Location`` child for location.

The listing does NOT include job descriptions — only titles and
locations are available without a per-job detail fetch (deferred to
enrichment).
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from jobcannon.engine.ats_platforms._http_session import get_session
from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _auth_block_statuses,
)
from jobcannon.engine.ats_prober import _PROBE_TIMEOUT

logger = logging.getLogger(__name__)


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """GET + HTML parse → list of {anchor, location-element} extracts."""
    url = f"https://{slug}.bamboohr.com/jobs/embed2.php"
    try:
        resp = get_session().get(url, timeout=_PROBE_TIMEOUT)
    except Exception as exc:
        logger.warning("scan_bamboohr('%s') request failed: %s", slug, exc)
        return []

    if resp.status_code != 200:
        if resp.status_code in _auth_block_statuses():
            logger.warning(
                "scan_bamboohr('%s') possible auth/anti-bot wall: HTTP %d",
                slug,
                resp.status_code,
            )
        else:
            logger.debug("scan_bamboohr('%s') returned HTTP %d", slug, resp.status_code)
        return []

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        logger.warning("scan_bamboohr('%s') HTML parse error: %s", slug, exc)
        return []

    items = soup.select("li.BambooHR-ATS-Jobs-Item")
    postings: list[dict] = []
    for item in items:
        anchor = item.find("a")
        if anchor is None:
            continue
        location_el = item.find(class_="BambooHR-ATS-Location")
        postings.append(
            {
                "title": anchor.get_text(strip=True),
                "href": anchor.get("href") or "",
                "location": location_el.get_text(strip=True) if location_el else "",
            }
        )
    return postings


def _posting_to_job(posting: dict, slug: str) -> dict:
    href = posting.get("href") or ""
    if isinstance(href, str) and href.startswith("//"):
        href = "https:" + href
    elif isinstance(href, str) and href.startswith("/"):
        href = f"https://{slug}.bamboohr.com{href}"

    return {
        "title": posting.get("title", ""),
        "company_source": "BambooHR",
        "location": posting.get("location", ""),
        # No description in the listing; jd_full will be filled by the
        # enrichment tier when a real first-time hit comes through.
        "description": "",
        "source_url": href if isinstance(href, str) else "",
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="bamboohr",
    company_source="BambooHR",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)

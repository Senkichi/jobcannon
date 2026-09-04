# PORTED from job_finder/web/enrichment_tiers.py @ 0a4c33c5af7cd4055e539672158cb301b7bdc407 (private job-cannon). Ledger L-0178.
"""SerpAPI Google Jobs search + DuckDuckGo Instant Answer search tiers.

Split out of the private ``enrichment_tiers.py`` (design note PR-4). Binds to
``ScanServices.search_serpapi`` / ``ScanServices.search_duckduckgo``.

# PORT-SEAM: sources._error_envelope.VendorAccountError is L-0111 (HOLD, not
# ported). The private search_serpapi raises it on a SerpAPI 429 so the
# error propagates past this function's own except-Exception catch-all,
# letting a caller-level rate-limit handler see it distinctly (svc.
# vendor_account_error, currently unbound, per data_enricher.py's own
# PORT-SEAM comment). This port defines a local, narrowly-scoped substitute
# (_SerpApiRateLimitError) that preserves that "429 propagates" behavior
# without inventing a port of the unlanded module; data_enricher.py's
# `except svc.vendor_account_error or _NoVendorAccountError` clause stays
# dead (as it already is today) until L-0111 lands, and this exception falls
# through to that function's own generic `except Exception` catch instead —
# functionally identical, since that catch-all already treats a 429 as "log
# and move to the next tier" either way.
"""

from __future__ import annotations

import logging
import time

import requests

from jobcannon.engine._enrichment_jd_fetch import fetch_direct_jd
from jobcannon.engine._http_constants import _HEADERS, _REQUEST_TIMEOUT
from jobcannon.engine.domain_policy import domain_priority, is_blocked_domain
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)

# DuckDuckGo Instant Answer API endpoint
_DDG_API_URL = "https://api.duckduckgo.com/"

# SerpAPI Google Jobs endpoint
_SERPAPI_URL = "https://serpapi.com/search.json"

# Bounded retry / deadline constants for outbound search calls (issue #1277).
# Per-call timeouts are the first line of defense; the scheduler's job-level
# max_runtime_s is the architectural backstop for missed call sites.
_SEARCH_MAX_RETRIES = 1
_SEARCH_RETRY_DELAY_S = 1.0


class _SerpApiRateLimitError(Exception):
    """Raised on a SerpAPI 429. See module PORT-SEAM above."""


def search_serpapi(query: str, api_key: str) -> tuple[dict | None, list[str]]:
    """Search Google Jobs via SerpAPI for job details.

    Args:
        query: Search query string (e.g., "Data Scientist Acme Corp").
        api_key: SerpAPI API key.

    Returns:
        2-tuple of (result_dict, apply_urls):
        - result_dict: Dict with job data or None if no results.
        - apply_urls: Filtered and priority-sorted apply option URLs.
    """
    try:
        params = {
            "engine": "google_jobs",
            "q": query,
            "api_key": api_key,
            "num": 1,
        }
        response = None
        for attempt in range(_SEARCH_MAX_RETRIES + 1):
            try:
                response = fetch_with_deadline(
                    _SERPAPI_URL,
                    getter=requests.get,
                    params=params,
                    timeout=_REQUEST_TIMEOUT,
                    headers=_HEADERS,
                )
                if response.status_code == 429:
                    raise _SerpApiRateLimitError("SerpAPI rate limit exceeded (429)")
                response.raise_for_status()
                break
            except requests.exceptions.Timeout as exc:
                if attempt < _SEARCH_MAX_RETRIES:
                    logger.debug(
                        "SerpAPI timeout for '%s' (attempt %d), retrying: %s",
                        query,
                        attempt + 1,
                        exc,
                    )
                    time.sleep(_SEARCH_RETRY_DELAY_S)
                else:
                    logger.debug(
                        "SerpAPI timeout for '%s' (attempt %d): %s",
                        query,
                        attempt + 1,
                        exc,
                    )
                    raise

        data = response.json()
        jobs = data.get("jobs_results", [])
        if not jobs:
            return None, []

        job = jobs[0]
        result = {}

        # Extract job description
        description = job.get("description")
        if description:
            svc = get_services()  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars
            result["jd_full"] = description[: svc.jd_storage_max_chars]

        # Extract location
        location = job.get("location")
        if location:
            result["location"] = location

        # Extract salary from detected_extensions
        extensions = job.get("detected_extensions", {})
        salary_str = extensions.get("salary", "")
        if salary_str:
            salary_range = _parse_salary_string(salary_str)
            if salary_range:
                result.update(salary_range)

        # Extract, filter, and sort apply_options URLs
        apply_options = job.get("apply_options", [])
        apply_urls = [
            opt["link"]
            for opt in apply_options
            if opt.get("link") and not is_blocked_domain(opt["link"])
        ]
        apply_urls.sort(key=domain_priority)

        # Try to fetch JD from ATS apply URLs
        for url in apply_urls:
            try:
                url_jd = fetch_direct_jd(url)
                if url_jd:
                    result["url_jd"] = url_jd
                    break
            except Exception:
                pass

        return (result if result else None), apply_urls

    except _SerpApiRateLimitError:
        raise
    except Exception as e:
        logger.debug("SerpAPI search failed for '%s': %s", query, e)
        return None, []


def search_duckduckgo(query: str) -> str | None:
    """Query DuckDuckGo Instant Answer API for job/company info.

    Args:
        query: Search query string.

    Returns:
        AbstractText content string, or None if no useful content found.
    """
    try:
        params = {
            "q": query,
            "format": "json",
            "no_redirect": "1",
            "no_html": "1",
            "skip_disambig": "1",
        }
        response = None
        for attempt in range(_SEARCH_MAX_RETRIES + 1):
            try:
                response = fetch_with_deadline(
                    _DDG_API_URL,
                    getter=requests.get,
                    params=params,
                    headers=_HEADERS,
                    timeout=_REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                break
            except requests.exceptions.Timeout as exc:
                if attempt < _SEARCH_MAX_RETRIES:
                    logger.debug(
                        "DuckDuckGo Instant Answer timeout for '%s' (attempt %d), retrying: %s",
                        query,
                        attempt + 1,
                        exc,
                    )
                    time.sleep(_SEARCH_RETRY_DELAY_S)
                else:
                    logger.debug(
                        "DuckDuckGo Instant Answer timeout for '%s' (attempt %d): %s",
                        query,
                        attempt + 1,
                        exc,
                    )
                    raise

        data = response.json()

        # Try AbstractText first (most informative)
        abstract = data.get("AbstractText", "")
        if abstract:
            return abstract

        # Fall back to first RelatedTopic text
        topics = data.get("RelatedTopics", [])
        for topic in topics:
            if isinstance(topic, dict) and topic.get("Text"):
                return topic["Text"]

        return None

    except Exception as e:
        logger.debug("DuckDuckGo search failed for '%s': %s", query, e)
        return None


def _parse_salary_string(salary_str: str) -> dict | None:
    """Parse a salary string like '$140K-$180K/yr' into min/max integers.

    P1.2 (D-2): thin wrapper — delegates to ``salary_normalizer.parse_salary_text``
    (single parser) + ``normalize_observation`` (single normalizer) instead of
    duplicating bespoke regex + K/M logic. Hourly/period cues are now captured
    and annualized via the salvage ladder (D-3) rather than silently ignored.
    Implausible values return None (existing behavior).

    Args:
        salary_str: Salary string from SerpAPI detected_extensions.

    Returns:
        Dict with salary_min and/or salary_max as integers, or None if parsing fails.
    """
    from jobcannon.engine.salary_normalizer import normalize_observation, parse_salary_text

    try:
        obs = parse_salary_text(salary_str, provenance="feed_string")
        if obs is None:
            return None
        normalized = normalize_observation(obs)
        if normalized.resolution not in (
            "ok",
            "salvaged_hourly",
            "salvaged_daily",
            "salvaged_weekly",
            "salvaged_monthly",
        ):
            return None
        result: dict = {}
        if normalized.salary_min is not None:
            result["salary_min"] = normalized.salary_min
        if normalized.salary_max is not None:
            result["salary_max"] = normalized.salary_max
        return result if result else None
    except Exception:
        logger.debug("_parse_salary_string failed", exc_info=True)
        return None

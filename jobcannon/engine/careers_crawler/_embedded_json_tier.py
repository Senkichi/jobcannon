# PORTED from job_finder/web/careers_crawler/_embedded_json_tier.py @ 6a2af961fbffb78564ce8783277d916d60ad0906 (private job-cannon). Ledger L-0443 (umbrella -- no ledger row of its own; imported at module scope by careers_crawler/__init__.py, so the package is unlandable without it -- see PR body). No DB access in this module -- no connection_factory seam needed.
"""Embedded JSON extraction tier for the careers crawler.

Tier 2.5: extracts job listings from embedded JSON state in the initial HTML
(__NEXT_DATA__ / window.__NUXT__ / __APOLLO_STATE__ / <script type="application/json">)
BEFORE falling through to the expensive Playwright tier.

Many custom_cms / js_app careers pages ship the full job list as embedded JSON
even when the visible DOM is JS-rendered — extracting it is far cheaper and
more reliable than rendering.

Behavior:
- Parse embedded blobs from <script id="__NEXT_DATA__" type="application/json">,
  window.__NUXT__ = ..., __APOLLO_STATE__, and generic <script type="application/json">
- Generic walker: recursively walk the parsed object for the largest array of
  homogeneous objects whose keys look job-shaped (a title-ish key AND a url/slug-ish
  key, ideally a location-ish key). NO hardcoded per-site paths.
- Reject non-job arrays via title+url key presence gate (not size-based).
- Apply title hygiene via clean_title() so titles match other tiers.
- Return shape matching sibling tiers: list of {"title": str, "url": str, "description": ""}

Defensive: missing/malformed JSON returns None, never raises.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT
from jobcannon.engine.ats_platforms import _title_matches
from jobcannon.engine.careers_crawler._title_filters import (
    _is_listing_tile,
    _is_metadata_blob,
    clean_title,
)
from jobcannon.engine.http_fetch import fetch_with_deadline

logger = logging.getLogger(__name__)

# Script IDs and patterns for common embedded JSON blobs
_NEXT_DATA_ID = "__NEXT_DATA__"
_NUXT_VAR = "__NUXT__"
_APOLLO_STATE_VAR = "__APOLLO_STATE__"

# Key patterns that suggest a job-related field
_TITLE_KEY_PATTERNS = (re.compile(r"title", re.IGNORECASE),)
_URL_KEY_PATTERNS = (
    re.compile(r"url", re.IGNORECASE),
    re.compile(r"slug", re.IGNORECASE),
    re.compile(r"path", re.IGNORECASE),
)
_LOCATION_KEY_PATTERNS = (
    re.compile(r"location", re.IGNORECASE),
    re.compile(r"city", re.IGNORECASE),
    re.compile(r"country", re.IGNORECASE),
)


def _matches_key_pattern(key: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    """Return True if *key* matches any of the given regex patterns."""
    if not isinstance(key, str):
        return False
    return any(p.search(key) for p in patterns)


def _has_job_like_keys(obj: dict) -> bool:
    """Return True if *obj* has keys that look like a job posting.

    Requires at least one title-ish key AND one url/slug-ish key.
    Location-ish key is optional but strengthens the signal.
    """
    if not isinstance(obj, dict):
        return False

    has_title = any(_matches_key_pattern(k, _TITLE_KEY_PATTERNS) for k in obj)
    has_url = any(_matches_key_pattern(k, _URL_KEY_PATTERNS) for k in obj)

    return has_title and has_url


def _is_homogeneous_array(arr: list[Any]) -> bool:
    """Return True if *arr* is a list of dicts with similar key sets.

    Similar means: at least 80% of objects share the same key set (allowing
    for sparse fields like optional location/description).
    """
    if not arr or len(arr) < 2:
        return False

    dicts = [item for item in arr if isinstance(item, dict)]
    if len(dicts) < len(arr) * 0.8:  # At least 80% must be dicts
        return False

    if len(dicts) < 2:
        return False

    # Get the most common key set
    key_sets = [frozenset(d.keys()) for d in dicts]
    from collections import Counter

    most_common_key_set, count = Counter(key_sets).most_common(1)[0]

    # At least 80% of dicts share this key set
    return count / len(dicts) >= 0.8


def _extract_url_from_obj(obj: dict, base_url: str) -> str | None:
    """Extract a URL from a job-like object, resolving relative URLs."""
    for key in obj:
        if _matches_key_pattern(key, _URL_KEY_PATTERNS):
            url = obj[key]
            if isinstance(url, str) and url:
                if url.startswith("/"):
                    return urljoin(base_url, url)
                if url.startswith(("http://", "https://")):
                    return url
    return None


def _extract_title_from_obj(obj: dict) -> str | None:
    """Extract a title from a job-like object."""
    for key in obj:
        if _matches_key_pattern(key, _TITLE_KEY_PATTERNS):
            title = obj[key]
            if isinstance(title, str) and title:
                return title
    return None


def _extract_location_from_obj(obj: dict) -> str | None:
    """Extract a location from a job-like object (optional)."""
    for key in obj:
        if _matches_key_pattern(key, _LOCATION_KEY_PATTERNS):
            loc = obj[key]
            if isinstance(loc, str) and loc:
                return loc
    return None


def _walk_for_job_array(data: Any) -> list[dict] | None:
    """Recursively walk *data* for the largest homogeneous array of job-like objects.

    Returns the largest array (by length) that:
    - Is homogeneous (similar key sets across objects)
    - Contains objects with job-like keys (title + url/slug)

    Returns None if no such array is found.

    Precision-over-recall by design: when a page has several job-like {title, url}
    arrays (e.g. a small featured-jobs list beside a larger nav/category menu),
    this picks the SINGLE largest and discards the rest. That can drop the real
    jobs if a non-job array outsizes them, but that miss is benign — the caller's
    ``if not jobs`` fall-through escalates to the Playwright tier and recovers
    them. The dangerous direction is the opposite: pulling a nav/category array in
    as fake jobs would poison the north-star coverage metric. The largest-array
    heuristic plus the downstream role-aware ``_title_matches`` filter guards that
    direction. Do NOT "fix" this into a union-of-all-arrays: that trades the benign
    false-negative for a false-positive regression the title filter can't fully
    contain.
    """
    if isinstance(data, list):
        # Check if this array itself is a candidate
        if _is_homogeneous_array(data):
            job_like_items = [
                item for item in data if isinstance(item, dict) and _has_job_like_keys(item)
            ]
            if job_like_items:
                # If most items are job-like, this is a strong candidate
                if len(job_like_items) >= len(data) * 0.7:
                    return job_like_items

        # Recurse into array elements
        candidates = []
        for item in data:
            result = _walk_for_job_array(item)
            if result:
                candidates.append(result)

        # Return the largest candidate
        if candidates:
            return max(candidates, key=len)
        return None

    elif isinstance(data, dict):
        # Recurse into dict values
        candidates = []
        for value in data.values():
            result = _walk_for_job_array(value)
            if result:
                candidates.append(result)

        # Return the largest candidate
        if candidates:
            return max(candidates, key=len)
        return None

    return None


def _parse_script_json(script_text: str) -> Any | None:
    """Parse JSON from a script tag's text content."""
    try:
        return json.loads(script_text)
    except (json.JSONDecodeError, TypeError):
        return None


def _extract_nuxt_state(html: str) -> Any | None:
    """Extract __NUXT__ state from HTML (window.__NUXT__ = ...)."""
    # Match window.__NUXT__ = {...} or __NUXT__ = {...}
    pattern = re.compile(r"window\.__NUXT__\s*=\s*({.*?});?\s*</script>", re.DOTALL)
    match = pattern.search(html)
    if match:
        return _parse_script_json(match.group(1))

    # Try without window. prefix
    pattern = re.compile(r"__NUXT__\s*=\s*({.*?});?\s*</script>", re.DOTALL)
    match = pattern.search(html)
    if match:
        return _parse_script_json(match.group(1))

    return None


def _extract_apollo_state(html: str) -> Any | None:
    """Extract __APOLLO_STATE__ from HTML."""
    pattern = re.compile(r"__APOLLO_STATE__\s*=\s*({.*?});?\s*</script>", re.DOTALL)
    match = pattern.search(html)
    if match:
        return _parse_script_json(match.group(1))
    return None


def _try_embedded_json_extract(
    url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict] | None:
    """Extract job listings from embedded JSON in the page's HTML.

    Fetches the page HTML, then tries multiple embedded JSON sources in order:
    1. <script id="__NEXT_DATA__" type="application/json">
    2. window.__NUXT__ = ...
    3. __APOLLO_STATE__ = ...
    4. Generic <script type="application/json"> blobs

    For each source, walks the parsed JSON for the largest homogeneous array
    of job-like objects (title + url/slug keys). Applies title hygiene and
    user's title filter.

    Args:
        url: The careers page URL to fetch and parse.
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        list[dict] — extracted jobs (may be empty)
        None — fetch failed or no embedded JSON found, caller should escalate
    """
    if not url:
        return None

    # Fetch HTML
    try:
        resp = fetch_with_deadline(url, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.debug("embedded_json_tier: fetch failed for '%s': %s", url, e)
        return None

    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict] = []

    # === Source 1: __NEXT_DATA__ ===
    next_data_script = soup.find("script", id=_NEXT_DATA_ID, type="application/json")
    if next_data_script and next_data_script.string:
        data = _parse_script_json(next_data_script.string)
        if data:
            job_array = _walk_for_job_array(data)
            if job_array:
                logger.debug("embedded_json_tier: found jobs in __NEXT_DATA__")
                candidates.extend(job_array)

    # === Source 2: __NUXT__ ===
    if not candidates:
        nuxt_data = _extract_nuxt_state(html)
        if nuxt_data:
            job_array = _walk_for_job_array(nuxt_data)
            if job_array:
                logger.debug("embedded_json_tier: found jobs in __NUXT__")
                candidates.extend(job_array)

    # === Source 3: __APOLLO_STATE__ ===
    if not candidates:
        apollo_data = _extract_apollo_state(html)
        if apollo_data:
            job_array = _walk_for_job_array(apollo_data)
            if job_array:
                logger.debug("embedded_json_tier: found jobs in __APOLLO_STATE__")
                candidates.extend(job_array)

    # === Source 4: generic application/json scripts ===
    if not candidates:
        for script in soup.find_all("script", type="application/json"):
            if script.string:
                data = _parse_script_json(script.string)
                if data:
                    job_array = _walk_for_job_array(data)
                    if job_array:
                        logger.debug("embedded_json_tier: found jobs in generic application/json")
                        candidates.extend(job_array)
                        break  # First match wins

    if not candidates:
        # No embedded JSON found or no job-like arrays
        return None

    # === Normalize candidates to job dicts ===
    results: list[dict] = []
    seen_urls: set[str] = set()

    for obj in candidates:
        if not isinstance(obj, dict):
            continue

        # Extract fields
        title = _extract_title_from_obj(obj)
        job_url = _extract_url_from_obj(obj, url)
        location = _extract_location_from_obj(obj)

        if not title or not job_url:
            continue

        # Apply title hygiene
        cleaned_title = clean_title(title)

        # Guard against metadata blobs and listing tiles
        if _is_metadata_blob(cleaned_title):
            continue
        if _is_listing_tile(cleaned_title):
            continue

        # Apply user's title filter
        if not _title_matches(cleaned_title, target_titles, exclusions):
            continue

        # Dedup by URL
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        # Build result dict
        entry: dict = {"title": cleaned_title, "url": job_url, "description": ""}
        if location:
            entry["location"] = location
        results.append(entry)

    if results:
        logger.debug("embedded_json_tier: extracted %d jobs", len(results))
        return results

    # Embedded JSON found but no jobs matched filters
    return []


def _try_embedded_json_extract_from_html(
    html: str,
    base_url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict] | None:
    """Extract job listings from embedded JSON in pre-fetched HTML.

    Test helper variant that accepts HTML directly instead of fetching.
    Same extraction logic as _try_embedded_json_extract.

    Args:
        html: The HTML content of the careers page.
        base_url: Base URL for resolving relative URLs.
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        list[dict] — extracted jobs (may be empty)
        None — no embedded JSON found or parsing failed
    """
    if not html or not base_url:
        return None

    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict] = []

    # === Source 1: __NEXT_DATA__ ===
    next_data_script = soup.find("script", id=_NEXT_DATA_ID, type="application/json")
    if next_data_script and next_data_script.string:
        data = _parse_script_json(next_data_script.string)
        if data:
            job_array = _walk_for_job_array(data)
            if job_array:
                logger.debug("embedded_json_tier: found jobs in __NEXT_DATA__")
                candidates.extend(job_array)

    # === Source 2: __NUXT__ ===
    if not candidates:
        nuxt_data = _extract_nuxt_state(html)
        if nuxt_data:
            job_array = _walk_for_job_array(nuxt_data)
            if job_array:
                logger.debug("embedded_json_tier: found jobs in __NUXT__")
                candidates.extend(job_array)

    # === Source 3: __APOLLO_STATE__ ===
    if not candidates:
        apollo_data = _extract_apollo_state(html)
        if apollo_data:
            job_array = _walk_for_job_array(apollo_data)
            if job_array:
                logger.debug("embedded_json_tier: found jobs in __APOLLO_STATE__")
                candidates.extend(job_array)

    # === Source 4: generic application/json scripts ===
    if not candidates:
        for script in soup.find_all("script", type="application/json"):
            if script.string:
                data = _parse_script_json(script.string)
                if data:
                    job_array = _walk_for_job_array(data)
                    if job_array:
                        logger.debug("embedded_json_tier: found jobs in generic application/json")
                        candidates.extend(job_array)
                        break  # First match wins

    if not candidates:
        # No embedded JSON found or no job-like arrays
        return None

    # === Normalize candidates to job dicts ===
    results: list[dict] = []
    seen_urls: set[str] = set()

    for obj in candidates:
        if not isinstance(obj, dict):
            continue

        # Extract fields
        title = _extract_title_from_obj(obj)
        job_url = _extract_url_from_obj(obj, base_url)
        location = _extract_location_from_obj(obj)

        if not title or not job_url:
            continue

        # Apply title hygiene
        cleaned_title = clean_title(title)

        # Guard against metadata blobs and listing tiles
        if _is_metadata_blob(cleaned_title):
            continue
        if _is_listing_tile(cleaned_title):
            continue

        # Apply user's title filter
        if not _title_matches(cleaned_title, target_titles, exclusions):
            continue

        # Dedup by URL
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        # Build result dict
        entry: dict = {"title": cleaned_title, "url": job_url, "description": ""}
        if location:
            entry["location"] = location
        results.append(entry)

    if results:
        logger.debug("embedded_json_tier: extracted %d jobs", len(results))
        return results

    # Embedded JSON found but no jobs matched filters
    return []

# PORTED from job_finder/web/careers_crawler/_static_tier.py @ 6a2af961fbffb78564ce8783277d916d60ad0906 (private job-cannon). Ledger L-0443 (umbrella -- no ledger row of its own; carried by L-0464/L-0469 because _cohort_legitimacy.py imports _extract_jsonld_postings/_location_from_jsonld from this module at module scope; see PR body).
"""Static HTML extraction for the careers crawler.

This module owns:
- The shared HTML extractor (`_extract_jobs_from_soup`) used by both the
  static fetch path and the Playwright tiers — it consumes a parsed
  BeautifulSoup tree and returns matched job dicts.
- The recursive JSON-LD walker (`_extract_jsonld_postings`).
- `_location_from_jsonld` — extracts a location string from a schema.org
  JobPosting dict (D-1: lossless, no normalisation beyond whitespace).
- `_location_from_url_slug` — gazetteer-validates a candidate location token
  extracted from the job's URL path (D-3: discard if not resolved).
- The static-tier entry point (`_try_static_extract`) that performs the
  pure-`requests` fetch, parses, and decides whether the result is
  conclusive (jobs found OR genuinely empty static page) or whether the
  caller should escalate to Playwright (page looks JS-heavy).

`_extract_jobs_from_soup` is part of the public surface — it's imported
lazily by `careers_page_interactions.py` and `ai_career_navigator.py` —
so the parent package re-exports it from `__init__.py`.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT
from jobcannon.engine.ats_platforms import _title_matches
from jobcannon.engine.careers_crawler._autoheal_seam import (
    record_careers_capture,
    try_careers_override,
)
from jobcannon.engine.careers_crawler._title_filters import (
    _clean_title,
    _is_listing_tile,
    _is_metadata_blob,
    _is_nav_path,
)
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.identity_evidence import _hiring_org_name

logger = logging.getLogger(__name__)

# Minimum text/html ratio to consider a page statically rendered.
# Below this, the page is likely JS-heavy and needs Playwright.
_STATIC_TEXT_RATIO = 0.02
_STATIC_MIN_TEXT_LEN = 500

# --- Location hint extraction from URL slugs ---
# Matches a hyphenated token group that appears BEFORE the job-title slug in
# common ATS URL patterns, e.g.:
#   /job/Hyderabad-DE-Data-Scientist.../  -> candidate: "Hyderabad"
#   /jobs/new-york-ny/senior-engineer-.../ -> candidate: "new york ny"
# Strategy: take the last path segment, split on dashes, and offer the longest
# prefix that could be a location (gazetteer-validated before accepting).
# Only the FIRST contiguous word group is tried (greedy prefix) so we don't
# accidentally submit job-title words as location candidates.
_SLUG_SPLIT_RE = re.compile(r"[\-_]+")

# Tile-container element names that bound the context-title search when an
# <a> has empty inner text. Once we hit one of these we stop walking up.
_TILE_CONTAINER_TAGS = {"li", "article", "section"}
_MAX_CONTEXT_HOPS = 3


def _location_from_jsonld(posting: dict) -> str:
    """Extract a location string from a schema.org JobPosting dict (D-1: lossless).

    Handles three schema.org ``jobLocation`` shapes:
    - Plain string: ``"jobLocation": "Hyderabad, India"``
    - Single ``Place`` with nested ``address`` (``PostalAddress``):
      ``"jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
      "addressLocality": "Hyderabad", "addressRegion": "TG",
      "addressCountry": "IN"}}``
    - List of ``Place`` objects (multi-location postings) — assembled as
      comma-joined ``"addressLocality, addressRegion, addressCountry"`` strings
      joined by " | " (the unambiguous multi-location separator that
      ``split_multi_locations`` recognises).

    No normalisation beyond whitespace collapse and empty-part stripping — the
    caller (``Job.location``) routes through ``upsert_job``'s ``parse_locations``
    fallback, which is the single normaliser (D-2).

    Returns "" when ``jobLocation`` is absent, ``None``, or structurally empty.
    """
    job_location = posting.get("jobLocation")
    if not job_location:
        return ""

    def _place_to_string(place: dict) -> str:
        """Assemble a human-readable string from a schema.org Place/PostalAddress.

        Prefers ``addressLocality`` alone (e.g. "Hyderabad") as the location
        string for ``parse_locations``. Short code subfields (``addressRegion``,
        ``addressCountry``) are intentionally excluded: ISO alpha-2 country codes
        and subdivision codes are ambiguous to ``parse_locations`` — the code
        "TG" resolves as Togo before Telangana, and "IN" resolves as Indiana
        (US) before India. The gazetteer correctly infers country + region from
        the city name alone.

        Fallback chain: locality → region → country (only the first non-empty
        token in that order). Plain-string and Place.name inputs are returned
        verbatim (D-1: no normalisation beyond whitespace strip).
        """
        if isinstance(place, str):
            return place.strip()
        if not isinstance(place, dict):
            return ""
        address = place.get("address")
        if isinstance(address, str):
            return address.strip()
        if isinstance(address, dict):
            locality = (address.get("addressLocality") or "").strip()
            if locality:
                return locality
            region = (address.get("addressRegion") or "").strip()
            if region:
                return region
            country = (address.get("addressCountry") or "").strip()
            return country
        # Place with no address — try the Place's name
        return place.get("name", "").strip()

    if isinstance(job_location, str):
        return job_location.strip()
    if isinstance(job_location, dict):
        return _place_to_string(job_location)
    if isinstance(job_location, list):
        parts = [_place_to_string(p) for p in job_location if p]
        assembled = " | ".join(p for p in parts if p)
        return assembled

    return ""


def _location_from_url_slug(url: str) -> str | None:
    """Extract a gazetteer-validated location hint from a job URL slug (D-3).

    Candidate accepted ONLY if ``parse_locations(candidate)`` resolves it
    with ``unresolved=False`` — gazetteer-validated or discarded (no guessing,
    D-3 spirit). JSON-LD beats slug (callers prefer non-empty JSON-LD result).

    Strategy:
    - Take the last non-empty path segment of the URL.
    - Split on hyphens/underscores → word list.
    - Progressively try longer prefixes (1 word up to 4 words) joined by spaces.
    - Accept the LONGEST prefix that ``parse_locations`` resolves without
      ``unresolved=True``.  Returns ``None`` when nothing resolves.

    Args:
        url: An absolute or relative job URL.

    Returns:
        A location string (e.g. ``"Hyderabad"``, ``"New York"``), or ``None``
        when no gazetteer-validated location is found in the slug.
    """
    if not url:
        return None

    try:
        path = urlparse(url).path
    except Exception:
        return None

    segments = [s for s in path.split("/") if s]
    if not segments:
        return None

    # Use the last path segment (most likely the job-specific slug).
    slug = segments[-1]
    words = _SLUG_SPLIT_RE.split(slug)
    words = [w for w in words if w and w.isalpha()]  # drop numeric/empty tokens
    if not words:
        return None

    # Lazy import — same pattern as apply_location_observation to avoid a
    # module-load-time circular import (web/ module importing web/).
    from jobcannon.engine.location_parser import parse_locations

    # Try progressively longer prefixes (1–4 words) and accept the longest
    # prefix that resolves. Cap at 4 to avoid consuming job-title words.
    best: str | None = None
    for end in range(1, min(len(words) + 1, 5)):
        candidate = " ".join(words[:end])
        try:
            parsed = parse_locations(candidate)
        except Exception:
            continue
        if parsed and not parsed[0].unresolved:
            best = candidate  # keep going — a longer prefix might match better
    return best


def _find_title_via_context(tag) -> str:
    """Find a title in the DOM context of an empty/short-text <a> tag.

    Oracle-style listings render each tile as an <a> wrapping no visible
    text — the title lives in a sibling/parent <h*> instead. Walk up at
    most ``_MAX_CONTEXT_HOPS`` ancestors (or stop early at a tile-like
    container), searching each ancestor's subtree for a heading.

    Returns the heading text, or "" when no usable title is found.
    """
    current = tag
    for _ in range(_MAX_CONTEXT_HOPS):
        parent = current.parent
        if parent is None or parent.name in (None, "body", "html", "[document]"):
            break
        heading = parent.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        if heading is not None:
            text = heading.get_text(strip=True)
            if text and len(text) >= 4:
                return text
        if parent.name in _TILE_CONTAINER_TAGS:
            break
        current = parent
    return ""


def _soup_page_title(soup: BeautifulSoup) -> str:
    """Return the text content of the <title> tag, or empty string."""
    title_tag = soup.title
    if title_tag:
        return title_tag.get_text(strip=True)
    return ""


def _soup_og_site_name(soup: BeautifulSoup) -> str:
    """Return the content of the og:site_name meta tag, or empty string."""
    for tag in soup.find_all("meta"):
        if tag.get("property", "").lower() == "og:site_name":
            return tag.get("content", "").strip()
    return ""


def _extract_candidates(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Structural candidate extraction: JSON-LD + link passes WITHOUT title filtering.

    Returns every plausible job posting/link in DOM order — nav links,
    metadata blobs, and exact ``(url, title)`` duplicates excluded (those are
    structural junk), but titles NOT matched against the user's targets.

    The autoheal detector counts these (invariant I4): a page that still
    renders job links but has zero *matching* titles is "your roles were
    filled", not "the page broke". ``_filter_candidates`` applies the user's
    title filter on top to produce the crawl result.
    """
    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Page-level identity evidence is the same for every candidate on this
    # page; capture it once and stamp it on each candidate. Tiers that
    # don't have access to the parsed soup (API results, etc.) simply omit
    # these keys and the persistence layer falls back to seed-trust.
    page_title = _soup_page_title(soup)
    og_site_name = _soup_og_site_name(soup)

    # --- Pass 1: JSON-LD structured data ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        postings = _extract_jsonld_postings(data)
        for posting in postings:
            title = posting.get("title", "")
            url = posting.get("url") or posting.get("sameAs") or ""
            if not title:
                continue
            if url and url.startswith("/"):
                url = urljoin(base_url, url)
            # Collapse exact duplicates only when a URL exists — URL-less
            # JSON-LD postings were never deduped by the pre-split code.
            if url:
                if (url, title) in seen_pairs:
                    continue
                seen_pairs.add((url, title))
            # D-1: carry the JSON-LD jobLocation forward losslessly. The
            # 'location' key is optional — absent when jobLocation was missing.
            entry: dict = {"title": title, "url": url, "description": ""}
            loc = posting.get("location", "")
            if loc:
                entry["location"] = loc
            # Identity evidence: JSON-LD hiringOrganization and page-level
            # title/og:site_name let the persistence layer verify that the
            # scraped employer matches the seed company (#1333).
            org_name = _hiring_org_name(posting)
            if org_name:
                entry["hiring_organization"] = org_name
            entry["page_title"] = page_title
            entry["og_site_name"] = og_site_name
            candidates.append(entry)

    # --- Pass 2: Link text matching ---
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        raw_text = tag.get_text(strip=True)
        # Oracle-style listings: the <a> wrapping a job tile has empty inner
        # text because the title lives in a sibling <h*>. Look up the DOM
        # before discarding. See FOLLOWUPS.md round-15 Gap #2.
        context_title = ""
        if not raw_text or len(raw_text) < 4:
            context_title = _find_title_via_context(tag)
            if not context_title:
                continue

        # Resolve URL
        absolute_url = urljoin(base_url, href)
        parsed = urlparse(absolute_url)

        # Filter out navigation links (with /search subpath allowance — see
        # `_is_nav_path` docstring and FOLLOWUPS round-15 Gap #3).
        if _is_nav_path(parsed.path):
            continue

        # Clean title. A context-resolved title already comes from a heading
        # element and skips _clean_title's suffix-stripping (the heading text
        # is clean by construction).
        title = context_title or _clean_title(tag, raw_text)
        # Obvious metadata blobs from aggregator-style pages are structural
        # junk, not postings. ParsedJob.from_job() provides universal
        # enforcement (Phase 48.01); this avoids wasted work on garbage
        # titles. See FOLLOWUPS.md 2026-05-27 audit.
        if _is_metadata_blob(title):
            continue
        # Result-count / category-landing tiles (#211): "84 Data Scientist
        # Jobs" ordered-words-matches a target title and slips the keyword
        # gate. ParsedJob.from_job hard-drops these (I-14), but reject here
        # too — a cheap early exit before ParsedJob construction.
        if _is_listing_tile(title):
            continue

        if (absolute_url, title) in seen_pairs:
            continue
        seen_pairs.add((absolute_url, title))
        # D-3: URL-slug location hint — accepted only when gazetteer-validated.
        # JSON-LD location (if any) takes precedence over the slug; the slug
        # provides a fallback for sites that embed city names in their URLs
        # (e.g. /jobs/Hyderabad-DE-Data-Scientist...) but don't publish JSON-LD.
        link_entry: dict = {"title": title, "url": absolute_url, "description": ""}
        slug_loc = _location_from_url_slug(absolute_url)
        if slug_loc:
            link_entry["location"] = slug_loc
        # Page-level identity evidence for the persistence-layer employer gate.
        link_entry["page_title"] = page_title
        link_entry["og_site_name"] = og_site_name
        candidates.append(link_entry)

    return candidates


def _filter_candidates(
    candidates: list[dict],
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict]:
    """Apply the user's title filter + URL dedup to structural candidates.

    Filter-then-dedup reproduces the pre-split semantics exactly: only
    MATCHED results ever claimed a URL, so a tile whose generic "Apply" link
    precedes its title link still yields the titled match.
    """
    results: list[dict] = []
    seen_urls: set[str] = set()
    for cand in candidates:
        if cand["url"] and cand["url"] in seen_urls:
            continue
        if not _title_matches(cand["title"], target_titles, exclusions):
            continue
        if cand["url"]:
            seen_urls.add(cand["url"])
        results.append(cand)
    return results


def _extract_jobs_from_soup(
    soup: BeautifulSoup,
    base_url: str,
    target_titles: list[str],
    exclusions: list[str],
) -> list[dict]:
    """Extract job listings from parsed HTML using JSON-LD and link matching.

    Returns list of dicts with 'title', 'url', 'description' keys.
    Description is always empty — the enrichment pipeline handles JD fetching.

    Composed of ``_extract_candidates`` (structural pass — what the autoheal
    detector counts, I4) and ``_filter_candidates`` (the user's title filter);
    output is behavior-identical to the pre-split implementation.

    Args:
        soup: Parsed HTML.
        base_url: Base URL for resolving relative hrefs.
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.

    Returns:
        List of matched job dicts. May be empty.
    """
    return _filter_candidates(_extract_candidates(soup, base_url), target_titles, exclusions)


def _extract_jsonld_postings(data) -> list[dict]:
    """Recursively extract JobPosting entries from JSON-LD data.

    Handles single objects, arrays, ItemList wrappers, and @graph arrays.

    Each returned dict has at least 'title' key. When ``jobLocation`` is
    present on the schema.org JobPosting, a 'location' key is added with
    the assembled location string (see ``_location_from_jsonld``).

    Args:
        data: Parsed JSON-LD data (dict or list).

    Returns:
        List of dicts with at least 'title' key and optional 'location' key.
    """
    postings: list[dict] = []
    if isinstance(data, list):
        for item in data:
            postings.extend(_extract_jsonld_postings(item))
    elif isinstance(data, dict):
        dtype = data.get("@type", "")
        if dtype == "JobPosting":
            entry: dict = dict(data)
            loc = _location_from_jsonld(data)
            if loc:
                entry["location"] = loc
            postings.append(entry)
        elif dtype == "ItemList":
            for item in data.get("itemListElement", []):
                postings.extend(_extract_jsonld_postings(item))
        elif "@graph" in data:
            postings.extend(_extract_jsonld_postings(data["@graph"]))
    return postings


def _try_static_extract(
    url: str,
    target_titles: list[str],
    exclusions: list[str],
    # PORT-SEAM: db_path param dropped -- record_careers_capture no longer
    # takes one; svc.connection_factory() is zero-arg (L-0469)
) -> list[dict] | None:
    """Try extracting jobs from static HTML (no JS rendering).

    Returns:
        list[dict] — extracted jobs (may be empty if page is static but has no matches)
        None — page appears JS-heavy, caller should try Playwright
    """
    try:
        resp = fetch_with_deadline(url, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("Static fetch failed for '%s': %s", url, e)
        return None  # Can't tell if JS or down — let Playwright try

    html = resp.text
    text_len = len(resp.text.strip())
    if text_len == 0:
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    # Check if page is JS-heavy (low text content relative to HTML size)
    plain_text = soup.get_text(strip=True)
    ratio = len(plain_text) / max(len(html), 1)

    # Extract jobs regardless — JSON-LD works even on JS-heavy pages
    # if the structured data is embedded in the initial HTML. Candidates are
    # computed once and reused for both the filtered extraction and the
    # structural detection count (no double parse).
    candidates = _extract_candidates(soup, url)
    generic_jobs = _filter_candidates(candidates, target_titles, exclusions)

    # --- Autoheal D4: per-company override first; generic is the shadow comparator ---
    ovr_jobs, ovr_structural = try_careers_override(html, url, target_titles, exclusions)
    used_override = bool(ovr_jobs)
    jobs = ovr_jobs if used_override else generic_jobs

    if used_override:
        # Override recipes don't run through _extract_candidates, so stamp
        # page-level identity evidence directly from the parsed soup.
        page_title = _soup_page_title(soup)
        og_site_name = _soup_og_site_name(soup)
        for job in jobs:
            job.setdefault("page_title", page_title)
            job.setdefault("og_site_name", og_site_name)

    record_careers_capture(  # PORT-SEAM: db_path arg dropped (L-0469)
        url,
        html,
        generic_structural=len(candidates),
        override_structural=ovr_structural,
        used_override=used_override,
        filtered_count=len(jobs),
    )

    if jobs:
        # Found jobs statically — no need for Playwright
        return jobs

    # No jobs found. Determine if Playwright might help.
    if ratio < _STATIC_TEXT_RATIO or len(plain_text) < _STATIC_MIN_TEXT_LEN:
        # Page looks JS-heavy — signal Playwright
        return None

    # Page has plenty of static text but no matching jobs — genuinely empty
    return []

# PORTED from job_finder/web/careers_crawler/_sitemap_tier.py @ 6a2af961fbffb78564ce8783277d916d60ad0906 (private job-cannon). Ledger L-0466 (own row, but not in this unit's assigned list; pulled forward from design note PR-5 -- __init__.py imports this module at module scope, so the boundary guard forces it into this unit; see PR body 'L-0466 absorbed'). No DB access in this module -- no connection_factory seam needed.
"""Sitemap / RSS tier for the careers crawler.

NO-KEY-COMPENSATION Stage 5 lever. Cheap pre-static probe that extracts
job URLs from XML sitemaps and RSS/Atom feeds. Sits between the API-cache
tier and the static-HTML tier in the careers crawler's escalation chain.

Behavior:
- Try `/sitemap.xml`, `/sitemap_index.xml`, `/sitemap-index.xml` from the
  careers_url's root domain. If a sitemap index is returned, fetch up to
  `_MAX_CHILD_SITEMAPS` child sitemaps and merge their URLs.
- Filter to URLs whose path contains a job-listing substring
  (`/jobs/`, `/careers/`, `/positions/`, `/openings/`).
- Derive a candidate title from the URL's last path segment (strip
  trailing numeric / hex job IDs, separator -> space, capitalize words).
- Apply the standard target-titles + exclusions filter via
  `ats_platforms._title_matches`.
- Fall back to `/rss`, `/jobs.rss`, `/careers.rss`, and `<careers_url>.atom`
  if no sitemap candidates surface.

Pure-mechanical: no JS rendering, no API keys, bounded fetch budget
(at most 3 sitemap roots + 3 child sitemaps + 4 RSS feeds per company).
All HTTP errors swallowed — the tier returns `[]` and the orchestrator
falls through to the static tier.

XML parsing uses `defusedxml.ElementTree` (a project dep since `ed1e5f6`)
to neutralize XML entity expansion + external entity attacks on
untrusted sitemap content.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from defusedxml import ElementTree as DefusedET

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT
from jobcannon.engine.ats_platforms import _title_matches
from jobcannon.engine.careers_crawler._cohort_legitimacy import evaluate_cohort_legitimacy
from jobcannon.engine.http_fetch import fetch_with_deadline

logger = logging.getLogger(__name__)

# URL path substrings that suggest a job-listing page. Lowercase.
_JOB_URL_SUBSTRINGS: tuple[str, ...] = ("/jobs/", "/careers/", "/positions/", "/openings/")

# Sitemap candidate paths (relative to the careers_url's root domain).
_SITEMAP_PATHS: tuple[str, ...] = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")

# RSS / Atom candidate paths (relative to the careers_url's root domain).
# `<careers_url>.atom` is also tried — see `_try_rss`.
_RSS_PATHS: tuple[str, ...] = ("/rss", "/jobs.rss", "/careers.rss")

# Cap on child sitemaps fetched when the root sitemap is a `<sitemapindex>`.
# Bounds per-company work; large sites publish dozens of segmented sitemaps.
_MAX_CHILD_SITEMAPS = 3

# Trailing numeric job IDs (e.g. "...engineer-12345", "...engineer-99") —
# strip from URL slug before building a title. Real-world job-URL IDs are
# typically 2-7 digits; single-digit trailing numerals like "engineer-3" are
# preserved in case they encode a role level (rare, but harmless).
_SLUG_TRAILING_NUMERIC_ID = re.compile(r"[-_]+\d{2,}$")

# Trailing hex job IDs (e.g. UUID fragments). Require ≥8 hex chars.
_SLUG_TRAILING_HEX_ID = re.compile(r"[-_]+[a-f0-9]{8,}$", re.IGNORECASE)


def _local_name(elem) -> str:
    """Return an XML element's local tag name, stripping any XML namespace.

    ElementTree preserves namespace prefixes in `elem.tag` as
    `{namespace-uri}localname`. We only care about the local name for
    sitemap/RSS parsing, so strip and return it.
    """
    tag = elem.tag
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _root_url(url: str) -> str:
    """Return the scheme+host root for `url`, or "" if it can't be parsed.

    Examples:
        >>> _root_url("https://company.com/careers")
        'https://company.com'
        >>> _root_url("https://careers.company.com/")
        'https://careers.company.com'
        >>> _root_url("not-a-url")
        ''
    """
    try:
        p = urlparse(url)
    except Exception:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _fetch_xml(url: str, getter: Any | None = None) -> Any | None:
    """GET `url` and parse the body as XML. Return the root Element or None.

    Any HTTP error, non-200 status, or XML parse failure returns None so
    the orchestrator can fall through to the next candidate URL or tier.
    """
    if getter is None:
        getter = requests.get
    try:
        resp = fetch_with_deadline(url, getter=getter, timeout=_TIMEOUT, headers=_HEADERS)
    except Exception as e:
        logger.debug("sitemap_tier: HTTP error for %s: %s", url, e)
        return None
    if resp.status_code != 200:
        logger.debug("sitemap_tier: %s returned HTTP %d", url, resp.status_code)
        return None
    body = resp.content
    if not body or not body.strip():
        return None
    try:
        return DefusedET.fromstring(body)
    except Exception as e:
        logger.debug("sitemap_tier: XML parse failure for %s: %s", url, e)
        return None


def _extract_urls_from_sitemap(root, depth: int = 0, getter: Any | None = None) -> list[str]:
    """Recursively extract `<loc>` URLs from a sitemap root.

    - `<urlset>` root → return every `<loc>` text (the URL list).
    - `<sitemapindex>` root → fetch up to `_MAX_CHILD_SITEMAPS` child
      sitemaps and recurse, capping recursion at depth=1 so a malicious
      index-of-index can't fan out indefinitely.

    Args:
        root: Parsed XML root element.
        depth: Current recursion depth. 0 = top-level sitemap; ≥1 = inside
            a child sitemap, do not recurse further.
        getter: Optional HTTP getter to use for child sitemap fetches. When
            omitted, falls back to the module-level `requests.get` default.

    Returns:
        List of URL strings (may be empty).
    """
    name = _local_name(root)
    if name == "urlset":
        return [
            e.text.strip()
            for e in root.iter()
            if _local_name(e) == "loc" and e.text and e.text.strip()
        ]
    if name == "sitemapindex":
        if depth >= 1:
            return []
        urls: list[str] = []
        fetched = 0
        for e in root.iter():
            if _local_name(e) != "loc" or not e.text:
                continue
            if fetched >= _MAX_CHILD_SITEMAPS:
                break
            child_url = e.text.strip()
            if not child_url:
                continue
            fetched += 1
            child_root = _fetch_xml(child_url, getter=getter)
            if child_root is not None:
                urls.extend(_extract_urls_from_sitemap(child_root, depth + 1, getter=getter))
        return urls
    return []


def _is_job_url(url: str) -> bool:
    """Return True if `url`'s path contains a job-listing substring."""
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False
    return any(s in path for s in _JOB_URL_SUBSTRINGS)


def _title_from_url(url: str) -> str:
    """Derive a candidate job title from the last segment of `url`'s path.

    Pipeline: take last non-empty path segment → strip trailing numeric or
    hex IDs → replace `-` and `_` with spaces → capitalize each word
    (preserving all-uppercase tokens like "QA" or "ML").

    Examples:
        >>> _title_from_url("https://co.com/jobs/senior-software-engineer-12345")
        'Senior Software Engineer'
        >>> _title_from_url("https://co.com/jobs/data-scientist-a1b2c3d4")
        'Data Scientist'
        >>> _title_from_url("https://co.com/")
        ''
    """
    if not url:
        return ""
    try:
        path = urlparse(url).path
    except Exception:
        return ""
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""
    slug = segments[-1]
    slug = _SLUG_TRAILING_NUMERIC_ID.sub("", slug)
    slug = _SLUG_TRAILING_HEX_ID.sub("", slug)
    raw = slug.replace("-", " ").replace("_", " ").strip()
    if not raw:
        return ""
    words = raw.split()
    return " ".join(w if w.isupper() and len(w) > 1 else w.capitalize() for w in words)


def _try_rss(root_url: str, careers_url: str, getter: Any | None = None) -> list[str]:
    """Try a few RSS/Atom feed URLs; return the first non-empty <link> set.

    RSS items use a `<link>URL</link>` text child; Atom uses
    `<link href="URL"/>`. We accept either form.

    Args:
        root_url: Scheme+host of the careers domain (e.g. https://company.com).
        careers_url: Full careers page URL — used to build a `<careers_url>.atom`
            fallback candidate.
        getter: Optional HTTP getter to use for feed fetches. When omitted,
            falls back to the module-level `requests.get` default.

    Returns:
        URLs harvested from the first feed that yielded any. Empty if
        none of the candidates resolved.
    """
    candidates: list[str] = []
    if root_url:
        for path in _RSS_PATHS:
            candidates.append(urljoin(root_url + "/", path.lstrip("/")))
    if careers_url:
        candidates.append(careers_url.rstrip("/") + ".atom")

    for feed_url in candidates:
        root = _fetch_xml(feed_url, getter=getter)
        if root is None:
            continue
        urls: list[str] = []
        for e in root.iter():
            if _local_name(e) != "link":
                continue
            href = e.get("href")  # Atom form: <link href="..."/>
            if href:
                urls.append(href.strip())
            elif e.text and e.text.strip():
                urls.append(e.text.strip())  # RSS form: <link>...</link>
        if urls:
            return urls
    return []


def _try_sitemap_extract(
    careers_url: str,
    target_titles: list[str],
    exclusions: list[str],
    *,
    company_name: str = "",
    config: dict | None = None,
    flag_sink: list[str] | None = None,
) -> list[dict]:
    """Extract job postings from the company's sitemaps + RSS feeds.

    Returns a list of `{"title": str, "url": str, "description": ""}`
    dicts (matching the static-tier result shape). Description is always
    empty — the enrichment pipeline fills it from the job's own page.

    Before returning a large cohort, runs it through the cohort-legitimacy
    gate (`_cohort_legitimacy.evaluate_cohort_legitimacy`) — a bounded,
    sample-based check for "is this actually one company's careers page, or
    a multi-employer aggregator masquerading as one" (closes the
    architectural gap behind the role.com incident, #1144). This is the
    gate's original call site; #1921 extended the gate to the orchestrator
    level so every other tier that can import a cohort is also covered.
    `company_name` and `config` are optional and keyword-only so existing
    callers that don't supply them (unit tests, or callers that don't have
    company context) keep the tier's original unconditional-pass-through
    behavior — the gate no-ops without a company name to check against.

    Args:
        careers_url: The company's careers page URL (e.g. https://co.com/careers).
        target_titles: Target title keywords for inclusion filter.
        exclusions: Title keywords for exclusion filter.
        company_name: The crawled company's `name_raw`, for the
            legitimacy-gate cross-check. Omit to skip the gate entirely.
        config: App config dict, for the gate's `careers_crawl.legitimacy_*`
            tunables. Omit to use gate defaults.
        flag_sink: Optional list the caller can pass to learn whether the
            gate flagged this cohort. On a positive trip, the gate's reason
            string is appended (mirrors the `html_sink` pattern already used
            for Playwright's rendered-DOM handoff elsewhere in this
            package) so the orchestrator can short-circuit the remaining
            escalation chain and persist the flag without a second,
            redundant evaluation.

    Returns:
        Possibly-empty list of matched job dicts. An empty list signals
        either "no candidates" (orchestrator falls through to the static
        tier) or "cohort withheld by the legitimacy gate" (`flag_sink` will
        be non-empty in that case) — the two are deliberately
        indistinguishable to callers that don't pass `flag_sink`, since
        both mean "the sitemap tier produced nothing safe to import."
    """
    if not careers_url:
        return []
    root_url = _root_url(careers_url)
    if not root_url:
        return []

    # The session is closed in the finally block. fetch_with_deadline may
    # abandon a worker thread on its hard deadline while it still holds a
    # connection from this Session's pool. That is safe: Session.close()
    # only drains idle pool entries, and a straggler returning its connection
    # to a cleared pool is closed by urllib3 without raising.
    session = requests.Session()
    try:
        candidate_urls: list[str] = []
        getter = session.get

        # === Pass 1: sitemap.xml family ===
        for path in _SITEMAP_PATHS:
            sitemap_url = urljoin(root_url + "/", path.lstrip("/"))
            sitemap_root = _fetch_xml(sitemap_url, getter=getter)
            if sitemap_root is None:
                continue
            candidate_urls.extend(_extract_urls_from_sitemap(sitemap_root, getter=getter))
            if candidate_urls:
                break  # First non-empty sitemap wins

        # === Pass 2: RSS / Atom fallback ===
        if not candidate_urls:
            candidate_urls = _try_rss(root_url, careers_url, getter=getter)

        if not candidate_urls:
            return []

        # === Filter: job-URL signature + title-match ===
        results: list[dict] = []
        seen: set[str] = set()
        for url in candidate_urls:
            if not url or url in seen:
                continue
            seen.add(url)
            if not _is_job_url(url):
                continue
            title = _title_from_url(url)
            if not title:
                continue
            if not _title_matches(title, target_titles, exclusions):
                continue
            results.append({"title": title, "url": url, "description": ""})

        # === Cohort-legitimacy gate: is this one employer, or an aggregator? ===
        # No-ops (returns flagged=False) when company_name is unset — existing
        # callers that don't pass it get the tier's original behavior unchanged.
        verdict = evaluate_cohort_legitimacy(company_name, results, config)
        if verdict.flagged:
            logger.warning(
                "sitemap_tier: cohort-legitimacy gate flagged '%s' (%s) — "
                "withholding %d candidate posting(s) from import",
                company_name,
                verdict.reason,
                len(results),
            )
            if flag_sink is not None:
                flag_sink.append(verdict.reason)
            return []

        return results
    finally:
        session.close()

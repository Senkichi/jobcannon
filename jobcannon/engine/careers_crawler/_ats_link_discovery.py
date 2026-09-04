# PORTED from job_finder/web/careers_crawler/_ats_link_discovery.py @ 1ff50e2e6b996fe79023c11b14028afe52828c0d (private job-cannon). Ledger L-0470.
"""Outbound ATS-link discovery for custom career pages (#453).

A large cohort of ``companies`` rows have a ``careers_url`` but no detectable
ATS (``ats_platform IS NULL`` / ``ats_probe_status != 'hit'``). Many such
"custom" sites are thin shells that link out to a real Greenhouse / Lever /
Ashby / Workday / SmartRecruiters board in an anchor, an ``<iframe>``, or an
inline-JS string. This module harvests those outbound links from an
*already-rendered* DOM and classifies each via the existing
``extract_ats_from_url_best`` URL classifier. The caller (the careers crawler)
then promotes the company to the matching existing scanner.

This is **link discovery**, not a new extractor: no new scanner, no LLM, no
network call. Pure functions over an HTML string. Only platforms that own a
working scanner are eligible for promotion (``_TARGET_PLATFORMS``, derived from
the scanner registry); a URL pointing at a non-scannable platform (e.g. the
jobvite stub) is dropped and the company left as a custom site.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from jobcannon.engine.ats_detection import extract_ats_from_url_best
from jobcannon.engine.ats_registry import SCANNABLE_TARGET_PLATFORMS as _TARGET_PLATFORMS

# Platforms eligible for promotion = every scannable platform (requests OR the
# Playwright iCIMS path) MINUS the non-scannable stubs (jobvite/google), derived
# once in ``ats_registry`` so this set can never drift behind a newly added
# scanner. (Previously hand-rolled here as
# ``(SCANNERS_BY_NAME - NON_SCANNABLE) | {icims}``.) A contract test
# (test_ats_link_discovery) pins it against the live registry.

# Permissive absolute-URL matcher for inline-JS / raw-text scraping. Stops at
# whitespace and the common string/markup delimiters so a URL embedded in a
# JS string literal (``"https://jobs.lever.co/acme"``) or an HTML attribute is
# captured without trailing quotes/brackets.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\\]+", re.IGNORECASE)


def _candidate_urls(html: str, soup: BeautifulSoup) -> list[str]:
    """Collect candidate absolute URLs from anchors, iframes, and raw text.

    Returns a new list with original casing preserved (Ashby slugs are
    case-sensitive). Order is anchors → iframes → regex-over-text/html, which
    only matters for the final dedup's first-wins; specificity sorting happens
    downstream.
    """
    urls: list[str] = []

    for a in soup.find_all("a", href=True):
        href = a.get("href") or ""
        if isinstance(href, str) and href.strip():
            urls.append(href.strip())

    for frame in soup.find_all("iframe", src=True):
        src = frame.get("src") or ""
        if isinstance(src, str) and src.strip():
            urls.append(src.strip())

    # Inline-JS / raw-text URLs: scan both the rendered text and the raw HTML
    # so URLs living in <script> bodies (not surfaced by get_text) are caught.
    urls.extend(_URL_RE.findall(soup.get_text(" ")))
    urls.extend(_URL_RE.findall(html))

    return urls


def discover_ats_links_from_html(html: str, page_url: str) -> list[tuple[str, str, int]]:
    """Extract target-scanner ATS links from a rendered custom career page.

    Pulls candidate URLs from ``<a href>``, ``<iframe src>``, and inline-JS /
    raw-text matches, classifies each via ``extract_ats_from_url_best``, and
    keeps only scanner-backed target platforms. The result is deduped on
    ``(platform, slug)`` (highest specificity wins) and sorted by specificity
    descending so the canonical/API-shaped trace ranks above the board-shaped
    one. ``page_url`` is accepted for signature symmetry with the caller and
    future relative-URL resolution; it is not used today (ATS links are
    absolute).

    Returns:
        A new list of ``(platform, slug, specificity)`` tuples. Empty when the
        page links to no target-scanner board. Never mutates its inputs.
    """
    if not isinstance(html, str) or not html.strip():
        return []

    soup = BeautifulSoup(html, "html.parser")

    # (platform, slug) -> best specificity seen.
    best_by_pair: dict[tuple[str, str], int] = {}
    for url in _candidate_urls(html, soup):
        hit = extract_ats_from_url_best(url)
        if hit is None:
            continue
        platform, slug, spec = hit
        if platform not in _TARGET_PLATFORMS:
            continue
        key = (platform, slug)
        if spec > best_by_pair.get(key, -1):
            best_by_pair[key] = spec

    candidates = [(plat, slug, spec) for (plat, slug), spec in best_by_pair.items()]
    # Specificity desc; platform/slug asc as deterministic tie-breakers so the
    # ordering is stable regardless of dict insertion order.
    candidates.sort(key=lambda c: (-c[2], c[0], c[1]))
    return candidates


def best_ats_candidate(html: str, page_url: str) -> tuple[str, str] | None:
    """Return the single best ``(platform, slug)`` to promote, or ``None``.

    Picks the highest-specificity discovered link. Abstains (``None``) when two
    *distinct platforms* tie at the top specificity — mirroring
    ``reconcile_company_ats``'s conflict behavior: a page that links to both a
    Greenhouse and a Lever board gives no clear signal which scanner owns it.
    Also ``None`` when the page has no target-scanner links.
    """
    candidates = discover_ats_links_from_html(html, page_url)
    if not candidates:
        return None

    top_spec = candidates[0][2]
    top_platforms = {c[0] for c in candidates if c[2] == top_spec}
    if len(top_platforms) > 1:
        return None

    platform, slug, _spec = candidates[0]
    return platform, slug

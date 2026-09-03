# PORTED from job_finder/web/direct_link.py @ c6e37b72c6706e6f547c63d0a697b9ef645c2dff (private job-cannon). Ledger L-0176.
"""Pure resolution logic for the direct company-posting link.

No DB, no network. Four responsibilities:
  - classify a URL as an ATS/careers (company-owned) link vs an aggregator —
    by host (is_ats_or_careers_url) for the strict, security-sensitive gate, or
    by host OR the job's own company careers domain (is_canonical_apply_url)
    for display preference;
  - promote an already-known ATS source_url to the direct link (free, no scan);
  - pick the best (matched_posting, url, confidence) from postings an ATS scan /
    careers scrape already fetched, tagging strict (unique exact-title, or
    location-disambiguated among exact-title duplicates) vs loose (first-match);
  - choose the Apply-button target for a job row (apply_url_for) — the single
    enforcement point for the strict-direct_url > source_urls[0] precedence.

Title normalization consistently uses ats_platforms._title_match._normalize_title
(NOT normalizers.normalize_title, the dedup-key normalizer) — the two differ,
and mixing them silently changes match behavior.

The strict/loose tag is an experiment: both bars are evaluated on the same
posting set so the user can compare link quality in real use and later drop
the losing branch. Data merging is strict-gated: a loose match yields a LINK
only — callers must never merge posting data (jd_full, salary, ...) from it.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from jobcannon.engine.ats_platforms._title_match import _normalize_title

# Bare domains that mark a URL as a company-owned ATS / careers posting.
# Matched against the PARSED hostname (host-boundary: exact or subdomain), not
# a substring of netloc. Covers the registered ATS platforms.
_ATS_HOST_MARKERS: tuple[str, ...] = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "recruitee.com",
    "breezy.hr",
    "applytojob.com",  # JazzHR
    "pinpointhq.com",
    "jobs.personio.de",
    "jobs.personio.com",
    "bamboohr.com",
    "teamtailor.com",
    "workable.com",
    "jobvite.com",
    "paylocity.com",
    "rippling.com",
)


def is_ats_or_careers_url(url: str | None) -> bool:
    """Return True if the URL host is a known ATS / company careers board.

    Host-boundary matched (exact or subdomain) against the PARSED hostname —
    never a substring of netloc/URL — so a look-alike host
    ("greenhouse.io.evil.example") or a domain embedded in a path/query cannot
    be misclassified as a trusted ATS/company link (py/incomplete-url-
    substring-sanitization guard). This matters more than most callers: this
    function gates promote_existing_direct_url -> apply_url_for, the single
    enforcement point for the Apply-button URL surfaced to the user.
    """
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return any(host == marker or host.endswith("." + marker) for marker in _ATS_HOST_MARKERS)


def _url_host(url: str | None) -> str | None:
    """Lowercased hostname of an http(s) URL, or None.

    The anti-spoof shape shared by every host comparison here: callers match a
    PARSED hostname on a label boundary, never a raw substring of the URL — so a
    look-alike host or a domain embedded in a path/query can't be misclassified.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    return (parsed.hostname or "").lower() or None


def careers_host_of(careers_url: str | None) -> str | None:
    """Return the label-boundary host marker for a company's careers_url.

    The host of companies.careers_url with a leading ``www.`` stripped, so a stored
    ``https://www.metacareers.com/jobs`` marks both ``www.metacareers.com`` and
    ``metacareers.com`` postings as that employer's own. Returns None when the
    careers_url is empty/unparseable. Callers pass the result to is_canonical_apply_url
    / apply_url_for / apply_targets as ``company_careers_host``.
    """
    host = _url_host(careers_url)
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def is_canonical_apply_url(url: str | None, *, company_careers_host: str | None = None) -> bool:
    """True if the URL is the employer's own canonical application page.

    Provenance-based, NOT vendor-specific: canonical iff the URL is on a recognized
    ATS/careers host (is_ats_or_careers_url) OR on this job's own company careers
    domain (``company_careers_host`` — the host of companies.careers_url, collected
    per company by the careers crawler, migration m030).

    This generalizes "prefer the employer's link over any aggregator" to EVERY ATS
    vendor and EVERY employer domain Job Cannon has crawled, with no per-vendor
    embed-param list: an employer that embeds any ATS on careers.<employer>.com or
    joinhandshake.com is recognized because the host matches the company's own
    careers_url — not because we enumerated that ATS's query param.

    Display-preference predicate ONLY — it MUST NOT gate auto-submit.
    submit_orchestrator stays on the host-allowlisted is_ats_or_careers_url so it
    never dispatches an application to a company-specific careers host we haven't
    independently vetted; surfacing such a link for the user to click while refusing
    to auto-submit to it is the safe asymmetry.
    """
    if is_ats_or_careers_url(url):
        return True
    if not company_careers_host:
        return False
    host = _url_host(url)
    return bool(host) and (
        host == company_careers_host or host.endswith("." + company_careers_host)
    )


def promote_existing_direct_url(source_urls: list[str]) -> str | None:
    """Return the first source_url already on an ATS/careers host, else None."""
    for url in source_urls or []:
        if is_ats_or_careers_url(url):
            return url
    return None


def _posting_link(posting: dict) -> str | None:
    """Return a posting's link, tolerating ATS (source_url) vs careers (url) keys."""
    return posting.get("source_url") or posting.get("url") or None


# Tokens too generic to disambiguate a location on their own ("United States",
# "Greater Boston Area"). Remote/Hybrid are deliberately KEPT — they are the
# signal for remote-vs-office duplicates of the same title.
_LOCATION_STOPWORDS = frozenset(
    {"united", "states", "usa", "the", "and", "area", "greater", "metro"}
)


def _location_tokens(text: str | None) -> set[str]:
    """Lowercased alphanumeric tokens (len >= 3) of a freeform location string."""
    if not text:
        return set()
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if t not in _LOCATION_STOPWORDS}


def resolve_primary_posting(
    postings: list[dict],
    job_title: str,
    job_location: str = "",
) -> tuple[dict | None, str, str] | None:
    """Return (matched_posting, url, confidence) for the best primary posting.

    confidence:
      strict — exactly one posting's normalized title equals the job's; or,
               among several exact-title postings (same role posted in N
               locations), exactly one shares a location token with the job.
               matched_posting is that posting — safe to merge data from.
      loose  — ambiguous exact-title match (same title in N locations that
               location tokens could not disambiguate): the first exact-title
               posting's link, with matched_posting=None. Callers MUST NOT
               merge posting data on a loose match — only the link itself is
               worth showing.

    Postings without a usable link are ignored. Returns None when no posting
    carries a link, OR when no posting's normalized title exactly matches the
    job's — a no-exact-title-match job has no plausible board posting, so
    handing it the first posting's URL would stamp one stale link across an
    entire unmatched batch (#1932). The LLM tie-breaker in the
    primary_source_resolver can still recover a semantic match from the full
    board; this function's heuristic responsibility ends at exact-title match.
    """
    linked = [(p, _posting_link(p)) for p in (postings or [])]
    linked = [(p, url) for p, url in linked if url]
    if not linked:
        return None

    target = _normalize_title(job_title or "")
    exact = [(p, url) for p, url in linked if _normalize_title(p.get("title", "")) == target]
    if len(exact) == 1:
        posting, url = exact[0]
        return (posting, url, "strict")

    if len(exact) > 1:
        # Same title posted in several locations — disambiguate by location.
        job_tokens = _location_tokens(job_location)
        if job_tokens:
            located = [
                (p, url) for p, url in exact if job_tokens & _location_tokens(p.get("location"))
            ]
            if len(located) == 1:
                posting, url = located[0]
                return (posting, url, "strict")
        # Still ambiguous — link the first exact-title posting, merge nothing.
        return (None, exact[0][1], "loose")

    # No exact-title match — the job has no plausible board posting. Returning
    # the first linked posting's URL here would stamp one stale link across
    # every unmatched job in a company's batch (#1932). The LLM tie-breaker in
    # the resolver can still recover a semantic match; this heuristic does not.
    return None


def resolve_direct_link(postings: list[dict], job_title: str) -> tuple[str, str] | None:
    """Return (url, confidence) for the best direct posting link, or None.

    Thin wrapper over resolve_primary_posting for callers that only need the
    link. confidence is 'strict' (unambiguous title match) or 'loose'.
    """
    resolved = resolve_primary_posting(postings, job_title)
    if resolved is None:
        return None
    _posting, url, confidence = resolved
    return (url, confidence)


def pick_direct_link(
    source_urls: list[str],
    ats_result: dict,
    careers_result: dict,
) -> tuple[str, str] | None:
    """Choose the best direct link by source precedence.

    Order: an existing source_url already on an ATS/careers host (strict, free)
    -> the ATS-scan result -> the careers-scrape result. Returns (url, confidence)
    or None.
    """
    promoted = promote_existing_direct_url(source_urls)
    if promoted:
        return (promoted, "strict")

    for result in (ats_result or {}, careers_result or {}):
        url = result.get("direct_url")
        conf = result.get("direct_url_confidence")
        if url and conf in ("strict", "loose"):
            return (url, conf)

    return None


def _row_value(job_row, key: str):
    """Tolerant field access for plain dicts and sqlite3.Row objects."""
    try:
        return job_row[key]
    except (KeyError, IndexError, TypeError):
        return None


def apply_url_for(
    job_row,
    *,
    company_careers_host: str | None = None,
    allow_loose_direct_url: bool = True,
) -> str | None:
    """Return the Apply-button target for a job row.

    The single enforcement point for the apply precedence:
      1. the resolved company posting (direct_url) always wins when live —
         strict OR loose. direct_url is canonical BY CONSTRUCTION (it comes from
         an ATS scan / careers crawl / promoted ATS source_url, never an
         aggregator), so we prefer it over any aggregator sighting even when the
         title match was only loose. A loose match may point at a nearby role at
         the SAME employer rather than the aggregator's exact posting — an
         accepted trade for a clean, canonical, referrer-safe apply page
         (user decision: prefer-canonical-over-aggregator is the consistent
         default in all cases);
      2. otherwise, among the source_urls, prefer a canonical link
         (is_canonical_apply_url: a recognized ATS/careers host OR this job's own
         company careers domain, company_careers_host) over blind positional
         order — a real posting link that was never promoted to direct_url still
         beats a known aggregator redirect;
      3. failing that, the first source_url, whatever it is.

    Staleness fallback (Phase 5): an expired job's direct_url is skipped even if
    still populated — the company posting is gone, so we send the user to the
    aggregator listing (which often outlives the ATS posting and at least shows
    context). The reconciler NULLs direct_url on expiry, but this guard also
    covers the window before the next reconcile pass runs.

    company_careers_host is the host of this job's company careers_url (via
    careers_host_of); the Jinja global resolves it per row from the companies
    table. When absent, canonical detection falls back to ATS/careers-host
    recognition only.

    allow_loose_direct_url (default True) implements the display decision above:
    a loose direct_url wins for the Apply BUTTON. The browser-extension /match
    endpoint (apply_api) passes False, because a loose direct_url is SHARED
    across every ambiguous-title sibling job at a company — resolve_primary_posting
    hands each such job the SAME board posting URL — so using it to disambiguate
    which job a URL belongs to collapses those siblings into a spurious
    multi-match (409). With False, a loose direct_url is skipped and resolution
    falls back to each row's own (unique) source_urls. A strict direct_url is
    unaffected and always wins.

    Accepts dicts and sqlite3.Row; source_urls may be a JSON string (raw row)
    or an already-parsed list.
    """
    direct = _row_value(job_row, "direct_url")
    confidence = _row_value(job_row, "direct_url_confidence")
    expired = _row_value(job_row, "expiry_status") == "expired"
    if (
        direct
        and not expired
        and (confidence == "strict" or (confidence == "loose" and allow_loose_direct_url))
    ):
        return direct

    raw = _row_value(job_row, "source_urls")
    if isinstance(raw, str):
        try:
            urls = json.loads(raw)
        except (ValueError, TypeError):
            urls = []
    else:
        urls = raw if isinstance(raw, list) else []
    urls = [url for url in urls if url and isinstance(url, str)]

    # Prefer a canonical company/ATS posting over positional order (2b,
    # job-listing-verification Plan 1) — the Apply button must never point at a
    # raw aggregator redirect when the row already carries a real posting link,
    # whether on a multi-tenant ATS host or the employer's own careers domain
    # (company_careers_host), and even if it was never promoted to direct_url.
    for url in urls:
        if is_canonical_apply_url(url, company_careers_host=company_careers_host):
            return url

    return urls[0] if urls else None


def _posting_label(descriptor: dict) -> str:
    """Return a short human-readable label for a posting descriptor.

    Derives the label from the descriptor's locations_structured + workplace_type.
    Format: "City (Workplace)" or "City, Region (Workplace)" or "Remote".
    Falls back to ats_platform name or "Apply" when no location is known.
    """
    # Try to build a location-based label
    loc_structured = descriptor.get("locations_structured")
    workplace = descriptor.get("workplace_type")

    if loc_structured and isinstance(loc_structured, list) and len(loc_structured) > 0:
        # Use the first location from the structured list
        first_loc = loc_structured[0]
        if isinstance(first_loc, dict):
            city = first_loc.get("city") or ""
            region = first_loc.get("region_code") or ""

            parts = []
            if city:
                parts.append(city)
            if region:
                parts.append(region)

            location_str = ", ".join(parts) if parts else ""

            # Add workplace suffix if present and not UNSPECIFIED
            if workplace and workplace != "UNSPECIFIED":
                workplace_suffix = workplace.title()
                if location_str:
                    return f"{location_str} ({workplace_suffix})"
                else:
                    return workplace_suffix
            elif location_str:
                return location_str

    # Fallback to workplace-only if no structured location
    if workplace and workplace != "UNSPECIFIED":
        return workplace.title()

    # Final fallback to ats_platform or neutral label
    ats_platform = descriptor.get("ats_platform")
    if ats_platform:
        return ats_platform
    return "Apply"


def apply_targets(
    job_row, *, loose_apply_default: bool = False, company_careers_host: str | None = None
) -> list[dict]:
    """Return an ordered list of apply targets for a job row.

    Each target is a dict with keys:
      - "label": str (short human-readable location/identifier)
      - "apply_url": str (the apply link)
      - "location_fit": int | None (the descriptor's location_fit, or None)
      - "location_fit_color": str | None (the descriptor's policy-driven
        ``location_fit_color`` — "bg-emerald-500" / "bg-amber-500" /
        "bg-red-500" — written by location_policy.apply_location_policy_to_postings
        (issue #1214). ALWAYS present as a key, explicitly None for postings that
        predate #1214 (no color was ever computed for them) so the template never
        sees a Jinja Undefined and can fall back to its own legacy threshold
        coloring. This is the single source of truth for badge color — callers
        must NOT re-derive a color from location_fit thresholds when this is set.

    When the job has a populated 'postings' column (P1+), returns one target
    per posting descriptor, preserving the postings list order. Per-posting
    apply_url precedence:
      1. A canonical descriptor apply_url (recognized ATS/careers host OR this
         job's own company careers domain, company_careers_host)
      2. A non-canonical (aggregator-ish) descriptor link ONLY when
         loose_apply_default is set
      3. Otherwise skip the descriptor (no link-less targets)

    Note: `loose_apply_default` here gates the per-posting link-QUALITY check
    (is this descriptor's apply_url canonical?), a different axis from
    apply_url_for's direct_url title-match confidence — which now always prefers
    the canonical company posting. When postings is empty/missing, falls back to
    a single-element list built from apply_url_for(job_row,
    company_careers_host=...). When apply_url_for also returns None, returns an
    empty list.

    Accepts dicts and sqlite3.Row; postings may be a JSON string (raw row)
    or an already-parsed list. Malformed JSON degrades to the fallback.
    """
    # Parse postings tolerantly
    raw_postings = _row_value(job_row, "postings")
    postings = []

    if raw_postings:
        if isinstance(raw_postings, str):
            try:
                postings = json.loads(raw_postings)
            except (ValueError, TypeError):
                postings = []
        elif isinstance(raw_postings, list):
            postings = raw_postings

    targets = []
    if postings and isinstance(postings, list):
        for descriptor in postings:
            if not isinstance(descriptor, dict):
                continue

            # Get the apply_url from the descriptor
            apply_url = descriptor.get("apply_url")

            # Skip descriptors without an apply_url
            if not apply_url or not isinstance(apply_url, str):
                continue

            # Check if this is a loose link (not a canonical company/ATS URL — a
            # link on a recognized ATS/careers host or this job's own company
            # careers domain counts as canonical, same as apply_url_for).
            is_loose = not is_canonical_apply_url(
                apply_url, company_careers_host=company_careers_host
            )

            # Skip loose links unless loose_apply_default is set
            if is_loose and not loose_apply_default:
                continue

            # Build the target
            label = _posting_label(descriptor)
            location_fit = descriptor.get("location_fit")  # None if missing (P1)
            # Policy-driven color (#1214/#1215): None for postings written before
            # #1214 ever ran the location-policy engine over them — the template
            # falls back to its own legacy threshold coloring in that case.
            location_fit_color = descriptor.get("location_fit_color")

            targets.append(
                {
                    "label": label,
                    "apply_url": apply_url,
                    "location_fit": location_fit,
                    "location_fit_color": location_fit_color,
                }
            )

    if not targets:
        # Fallback: use apply_url_for when postings is empty/missing/malformed,
        # or when every per-posting link was non-canonical and loose_apply_default
        # is false. This ensures the user still gets an Apply button if the row
        # carries any resolvable URL.
        fallback_url = apply_url_for(job_row, company_careers_host=company_careers_host)
        if fallback_url:
            # Use row's own location_fit if present. There is no row-level
            # location_fit_color column (#1214 only writes it per-posting), so this
            # single-target fallback always explicitly sets None — the template
            # falls back to its own legacy threshold coloring for this path.
            location_fit = _row_value(job_row, "location_fit")
            return [
                {
                    "label": "Apply",
                    "apply_url": fallback_url,
                    "location_fit": location_fit,
                    "location_fit_color": None,
                }
            ]

    return targets

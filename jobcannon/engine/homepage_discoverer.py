# PORTED from job_finder/web/homepage_discoverer.py @ f20c5b927308f288888fd068a1d3e7af64b644be (private job-cannon). Ledger L-0186.
"""Homepage auto-discovery for companies without a known homepage URL.

Private's five-tier lookup is REDUCED here to the two mechanical tiers plus
the hardened validation gate. This is an ADAPT-with-drop reduction (design
note PR-4 section 1c lists L-0186 among the "six call_model-gated modules,"
per-module change "none expected beyond threading" — that line is WRONG for
this module and is corrected here, not silently papered over; see the three
PORT-SEAM blocks below for why).

Ported (Tiers 1/2/2b, pure HTTP, zero cost):
  - ``_try_domain_guess`` / ``_try_slug_heuristic`` / ``_discover_homepage_free_tiers``
  - ``discover_homepage`` — public one-shot entry point, REDUCED to the free
    ladder only (see PORT-SEAM below).
  - The hardened validation gate: ``HostCheckResult``, ``_validate_guessed_homepage``,
    ``_reject_structural_host``, ``_build_host_census``, ``_other_companies_on_host``,
    ``_registrable_label_and_domain``, ``_host_in_domain_set``.
  - ``_validate_url`` — permissive live-and-not-parked check used by Tiers 1/2.

# PORT-SEAM (Tier 2.5, Ollama guess): private's ``_try_ollama_guess`` constructs
# ``OllamaProvider`` directly and is explicitly HARD-CONSTRAINED to never
# cascade to a paid provider on failure ("Ollama-or-skip"). This repo's
# injected ``call_model`` (``jobcannon.host.model_provider.call_model``) has
# no provider-pin / allowed-providers parameter — routing Tier 2.5 through it
# would let a local-model miss silently escalate to a paid provider, which is
# precisely the invariant Tier 2.5 exists to prevent. There is also no direct
# Ollama-inference class on this host (``jobcannon/host/_ollama.py``, L-0054,
# is a liveness PROBE only — GET /api/tags — not an inference client). Tier
# 2.5 is dropped rather than silently reimplemented against the wrong
# dispatch mechanism. The hardened gate it would have routed through
# (``_validate_guessed_homepage`` et al.) is still ported standalone/unwired
# below, since a future provider-pinned ``call_model`` would reuse it as-is.
#
# PORT-SEAM (Tier 3, Claude CLI): private's ``_try_claude_enricher`` lazily
# imports ``job_finder.web.claude_enricher.enrich_companies_via_claude``, a
# CLI-subprocess provider (``claude -p`` via ``subprocess``) — exactly the
# "CLI / local-binary providers DIE or HOLD" category the design note rules
# on generally. Reimplementing Tier 3 against the injected ``call_model``
# would require authoring a NEW system prompt, JSON schema, and validation
# path with no private counterpart — net-new LLM feature work, not a port.
# Tier 3 DIES (not ported), matching the ai-nav-tier precedent for the same
# category of dependency.
#
# PORT-SEAM (Tier 4, SerpAPI): explicit owner ruling — HOLD (design note's
# "Paid Serp-style sources are HOLD, homepage_discoverer Tier-4 SerpAPI gated
# off"). HOLD is not DIE: simply not ported, no deletion ceremony needed
# since nothing from this tier was ever landed. ``_search_serpapi``,
# ``SerpAPIQuotaError``, ``_SERPAPI_BASE_URL``, ``_SKIP_DOMAINS``,
# ``_SERPAPI_TIER_CAP`` are all dropped with it.
#
# PORT-SEAM (two-phase orchestrator): ``run_homepage_discovery`` (Phase A /
# Phase B split) and ``run_absorbing_resweep`` are NOT ported. Both require
# ``companies.homepage_probe_attempted_at`` / ``companies.homepage_probe_attempts``,
# which do not exist on this host (no migration has ever added them — grep-
# confirmed against jobcannon/db/migrations/). Phase B's entire rationale
# (metering Tiers 2.5/3/4 against separate paid/local-compute quotas while
# Phase A runs the free tiers uncapped) also evaporates now that Tiers 3/4
# are gone and Tier 2.5 is unported — the remaining tiers are all free,
# uncapped, pure-HTTP, so there is nothing left for a metered second phase
# to gate. ``_stamp_probe_attempted`` and ``_write_homepage_found`` (the
# phase-shared write paths, both keyed on the missing probe columns) are
# dropped with the orchestrator. A future PR that ports Tier 2.5 (once a
# provider-pinned call_model exists) or re-derives a genuine metering need
# should revisit whether the probe columns are worth minting via a new
# migration at that point — not speculatively added here with no caller.
#
# Callers reach ``discover_homepage`` directly (no per-run backoff/dedup);
# a caller wanting retry-avoidance should track ``homepage_url IS NULL``
# itself rather than relying on now-absent probe-attempt bookkeeping.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import requests

from jobcannon.engine._http_constants import _HEADERS, _TIMEOUT
from jobcannon.engine.ats_registry import ATS_HOSTED_DOMAINS
from jobcannon.engine.domain_policy import BLOCKED_DOMAINS
from jobcannon.engine.http_fetch import fetch_with_deadline
from jobcannon.engine.identity_evidence import (
    _extract_identity_evidence,
    _name_to_slug,
    _slug_has_token_sequence,
    _strip_company_suffixes,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PARKED_DOMAIN_SIGNATURES = [
    "domain is for sale",
    "buy this domain",
    "parked domain",
    "this domain is available",
    "hugedomains.com",
    "domain_profile.cfm",
    "gen.xyz/cart",
    "this domain may be for sale",
    "make an offer on this domain",
]

# Minimum OTHER distinct companies already sharing a candidate host before it
# is treated as a shared/multi-tenant platform (e.g. a still-unregistered ATS
# vendor, or a directory-style listing site not yet in BLOCKED_DOMAINS).
_MULTI_TENANT_THRESHOLD = 2

# ---------------------------------------------------------------------------
# Tiers 1/2/2b — mechanical, zero-cost
# ---------------------------------------------------------------------------


def _try_domain_guess(name_raw: str) -> str | None:
    """Tier 1: single-token companies only (e.g., 'Stripe' -> stripe.com).

    Strips company suffixes, checks if result is a single token.
    Multi-word names return None immediately (let Tier 2 handle).
    Reuses _try_slug_heuristic for HEAD probe + parked-domain guard.
    """
    stripped = _strip_company_suffixes(name_raw)
    tokens = stripped.split()
    if len(tokens) != 1:
        return None
    return _try_slug_heuristic(tokens[0])


def _try_slug_heuristic(ats_slug: str) -> str | None:
    """Try https://{ats_slug}.com via HEAD + body validation.

    Returns the final URL (after redirects) if the page is HTML and not a
    parked domain, otherwise None.
    """
    url = f"https://{ats_slug}.com"
    try:
        # Many modern sites block HEAD requests (return 403/405/406/502).
        # Try HEAD first for efficiency, fall back to GET if non-200.
        head_resp = requests.head(url, allow_redirects=True, timeout=_TIMEOUT, headers=_HEADERS)
        if head_resp.status_code == 200:
            content_type = head_resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                logger.debug("Slug heuristic: %s has non-HTML content-type: %s", url, content_type)
                return None
            final_url = head_resp.url
        else:
            # HEAD failed — fall back to GET (many sites only accept GET)
            final_url = None

        # Fetch body to check for parked domain signatures (first 5000 chars)
        get_resp = fetch_with_deadline(url, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS)

        # Bot-blocking codes (403, 405, 406) prove the domain is active —
        # parked domains never return these. Accept the URL directly.
        # But first check the redirect chain didn't land on a domain squatter.
        resolved = final_url or get_resp.url
        if get_resp.status_code in (403, 405, 406):
            if any(sig in resolved.lower() for sig in _PARKED_DOMAIN_SIGNATURES):
                logger.debug("Slug heuristic: %s redirected to parked domain: %s", url, resolved)
                return None
            return resolved

        if get_resp.status_code != 200:
            logger.debug("Slug heuristic: %s GET returned %d", url, get_resp.status_code)
            return None

        body_sample = get_resp.text[:5000].lower()
        for signature in _PARKED_DOMAIN_SIGNATURES:
            if signature in body_sample:
                logger.debug("Slug heuristic: %s appears to be a parked domain", url)
                return None

        # Return final URL after redirects (prefer HEAD redirect chain, fall back to GET)
        return final_url or get_resp.url

    except Exception as e:
        logger.debug("Slug heuristic failed for %s: %s", url, e)
        return None


def _discover_homepage_free_tiers(company_name: str, ats_slug: str | None) -> str | None:
    """Tiers 1/2/2b only: domain guess + slug heuristics. Pure HTTP, zero cost."""
    # Tier 1: Domain guess (single-token names only)
    result = _try_domain_guess(company_name)
    if result is not None:
        return result

    # Tier 2: Slug heuristic — try ats_slug first, then name-derived slug
    if ats_slug is not None:
        result = _try_slug_heuristic(ats_slug)
        if result is not None:
            return result

    # Tier 2b: Name-derived slug fallback (when ats_slug absent or failed)
    name_slug = _name_to_slug(company_name)
    if name_slug and name_slug != (ats_slug or ""):
        result = _try_slug_heuristic(name_slug)
        if result is not None:
            return result

    return None


def discover_homepage(company_name: str, ats_slug: str | None) -> str | None:
    """Auto-discover company homepage URL via Tiers 1/2/2b (mechanical, zero cost).

    # PORT-SEAM: private's four-tier signature (``ats_platform``, ``source_urls``,
    # ``api_key``) is reduced — ``ats_platform``/``source_urls`` were already
    # unused by the private body (kept only for caller convenience), and
    # ``api_key`` gated Tier 4 (SerpAPI, HOLD, dropped). See module docstring.

    Args:
        company_name: Human-readable company name.
        ats_slug: ATS slug to try as domain prefix (e.g. "ramp" -> ramp.com).

    Returns:
        Validated homepage URL string, or None if no tier succeeds.
    """
    return _discover_homepage_free_tiers(company_name, ats_slug)


def _validate_url(url: str) -> str | None:
    """HEAD request to validate URL resolves with 200 and HTML content-type.

    Args:
        url: URL to validate.

    Returns:
        The URL if valid, None otherwise.
    """
    try:
        resp = requests.head(url, allow_redirects=True, timeout=_TIMEOUT, headers=_HEADERS)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            return None
        return url
    except Exception as e:
        logger.debug("URL validation failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Hardened validation gate for guess-derived / structural-only candidates.
#
# Ported standalone/unwired: its sole private caller (Tier 2.5) is not
# ported (see module docstring PORT-SEAM). Kept because it is general-
# purpose reusable infrastructure a future provider-pinned Tier 2.5 port
# would need as-is, matching this unit's "port standalone, unwired"
# precedent (L-0054, run_description_reformat_pass).
#
# _validate_url() (above) only checks live-and-not-parked — no identity
# check. That is fine for Tiers 1/2 (candidates mechanically derived from the
# company's own name). It is NOT fine for a bare parametric-knowledge guess:
# a local model can confidently return a live wrong-company domain, an
# aggregator listing (glassdoor.com/company/acme), or an ATS-hosted board
# (acme.breezy.hr). A wrong homepage_url poisons the downstream chain, which
# is worse than no homepage at all.
#
# _validate_guessed_homepage() is the single hardened gate: fail-closed at
# every step, root-normalizes the candidate, rejects structurally-disallowed
# hosts (ATS registry / blocked domains / DB multi-tenancy) BEFORE any
# content check, and only accepts on positive identity evidence (domain-label
# match or a high-signal on-page field — never an anywhere-in-body match).
# ---------------------------------------------------------------------------


def _registrable_label_and_domain(host: str) -> tuple[str, str]:
    """Split a lowercased hostname into (label, registrable_domain).

    Best-effort without a public-suffix list: takes the last two labels as
    the registrable domain (correct for the overwhelming majority of
    company .com/.io/.co/etc. domains this pipeline deals with) and the
    label immediately before that as the "name" component (e.g.
    "www.acme.com" -> ("acme", "acme.com"); "acme.com" -> ("acme", "acme.com")).
    A bare single-label host returns itself for both.
    """
    parts = host.split(".")
    if len(parts) < 2:
        return host, host
    registrable = ".".join(parts[-2:])
    label = parts[-2]
    return label, registrable


def _host_in_domain_set(host: str, domains: frozenset[str] | set[str]) -> bool:
    """Label-boundary membership check: host == domain or a subdomain of it.

    Same host-boundary contract as domain_policy.is_blocked_domain /
    ats_registry's host matching — never a raw substring.
    """
    return any(host == d or host.endswith("." + d) for d in domains)


def _build_host_census(conn: Any) -> list[tuple[str, int]]:
    """Build a list of (hostname, company_id) tuples from all homepage_url rows.

    Called once per run to avoid O(companies) per-call cost in
    _other_companies_on_host. Snapshot staleness within one run is acceptable
    (the threshold is "≥2 others" — a stale count is conservative).

    Returns a list of (hostname, company_id) tuples for all companies with
    homepage URLs. This is used for efficient subdomain/exact matching in
    _other_companies_on_host.
    """
    host_census: list[tuple[str, int]] = []
    try:
        rows = conn.execute(
            "SELECT id, homepage_url, careers_url FROM companies "
            "WHERE homepage_url IS NOT NULL OR careers_url IS NOT NULL"
        ).fetchall()
    except Exception as e:
        logger.debug("Host census query failed: %s", e)
        return host_census

    for row in rows:
        company_id = row[0]
        for raw_url in (row[1], row[2]):
            if not raw_url:
                continue
            try:
                host = (urlparse(raw_url).hostname or "").lower()
            except Exception:
                continue
            if not host:
                continue

            host_census.append((host, company_id))

    return host_census


def _other_companies_on_host(
    conn: Any,
    host: str,
    exclude_company_id: Any,
    host_census: list[tuple[str, int]] | None = None,
) -> int:
    """Count DISTINCT OTHER companies whose homepage_url resolves to ``host``
    (host-boundary match, never substring).

    Read-only query against the passed-in connection. A company is "on this
    host" if its stored homepage_url's hostname equals ``host`` or is a
    subdomain of it (or vice versa is deliberately NOT checked — we only
    care whether other companies already occupy this exact registrable
    host, not whether they occupy a parent of it).

    When host_census is provided (from _build_host_census), uses the cache
    instead of querying the DB.

    Returns 0 when ``conn`` is None (e.g. a one-shot caller with no DB row
    context) — the DB-backed multi-tenancy signal is simply unavailable in
    that caller; the registry/blocked structural checks and the identity
    gate still apply in full.
    """
    if conn is None:
        return 0

    # Use cache if available (fast path for batch runs)
    if host_census is not None:
        matching_company_ids: set[int] = set()
        for cached_host, company_id in host_census:
            if (
                cached_host == host
                or cached_host.endswith("." + host)
                or host.endswith("." + cached_host)
            ):
                matching_company_ids.add(company_id)

        return len(matching_company_ids - {exclude_company_id})

    # Fallback to DB query (legacy path for one-shot callers without cache)
    try:
        rows = conn.execute(
            "SELECT id, homepage_url, careers_url FROM companies "
            "WHERE (homepage_url IS NOT NULL OR careers_url IS NOT NULL) AND id != ?",
            (exclude_company_id,),
        ).fetchall()
    except Exception as e:
        logger.debug("Multi-tenancy check query failed for host %s: %s", host, e)
        # Fail closed: if we can't check, treat as unknown-but-risky is
        # wrong direction here (would block everything) — the structural
        # checks (registry/blocked) and identity checks still apply, so a
        # query failure just skips this one signal rather than rejecting
        # every candidate. Returning 0 lets identity evidence decide.
        return 0

    seen_company_ids: set = set()
    for row in rows:
        other_id = row[0]
        for other_url in (row[1], row[2]):
            if not other_url:
                continue
            try:
                other_host = (urlparse(other_url).hostname or "").lower()
            except Exception:
                continue
            if not other_host:
                continue
            if (
                other_host == host
                or other_host.endswith("." + host)
                or host.endswith("." + other_host)
            ):
                seen_company_ids.add(other_id)
                break

    return len(seen_company_ids)


class HostCheckResult:
    """Result of a structural/collision host check.

    ``rejected`` means the host is structurally disallowed (ATS, blocked, or
    shared by >= _MULTI_TENANT_THRESHOLD other companies). The caller must
    not accept the URL.

    ``flag_reason`` is set when the host is a non-ATS-registry domain already
    claimed by exactly one other company. The URL itself is *not* rejected;
    the caller may still write it (homepage/careers page resolution is
    factually correct), but the company row must be flagged for review and
    crawling disabled until a human verifies the identity.

    ``__bool__`` returns ``rejected`` for backward compatibility with callers
    that only care about structural rejection.
    """

    __slots__ = ("flag_reason", "rejected")

    def __init__(self, rejected: bool, flag_reason: str | None = None) -> None:
        self.rejected = rejected
        self.flag_reason = flag_reason

    def __bool__(self) -> bool:
        return self.rejected


def _reject_structural_host(
    candidate_url: str,
    conn: Any,
    company_id: Any,
    host_census: list[tuple[str, int]] | None = None,
) -> HostCheckResult:
    """Evaluate ``candidate_url``'s host for structural rejection and
    bespoke single-company collisions.

    The two signals are split:

    * Existing ATS-SaaS / blocked-domain / multi-tenant checks still reject.
    * A non-ATS-registry candidate host already claimed by exactly one other
      company is NOT rejected here, but ``flag_reason`` is set so the write
      path can disable crawling pending human review.

    Fail-closed on unparseable URLs (rejected, not accepted).
    """
    try:
        host = (urlparse(candidate_url).hostname or "").lower()
    except Exception:
        return HostCheckResult(True)
    if not host:
        return HostCheckResult(True)

    if _host_in_domain_set(host, ATS_HOSTED_DOMAINS):
        logger.debug("Structural reject: %s is an ATS-hosted board host", host)
        return HostCheckResult(True)

    if _host_in_domain_set(host, BLOCKED_DOMAINS):
        logger.debug("Structural reject: %s is a domain_policy blocked domain", host)
        return HostCheckResult(True)

    other_count = _other_companies_on_host(conn, host, company_id, host_census)
    if other_count >= _MULTI_TENANT_THRESHOLD:
        logger.debug(
            "Structural reject: %s is shared by %d other companies (multi-tenant)",
            host,
            other_count,
        )
        return HostCheckResult(True)

    if other_count >= 1:
        # Bespoke single-company collision: host is already claimed by one
        # other company and is not a known ATS/shared platform. Don't reject
        # the factual homepage/careers resolution, but signal that crawling
        # should be disabled pending review. Report the registrable domain so
        # the flag reason is stable across www / non-www variants.
        _, registrable = _registrable_label_and_domain(host)
        return HostCheckResult(False, flag_reason=f"bespoke_host_collision:{registrable}")

    return HostCheckResult(False)


def _validate_guessed_homepage(
    candidate_url: str,
    company_name: str,
    conn: Any,
    company_id: Any,
    host_census: list[tuple[str, int]] | None = None,
) -> str | None:
    """Hardened validator for guess-derived homepage candidates.

    Fail-closed at every step:
      1. Root-normalize: parse the candidate, reduce to scheme://host/,
         follow redirects via the existing fetch helpers, and operate on
         the FINAL post-redirect URL. Reuses the parked-domain and
         bot-block (403/405/406) handling semantics from the slug-heuristic
         prober.
      2. Structural rejections (before any content check): ATS-registry
         domain, domain_policy.BLOCKED_DOMAINS, or DB multi-tenancy
         (>= _MULTI_TENANT_THRESHOLD other companies already on this host).
      3. Identity evidence (positive requirement) on the final root page:
         accept only if the suffix-stripped, slugified company name matches
         either the domain label itself, or a high-signal page field
         (<title>, og:site_name, copyright footer) within a bounded HEAD
         slice. Anywhere-in-body matches do NOT count. Weak/ambiguous
         evidence returns None.

    When host_census is provided (from _build_host_census), passes it to
    _reject_structural_host for efficient cache-based lookup.

    Returns the final validated URL, or None if any step fails/rejects.
    """
    try:
        parsed = urlparse(candidate_url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None

    root_url = f"{parsed.scheme}://{parsed.hostname}/"

    try:
        head_resp = requests.head(
            root_url, allow_redirects=True, timeout=_TIMEOUT, headers=_HEADERS
        )
        final_url = head_resp.url if head_resp.status_code == 200 else None

        get_resp = fetch_with_deadline(
            root_url, getter=requests.get, timeout=_TIMEOUT, headers=_HEADERS
        )
        resolved = final_url or get_resp.url

        if get_resp.status_code in (403, 405, 406):
            if any(sig in resolved.lower() for sig in _PARKED_DOMAIN_SIGNATURES):
                logger.debug("Guess validation: %s redirected to parked domain", root_url)
                return None
            # Bot-blocked but live and not parked — no body available to
            # check identity evidence against, so only the domain-label
            # match can satisfy identity for this response shape.
            body_text = ""
        elif get_resp.status_code != 200:
            logger.debug("Guess validation: %s GET returned %d", root_url, get_resp.status_code)
            return None
        else:
            body_sample = get_resp.text[:5000].lower()
            if any(sig in body_sample for sig in _PARKED_DOMAIN_SIGNATURES):
                logger.debug("Guess validation: %s appears to be a parked domain", root_url)
                return None
            body_text = get_resp.text
    except Exception as e:
        logger.debug("Guess validation fetch failed for %s: %s", root_url, e)
        return None

    final_host = (urlparse(resolved).hostname or "").lower()
    if not final_host:
        return None

    # Structural rejections, on the FINAL post-redirect host.
    if _reject_structural_host(resolved, conn, company_id, host_census).rejected:
        return None

    # Identity evidence: domain-label match OR high-signal page field.
    label, _registrable = _registrable_label_and_domain(final_host)
    name_slug = _name_to_slug(company_name)
    if not name_slug:
        logger.debug("Guess validation: company '%s' has no derivable name slug", company_name)
        return None

    # (a) Domain-label equality — how Tiers 1/2 derive identity mechanically.
    # Legitimate for any name, single- or multi-token (makers.com -> "Makers").
    if label == name_slug:
        return resolved

    if body_text:
        anchored_slugs, loose_slugs = _extract_identity_evidence(body_text)
        # (b) Anchored exact-field match (og:site_name value or a title edge
        # segment equals the name). Required for single-token names, allowed
        # for any name.
        if name_slug in anchored_slugs:
            return resolved
        # (c) Loose contiguous-subsequence match against whole title/footer
        # fields — MULTI-TOKEN names only. A single-token needle matches
        # loosely inside any multi-word field ("makers" in "Coffee Makers
        # United"), which is a false accept, so single-token names never take
        # this path (they must satisfy (a) or (b)).
        is_multi_token = "-" in name_slug
        if is_multi_token and any(_slug_has_token_sequence(ev, name_slug) for ev in loose_slugs):
            return resolved

    logger.debug(
        "Guess validation: %s has no identity evidence for company '%s'", resolved, company_name
    )
    return None

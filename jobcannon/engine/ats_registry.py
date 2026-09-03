# PORTED from job_finder/web/ats_registry.py @ b24cf4a6b434f96154144ee087acbae766b4e255 (private job-cannon). Ledger L-0017.
"""Single source of truth for ATS platform capabilities.

Historically, "which platforms can do X" was re-enumerated by hand in ~12
places (the ``_verify_live`` if-ladder, ``_verify_fastpath_live``, ``_PROBES``,
``_FP_PRONE_PLATFORMS``, ``_URL_FASTPATH_PLATFORMS``, ``_RECONCILABLE_PLATFORMS``,
``_PLAYWRIGHT_SCANNERS``, ``NON_SCANNABLE_PLATFORMS``, posting-id patterns, ...).
Adding a platform meant editing all of them, and missing one silently degraded
behaviour with no error — which is exactly how iCIMS / oracle_cloud / ultipro
ended up with working scanners + probes but a ``_verify_live`` that returned
``False`` for them, failing promotion 89% of the time.

This module collapses those facets into ONE :class:`PlatformSpec` per platform.
Every scattered list becomes a comprehension over :data:`PLATFORMS`, and
``tests/test_ats_registry_completeness.py`` turns any future half-wiring into a
CI failure (a scannable platform with no probe, a scanner missing from dispatch,
etc.), exemptable only via an explicit capability flag — never a hardcoded skip.

**Scatter map (all facets now derive from the registry):**
- PR-4 (#650): URL detection metadata (patterns, extractors, specificity)
- PR-5 (#655): Posting-id patterns (expiry_checker, ats_reconciler)
- PR-6 (this PR): Domain facets (ATS_DOMAINS, redirect patterns, PRIORITY_DOMAINS)

The one documented exception: PRIORITY_DOMAINS includes 4 non-ATS job boards
(linkedin.com/jobs, builtin.com, workingnomads.com, ycombinator.com/companies)
that have no PlatformSpec entry by design (they are not ATS platforms). These
are kept as a residual constant in domain_policy.py (_NON_ATS_PRIORITY_DOMAINS)
and appended to the registry-derived ATS portion.

Import layering (acyclic): this module sits ABOVE the leaves it imports
(``ats_platforms``, ``ats_prober``) and BELOW its consumers
(``ats_identity_reconcile``, ``ats_scanner/_probe``, ``ats_reconciler``, ...).
No leaf imports ``ats_registry``.

Probe dispatch resolves the probe function by NAME on the ``ats_prober`` module
at CALL time (``getattr(ats_prober, spec.probe_attr)``) rather than capturing a
reference at import. This preserves the documented test-patch semantics: a test
that monkeypatches ``ats_prober._probe_lever`` still takes effect.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import SplitResult, parse_qs, urlsplit

import jobcannon.engine.ats_prober as _prober
from jobcannon.engine.ats_platforms import PLAYWRIGHT_SCANNERS as _PLAYWRIGHT_SCANNERS
from jobcannon.engine.ats_platforms import SCANNERS_BY_NAME as _REQUESTS_SCANNERS
from jobcannon.engine.ats_platforms._platforms_icims import SCANNER as _ICIMS_SCANNER
from jobcannon.engine.ats_platforms._platforms_icims import PlaywrightPlatformScanner
from jobcannon.engine.ats_platforms._platforms_phenom import SCANNER as _PHENOM_SCANNER
from jobcannon.engine.ats_platforms._platforms_tesla import SCANNER as _TESLA_SCANNER
from jobcannon.engine.ats_platforms._registry import PlatformScanner

# ---------------------------------------------------------------------------
# URL detection patterns (migrated from ats_detection.py)
# ---------------------------------------------------------------------------
_SPECIFICITY_API = 10
_SPECIFICITY_BOARD = 5


def _extract_slug_default(match: re.Match, url: str) -> str:
    """Default slug extractor: return the first capture group, lowercased."""
    return match.group(1).lower()


def _extract_slug_preserve_case(match: re.Match, url: str) -> str:
    """Slug extractor that preserves case (for platforms where case matters)."""
    return match.group(1)


def _extract_slug_ashby(match: re.Match, url: str) -> str:
    """Ashby slug: case-sensitive (no lowercasing)."""
    return match.group(1)  # Preserve case


def _extract_slug_workday_api(match: re.Match, url: str) -> str:
    """Workday API slug: {subdomain}/{board} (middle tenant ignored)."""
    return f"{match.group(1)}/{match.group(2)}"  # tenant + board case preserved (legacy parity)


def _extract_slug_workday_human(match: re.Match, url: str) -> str | None:
    """Workday human slug: {subdomain}/{board}. Skip if URL has /wday/ (API handles those)."""
    if "/wday/" in url.lower():
        return None  # Signal to skip - API pattern should have matched
    return f"{match.group(1)}/{match.group(2)}"  # tenant + board case preserved (legacy parity)


def _extract_slug_ultipro(match: re.Match, url: str) -> str:
    """UltiPro slug: {host}/{tenant}/{board} (tenant case-sensitive)"""
    host = match.group(1).lower()
    tenant = match.group(2)  # case-sensitive
    board = match.group(3).lower()
    return f"{host}/{tenant}/{board}"


def _extract_slug_oracle_cloud(match: re.Match, url: str) -> str:
    """Oracle Cloud slug: {host}|{site} (default CX_1)"""
    host = match.group(1).lower()
    site_match = re.search(r"(?:/sites/|siteNumber=)([A-Za-z0-9_]+)", url, re.IGNORECASE)
    site = site_match.group(1) if site_match else "CX_1"
    return f"{host}|{site}"


def _extract_slug_phenom(match: re.Match, url: str) -> str:
    """Phenom slug: full host from URL"""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.netloc.lower()


def _extract_slug_successfactors(match: re.Match, url: str) -> str:
    """SuccessFactors slug: {host}|{company_id}"""
    host = match.group(1).lower()
    company_id = match.group(2)
    return f"{host}|{company_id}"


def _extract_slug_paylocity(match: re.Match, url: str) -> str:
    """Paylocity slug: GUID (preserve case)"""
    return match.group(1)  # GUID, keep as-is


@dataclass(frozen=True)
class PlatformSpec:
    """One platform's cross-facet capabilities. The dict key in :data:`PLATFORMS`.

    Exactly one fetch transport is set for a scannable platform
    (``requests_scanner`` xor ``playwright_scanner``); keyword adapters set
    neither slug-probe nor URL form and declare ``keyword_adapter=True``.
    """

    name: str
    # FETCH (attached from the scanner registries below)
    requests_scanner: PlatformScanner | None = None
    playwright_scanner: PlaywrightPlatformScanner | None = None
    # LIVENESS — attribute name on ats_prober, resolved via getattr at call time.
    probe_attr: str | None = None
    # LIVENESS (RAISING VARIANT) — attribute name on ats_prober for a probe
    # that returns a status-carrying ``ats_prober.ProbeHttpResult`` (hit +
    # status_code) and lets ``requests.exceptions.Timeout`` / ``ConnectionError``
    # propagate, instead of the ``probe_attr`` contract (always a bare bool,
    # never raises — the shape batch callers require). When set,
    # ``verify_live_detail`` dispatches through THIS function and classifies
    # both the raised exceptions and the result's status code via
    # ``ats_prober._is_transient_error``, so callers like
    # ``probe_single_company`` can route transient failures (Timeout/
    # ConnectionError, or a 429/5xx status) through ``_handle_scan_error``
    # (retry-with-backoff), and a blocked-but-reachable status (401/403) to a
    # distinct ``platform_slug_blocked`` miss — instead of everything
    # non-transient collapsing into a permanent ``platform_slug_404`` miss.
    # Only the platforms that had inline retry-aware HTTP pre-#1928
    # (lever / greenhouse / ashby / smartrecruiters) declare this; platforms
    # whose probe was always exception-swallowing (workday / icims /
    # successfactors / adp / ...) leave it None, and ``verify_live_detail``
    # falls back to the bool ``verify_live`` (returning HIT or MISS, never
    # TRANSIENT/BLOCKED) for them.
    probe_raising_attr: str | None = None
    # BOARD IDENTITY — attribute name on ats_prober for an OPTIONAL probe that
    # returns the board/company's own display name (e.g. Greenhouse's
    # ``GET /v1/boards/{slug}`` -> ``{"name": ...}``), resolved via getattr the
    # same way as probe_attr. Used only by ats_slug_challenge.process_slug_challenge
    # to break identity ties when both the incumbent owner and the challenger are
    # independently name-affine to the slug — name-vs-slug affinity can't separate
    # two companies that both plausibly own it (e.g. "Mercury Insurance Company"
    # and "Mercury" both bind to greenhouse/mercury). None means most platforms:
    # no such endpoint, so a tie stays unresolved (status quo, never demoted).
    identity_probe_attr: str | None = None
    # CAPABILITY FLAGS (each scattered list/ladder derives from one of these)
    fp_prone: bool = False
    speculative_safe: bool = False
    speculative_order: int | None = None
    url_fastpath: bool = False
    reconcilable: bool = False
    non_scannable: bool = False
    keyword_adapter: bool = False
    # POSTING-ID EXTRACTION — regex for extracting stable posting IDs from URLs.
    # Used by expiry_checker (Signal 1 per-posting ATS API) and ats_reconciler
    # (set-diff staleness detection). The pattern must match the URL shape
    # that the platform's single-posting endpoint accepts.
    posting_id_pattern: re.Pattern | None = None
    # DOMAIN FACETS — bare careers-page domains and subdomain-qualified redirect patterns.
    # domains: tuple of bare registrable domains for SENDER/HOSTNAME classification
    #   (e.g. "greenhouse.io", "lever.co"). Feeds ATS_DOMAINS, which is matched
    #   exact-or-suffix against email-sender domains (pipeline_detector._signals /
    #   _off_platform). This is the corporate/notification-host facet — it
    #   INTENTIONALLY EXCLUDES the apply/JD host when that host is a distinct
    #   registrable domain. Workday is the canonical case: its notification hosts are
    #   "workday.com"/"myworkday.com" (here) while its candidate apply host is
    #   "myworkdayjobs.com" (jd_fetch_domain, NOT here). Adding the apply host to
    #   domains would misclassify apply-host senders as ATS relays and break the
    #   ATS_DOMAINS golden baseline — the apply host lives in jd_fetch_domain instead.
    # redirect_domains: tuple of subdomain-qualified patterns for ATS-redirect detection
    #   during careers-page scraping (e.g. "jobs.lever.co", "boards.greenhouse.io").
    # jd_fetch_priority: int rank for JD-fetch ordering (lower = higher priority).
    #   Only set for the 5 ATS platforms in PRIORITY_DOMAINS; None for all others.
    # jd_fetch_domain: the CANONICAL apply/JD-host anchor (the exact string PRIORITY_DOMAINS
    #   ranks for JD-fetch ordering). May differ from bare domains (e.g. "myworkdayjobs.com"
    #   vs "workday.com"). Consumers that need the apply host — not the sender host — must
    #   anchor on THIS, not domains (see migration m205178950._has_pattern_bearing_platform).
    domains: tuple[str, ...] = ()
    redirect_domains: tuple[str, ...] = ()
    jd_fetch_priority: int | None = None
    jd_fetch_domain: str | None = None


# --- The registry. ONE entry per platform; capability flags only here. ---------
# Fetch-scanner objects are attached from the scanner registries afterwards so
# this table stays readable and cannot drift from SCANNERS_BY_NAME.
_SPECS: tuple[PlatformSpec, ...] = (
    # Speculative-ladder platforms (order is load-bearing: fastest JSON first).
    PlatformSpec(
        "lever",
        probe_attr="_probe_lever",
        probe_raising_attr="_probe_lever_with_result",
        speculative_safe=True,
        speculative_order=0,
        url_fastpath=True,
        reconcilable=True,
        posting_id_pattern=re.compile(r"jobs\.lever\.co/[^/]+/([a-f0-9-]+)", re.IGNORECASE),
        domains=("lever.co",),
        redirect_domains=("jobs.lever.co", "api.lever.co"),
        jd_fetch_priority=1,
        jd_fetch_domain="lever.co",
    ),
    PlatformSpec(
        "greenhouse",
        probe_attr="_probe_greenhouse",
        probe_raising_attr="_probe_greenhouse_raising",
        identity_probe_attr="_probe_identity_greenhouse",
        speculative_safe=True,
        speculative_order=1,
        url_fastpath=True,
        reconcilable=True,
        posting_id_pattern=re.compile(r"boards\.greenhouse\.io/[^/]+/jobs/(\d+)", re.IGNORECASE),
        domains=("greenhouse.io", "greenhouse-mail.io"),
        redirect_domains=("boards.greenhouse.io", "boards-api.greenhouse.io"),
        jd_fetch_priority=0,
        jd_fetch_domain="greenhouse.io",
    ),
    PlatformSpec(
        "ashby",
        probe_attr="_probe_ashby",
        probe_raising_attr="_probe_ashby_raising",
        speculative_safe=True,
        speculative_order=2,
        url_fastpath=True,
        reconcilable=True,
        posting_id_pattern=re.compile(r"jobs\.ashbyhq\.com/[^/]+/([a-f0-9-]+)"),
        domains=("ashbyhq.com",),
        redirect_domains=("jobs.ashbyhq.com",),
        jd_fetch_priority=2,
        jd_fetch_domain="ashbyhq.com",
    ),
    PlatformSpec(
        "jazzhr",
        probe_attr="_probe_jazzhr",
        speculative_safe=True,
        speculative_order=3,
        url_fastpath=True,
        domains=("jazz.co",),
    ),
    PlatformSpec(
        "pinpoint",
        probe_attr="_probe_pinpoint",
        speculative_safe=True,
        speculative_order=4,
        url_fastpath=True,
        domains=("pinpointhq.com",),
    ),
    PlatformSpec(
        "teamtailor",
        probe_attr="_probe_teamtailor",
        speculative_safe=True,
        speculative_order=5,
        url_fastpath=True,
    ),
    # Reconcile-only enterprise boards (POST APIs; not speculative-probed).
    PlatformSpec(
        "workday",
        probe_attr="_probe_workday",
        url_fastpath=True,
        reconcilable=True,
        posting_id_pattern=re.compile(
            r"myworkdayjobs\.com/[^?#]*?/([^/?#]+)(?:/?(?:[?#]|$))", re.IGNORECASE
        ),
        domains=("workday.com", "myworkday.com"),
        jd_fetch_priority=3,
        jd_fetch_domain="myworkdayjobs.com",
    ),
    PlatformSpec(
        "smartrecruiters",
        probe_attr="_probe_smartrecruiters",
        probe_raising_attr="_probe_smartrecruiters_raising",
        identity_probe_attr="_probe_identity_smartrecruiters",
        url_fastpath=True,
        reconcilable=True,
        posting_id_pattern=re.compile(
            r"jobs\.smartrecruiters\.com/[^/]+/([A-Za-z0-9_]+)", re.IGNORECASE
        ),
        domains=("smartrecruiters.com",),
        jd_fetch_priority=4,
        jd_fetch_domain="jobs.smartrecruiters.com",
    ),
    # FP-prone: evidence/URL-path promotable only (never speculative-guessed).
    PlatformSpec(
        "bamboohr",
        probe_attr="_probe_bamboohr",
        fp_prone=True,
        url_fastpath=True,
        domains=("bamboohr.com",),
    ),
    PlatformSpec("personio", probe_attr="_probe_personio", fp_prone=True, url_fastpath=True),
    PlatformSpec(
        "recruitee",
        probe_attr="_probe_recruitee",
        fp_prone=True,
        url_fastpath=True,
        domains=("recruitee.com",),
    ),
    PlatformSpec(
        "breezy",
        probe_attr="_probe_breezy",
        fp_prone=True,
        url_fastpath=True,
        domains=("breezy.hr",),
    ),
    # Round-6 URL-fastpath additions.
    PlatformSpec(
        "workable", probe_attr="_probe_workable", url_fastpath=True, domains=("workable.com",)
    ),
    PlatformSpec("paylocity", probe_attr="_probe_paylocity", url_fastpath=True),
    PlatformSpec(
        "rippling", probe_attr="_probe_rippling", url_fastpath=True, domains=("rippling.com",)
    ),
    # Probe exists but reconcile-only (not in the speculative fast-path today).
    PlatformSpec("oracle_cloud", probe_attr="_probe_oracle_cloud"),
    PlatformSpec("ultipro", probe_attr="_probe_ultipro"),
    PlatformSpec("ibm", probe_attr="_probe_ibm"),
    # SuccessFactors — public XML feed, URL-fastpath eligible.
    PlatformSpec(
        "successfactors",
        probe_attr="_probe_successfactors",
        url_fastpath=True,
        reconcilable=True,
        domains=("successfactors.com",),
    ),
    # ADP Workforce Now — public JSON feed, URL-fastpath eligible.
    PlatformSpec("adp", probe_attr="_probe_adp", url_fastpath=True, reconcilable=True),
    # Playwright-fetch (no requests API); promotable via reconcile.
    PlatformSpec(
        "icims",
        playwright_scanner=_ICIMS_SCANNER,
        probe_attr="_probe_icims",
        domains=("icims.com",),
    ),
    # Phenom — Playwright scanner via sitemap, no public JSON API.
    PlatformSpec("phenom", playwright_scanner=_PHENOM_SCANNER, probe_attr="_probe_phenom"),
    # Tesla — Playwright scanner with cua-api interception (anti-bot protected).
    # No ``domains`` entry: unlike ATS-vendor domains (greenhouse.io, icims.com),
    # ``tesla.com`` is Tesla's single-company corporate domain carrying mostly
    # non-careers mail/traffic. ATS_DOMAINS drives email-sender / off-platform
    # classification, so a bare corporate domain there would mislabel every
    # @tesla.com email as ATS-sourced. The scanner dispatches by platform name
    # ("tesla") and the probe hardcodes its careers URL, so neither needs it —
    # matching the Phenom playwright-scanner precedent (also no ``domains``).
    PlatformSpec(
        "tesla",
        playwright_scanner=_TESLA_SCANNER,
        probe_attr="_probe_tesla",
    ),
    # Registered stub with a probe but kept at 'miss' (careers_crawler owns it).
    PlatformSpec(
        "jobvite", probe_attr="_probe_jobvite", non_scannable=True, domains=("jobvite.com",)
    ),
    # Keyword-search adapters: scanner but no slug-probe and no URL form. The
    # explicit capability that exempts them from the scannable-must-have-probe
    # guard (never a hardcoded skip-list).
    PlatformSpec("amazon", keyword_adapter=True),
    PlatformSpec("microsoft", keyword_adapter=True),
    PlatformSpec("eightfold", keyword_adapter=True),
    # Registered stub, no public API (returns []).
    PlatformSpec("google", non_scannable=True),
    # ATS domains with no scanner/probe (email-sender/pipeline-signal matching only).
    # These are in ATS_DOMAINS for domain-based classification but have no live platform entry.
    PlatformSpec("taleo", non_scannable=True, domains=("taleo.net",)),
    PlatformSpec("kronos", non_scannable=True, domains=("kronos.net",)),
    PlatformSpec("modernloop", non_scannable=True, domains=("modernloop.io",)),
    PlatformSpec("governmentjobs", non_scannable=True, domains=("governmentjobs.com",)),
)


def _attach_scanners(specs: tuple[PlatformSpec, ...]) -> dict[str, PlatformSpec]:
    """Bind each spec to its requests-scanner from SCANNERS_BY_NAME (the owner of
    the scanner objects). iCIMS and Phenom have no requests scanner (playwright only)."""
    out: dict[str, PlatformSpec] = {}
    for spec in specs:
        rs = _REQUESTS_SCANNERS.get(spec.name)
        ps = _PLAYWRIGHT_SCANNERS.get(spec.name)
        if rs is not None:
            out[spec.name] = replace(spec, requests_scanner=rs)
        elif ps is not None:
            out[spec.name] = replace(spec, playwright_scanner=ps)
        else:
            out[spec.name] = spec
    return out


PLATFORMS: dict[str, PlatformSpec] = _attach_scanners(_SPECS)


# --- Liveness dispatch (call-time getattr preserves monkeypatch semantics) -----
def _resolve_probe(probe_attr: str):
    return getattr(_prober, probe_attr)


def verify_live(platform: str, slug: str) -> bool:
    """True if ``slug`` resolves to a live board on ``platform``.

    Table lookup into the registry, replacing the former hand-maintained
    if-ladder in ``ats_identity_reconcile``. Returns False for unknown platforms
    or platforms with no probe (keyword adapters / pure stubs).

    The dispatched ``_probe_*`` function swallows ALL exceptions (including
    transient ``Timeout`` / ``ConnectionError``) and returns ``False`` — this
    is the contract batch callers (``verify_fastpath_live``, the speculative
    ladder) rely on so they never raise. Callers that need to distinguish a
    transient network failure from a permanent 404 miss must use
    :func:`verify_live_detail` instead."""
    spec = PLATFORMS.get(platform)
    if spec is None or spec.probe_attr is None:
        return False
    return bool(_resolve_probe(spec.probe_attr)(slug))


class ProbeOutcome(enum.Enum):
    """Four-valued liveness result for :func:`verify_live_detail`.

    ``verify_live`` collapses transient, blocked, and permanent failures into
    ``False`` because its batch callers must not raise. ``verify_live_detail``
    restores those distinctions so retry-aware callers (``probe_single_company``)
    can route:

    - ``TRANSIENT`` (a ``Timeout``/``ConnectionError``, or a 429/5xx status —
      both classified through the single ``ats_prober._is_transient_error``
      chokepoint) through ``_handle_scan_error`` (retry-with-backoff), and
    - ``BLOCKED`` (a non-transient, non-404/410 status, e.g. 401/403 — the
      probe reached a real response but was denied) to a distinct
      ``platform_slug_blocked`` miss reason instead of the generic
      ``platform_slug_404`` — restoring the pre-#1928 diagnostic distinction
      (a slug that doesn't exist vs. one that exists but is blocked) the
      #1928 rework review flagged as lost.
    """

    HIT = "hit"
    MISS = "miss"
    TRANSIENT = "transient"
    BLOCKED = "blocked"


def verify_live_detail(platform: str, slug: str) -> ProbeOutcome:
    """Like :func:`verify_live` but distinguishes transient failures and
    blocked-but-reachable slugs from permanent misses.

    For platforms with a ``probe_raising_attr`` (lever / greenhouse / ashby /
    smartrecruiters — the four that had inline retry-aware HTTP pre-#1928),
    dispatches through the raising variant, which returns an
    ``ats_prober.ProbeHttpResult`` (hit + status_code) rather than a bare
    bool. Both the raised exceptions (Timeout / ConnectionError) AND the
    result's status code are classified through the single
    ``ats_prober._is_transient_error`` chokepoint — there is no second,
    status-blind notion of transience here:

    - ``result.hit`` → :attr:`ProbeOutcome.HIT`.
    - status 200 (non-hit — e.g. an empty postings list) or a status in
      ``ats_prober._PERMANENT_MISS_CODES`` ({404, 410}) → :attr:`ProbeOutcome.MISS`.
    - a transient exception, or a status in ``ats_prober._TRANSIENT_CODES``
      ({429, 500, 502, 503, 504}) → :attr:`ProbeOutcome.TRANSIENT`.
    - any other status (e.g. 401/403 — the probe reached a real response but
      was denied) → :attr:`ProbeOutcome.BLOCKED`.

    Non-transient exceptions (e.g. ``JSONDecodeError`` from a malformed
    response) are NOT classified here; they propagate to the caller, which
    treats them as a generic error (retryable via ``_handle_scan_error``'s
    broad ``except Exception`` path in ``probe_single_company``).

    For platforms WITHOUT a raising variant (workday / icims /
    successfactors / adp / ...), falls back to the bool :func:`verify_live`:
    returns :attr:`ProbeOutcome.HIT` or :attr:`ProbeOutcome.MISS`, never
    :attr:`ProbeOutcome.TRANSIENT`/:attr:`ProbeOutcome.BLOCKED`. These
    platforms' probes always swallowed exceptions pre-#1928 too, so no
    retry-with-backoff regression exists for them — this fallback preserves
    that pre-existing behaviour rather than silently introducing a new
    capability outside this issue's scope.

    Returns :attr:`ProbeOutcome.MISS` for unknown platforms or platforms with
    no probe (keyword adapters / pure stubs), matching :func:`verify_live`."""
    spec = PLATFORMS.get(platform)
    if spec is None or spec.probe_attr is None:
        return ProbeOutcome.MISS
    if spec.probe_raising_attr is not None:
        raising_fn = _resolve_probe(spec.probe_raising_attr)
        try:
            result = raising_fn(slug)
        except Exception as exc:
            if _prober._is_transient_error(exc):
                return ProbeOutcome.TRANSIENT
            raise
        if result.hit:
            return ProbeOutcome.HIT
        if result.status_code == 200 or result.status_code in _prober._PERMANENT_MISS_CODES:
            return ProbeOutcome.MISS
        if _prober._is_transient_error(result.status_code):
            return ProbeOutcome.TRANSIENT
        return ProbeOutcome.BLOCKED
    # No raising variant — fall back to the swallowing bool probe.
    return ProbeOutcome.HIT if verify_live(platform, slug) else ProbeOutcome.MISS


def verify_fastpath_live(platform: str, slug: str) -> bool:
    """Liveness gate for the speculative prober's B2 URL-evidence fast-path.

    Same dispatch as :func:`verify_live` but gated on ``url_fastpath`` so only
    the audited fast-path set is verifiable here."""
    spec = PLATFORMS.get(platform)
    if spec is None or not spec.url_fastpath or spec.probe_attr is None:
        return False
    return bool(_resolve_probe(spec.probe_attr)(slug))


def probe_board_identity(platform: str, slug: str) -> str | None:
    """Best-effort fetch of the board's own display name from its ATS API.

    Table lookup mirroring :func:`verify_live`'s dispatch, but for the (few)
    platforms that expose a board/company display name distinct from the slug
    string (see ``PlatformSpec.identity_probe_attr``). Returns None for unknown
    platforms, platforms with no identity probe registered, or any probe
    failure — the caller (``ats_slug_challenge.process_slug_challenge``) treats
    None as "tie unresolved", never as a verdict against either party.
    """
    spec = PLATFORMS.get(platform)
    if spec is None or spec.identity_probe_attr is None:
        return None
    return _resolve_probe(spec.identity_probe_attr)(slug)


# --- Derived views (single source for every formerly-hand-maintained list) -----
SCANNERS_BY_NAME: dict[str, PlatformScanner] = {
    n: s.requests_scanner for n, s in PLATFORMS.items() if s.requests_scanner is not None
}
PLAYWRIGHT_SCANNERS: dict[str, PlaywrightPlatformScanner] = {
    n: s.playwright_scanner for n, s in PLATFORMS.items() if s.playwright_scanner is not None
}
PLAYWRIGHT_PLATFORMS: frozenset[str] = frozenset(PLAYWRIGHT_SCANNERS)
NON_SCANNABLE_PLATFORMS: frozenset[str] = frozenset(
    n for n, s in PLATFORMS.items() if s.non_scannable
)
FP_PRONE_PLATFORMS: frozenset[str] = frozenset(n for n, s in PLATFORMS.items() if s.fp_prone)
URL_FASTPATH_PLATFORMS: frozenset[str] = frozenset(
    n for n, s in PLATFORMS.items() if s.url_fastpath
)
RECONCILABLE_PLATFORMS: frozenset[str] = frozenset(
    n for n, s in PLATFORMS.items() if s.reconcilable
)
KEYWORD_ADAPTER_PLATFORMS: frozenset[str] = frozenset(
    n for n, s in PLATFORMS.items() if s.keyword_adapter
)
# Platforms whose URLs carry a stable posting id extractable via a regex
# pattern (``spec.posting_id_pattern is not None``) — the "direct posting-id
# extraction" capability. This is a STRICT subset of RECONCILABLE_PLATFORMS:
# successfactors/adp are reconcilable via batch set-diff but expose no
# single-posting URL shape, so they have no posting_id_pattern. Consumers that
# need "platforms with a direct posting-id URL shape" (e.g. the M1 latency
# metric's ``ats_direct`` cohort in scripts/ats_rectification_metrics.py) must
# anchor on THIS view, not RECONCILABLE_PLATFORMS — the two diverge by design.
POSTING_ID_PLATFORMS: frozenset[str] = frozenset(
    n for n, s in PLATFORMS.items() if s.posting_id_pattern is not None
)

# URL detection ordering: load-bearing flat list of (platform, pattern, specificity, extractor)
# for extract_ats_from_url_best. Replaces the hand-maintained if-ladder in ats_detection.py.
# The order is byte-for-byte preserved by the parity test.
_URL_DETECTION_PATTERNS: list[
    tuple[str, re.Pattern, int, Callable[[re.Match, str], str | None]]
] = [
    # Order 0: Lever API
    (
        "lever",
        re.compile(r"https?://api\.lever\.co/v0/postings/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_API,
        _extract_slug_preserve_case,
    ),
    # Order 1: Greenhouse API
    (
        "greenhouse",
        re.compile(r"https?://boards-api\.greenhouse\.io/v1/boards/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_API,
        _extract_slug_preserve_case,
    ),
    # Order 2: Workday API
    (
        "workday",
        re.compile(
            r"https?://([^/]+)\.myworkdayjobs\.com/wday/cxs/[^/]+/([^/?#]+)", re.IGNORECASE
        ),  # PORT-SEAM: ruff line-length 100 (public) vs 99 (private) wraps this differently; pure reformat
        _SPECIFICITY_API,
        _extract_slug_workday_api,
    ),
    # Order 3: SmartRecruiters API
    (
        "smartrecruiters",
        re.compile(r"https?://api\.smartrecruiters\.com/v1/companies/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_API,
        _extract_slug_preserve_case,
    ),
    # Order 4: Lever board
    (
        "lever",
        re.compile(r"https?://jobs\.lever\.co/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_preserve_case,
    ),
    # Order 5: Greenhouse board
    (
        "greenhouse",
        re.compile(r"https?://(?:job-)?boards\.greenhouse\.io/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_preserve_case,
    ),
    # Order 6: Ashby (case-sensitive)
    (
        "ashby",
        re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#]+)"),
        _SPECIFICITY_BOARD,
        _extract_slug_ashby,
    ),
    # Order 7: Workday human (with /wday/ skip)
    (
        "workday",
        re.compile(r"https?://([^/]+)\.myworkdayjobs\.com/(?:en-US/)?([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_workday_human,
    ),
    # Order 8: SmartRecruiters board
    (
        "smartrecruiters",
        re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_preserve_case,
    ),
    # Order 9: Recruitee
    (
        "recruitee",
        re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 10: Breezy
    (
        "breezy",
        re.compile(r"https?://([a-z0-9-]+)\.breezy\.hr", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 11: JazzHR
    (
        "jazzhr",
        re.compile(r"https?://([a-z0-9-]+)\.applytojob\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 12: Pinpoint
    (
        "pinpoint",
        re.compile(r"https?://([a-z0-9-]+)\.pinpointhq\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 13: Personio
    (
        "personio",
        re.compile(r"https?://([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 14: BambooHR
    (
        "bamboohr",
        re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 15: Teamtailor
    (
        "teamtailor",
        re.compile(r"https?://([a-z0-9-]+)\.teamtailor\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 16: Workable
    (
        "workable",
        re.compile(r"https?://apply\.workable\.com/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 17: Jobvite
    (
        "jobvite",
        re.compile(r"https?://jobs\.jobvite\.com/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 18: Paylocity
    (
        "paylocity",
        re.compile(
            r"https?://(?:[^/]*)recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/All/([0-9a-f-]{36})",
            re.IGNORECASE,
        ),
        _SPECIFICITY_BOARD,
        _extract_slug_paylocity,
    ),
    # Order 19: Rippling
    (
        "rippling",
        re.compile(r"https?://ats\.rippling\.com/([^/?#]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 20: UltiPro
    (
        "ultipro",
        re.compile(
            r"https?://(recruiting\d*\.ultipro\.com)/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})",
            re.IGNORECASE,
        ),
        _SPECIFICITY_BOARD,
        _extract_slug_ultipro,
    ),
    # Order 21: Oracle Cloud
    (
        "oracle_cloud",
        re.compile(
            r"https?://([a-z0-9][a-z0-9-]*\.fa\.[a-z0-9-]+\.oraclecloud\.com)", re.IGNORECASE
        ),
        _SPECIFICITY_BOARD,
        _extract_slug_oracle_cloud,
    ),
    # Order 22: iCIMS
    (
        "icims",
        re.compile(r"https?://(?:careers|jobs)-([a-z0-9][a-z0-9-]*)\.icims\.com", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
    # Order 23: SAP SuccessFactors demo (returns None) - must be before Phenom
    (
        "successfactors",
        re.compile(r"https?://(?:sapsfdemojobs\.com|jobs\.hr\.cloud\.sap\.com)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        lambda m, u: None,
    ),
    # Order 24: Phenom
    (
        "phenom",
        re.compile(r"https?://(?:careers|jobs)\.([a-z0-9.-]+)", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_phenom,
    ),
    # Order 25: SuccessFactors
    (
        "successfactors",
        re.compile(
            r"https?://(career\d*\.successfactors\.(?:com|eu))\b.*company=([^&]+)", re.IGNORECASE
        ),
        _SPECIFICITY_BOARD,
        _extract_slug_successfactors,
    ),
    # Order 26: ADP
    (
        "adp",
        re.compile(r"https?://workforcenow\.adp\.com/.*[?&]cid=([0-9a-fA-F-]{36})", re.IGNORECASE),
        _SPECIFICITY_BOARD,
        _extract_slug_default,
    ),
]

# Extractors whose match evidence is host-shape only (currently just Phenom's
# catch-all `careers.<domain>` / `jobs.<domain>` pattern, order 24 above): the
# URL's host merely LOOKS like a careers subdomain, with no vendor-specific
# host token verified (contrast e.g. Order 14 BambooHR, which requires the
# literal `.bamboohr.com` suffix). Referenced by extractor-function identity,
# not by platform-name string, so membership can only grow via an explicit
# opt-in here -- never a silent trust bump for a future equally-weak pattern
# (#1899: `careers.playstation.com` matched this catch-all and produced a
# `set` proposal despite the page actually linking to Greenhouse).
_HOST_SHAPE_ONLY_EXTRACTORS: frozenset[Callable[[re.Match, str], str | None]] = frozenset(
    {_extract_slug_phenom}
)


def resolve_url_match(url: str) -> tuple[str, str, int, bool] | None:
    """Full-metadata single-URL ATS match: (platform, slug, specificity, host_shape_only).

    Single point of truth for walking :data:`_URL_DETECTION_PATTERNS` --
    ``ats_detection.extract_ats_from_url_best`` (the public 3-tuple contract)
    and :func:`url_match_is_host_shape_only` both delegate here so "which
    pattern won" is decided exactly once.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    for platform, pattern, specificity, extractor in _URL_DETECTION_PATTERNS:
        match = pattern.search(url)
        if match:
            slug = extractor(match, url)
            if slug is None:
                # Special marker: if extractor returns None and platform is
                # successfactors, this is a demo/internal URL -> stop matching.
                if platform == "successfactors":
                    return None
                continue  # Skip this match (e.g., workday /wday/ path)
            return platform, slug, specificity, extractor in _HOST_SHAPE_ONLY_EXTRACTORS
    return None


def url_match_is_host_shape_only(url: str) -> bool:
    """True if ``url``'s winning ATS match came from a host-shape-only pattern.

    See :data:`_HOST_SHAPE_ONLY_EXTRACTORS`. Used by
    ``scripts/ats_identity_adjudicate.py`` to cap a proposal at ``review``
    instead of ``set`` when the only URL evidence is a generic
    careers-subdomain shape rather than a vendor-specific host token (#1899).
    """
    match = resolve_url_match(url)
    return bool(match) and match[3]


# Speculative ladder: ordered (platform, probe_fn) pairs, fastest first. Probe
# refs captured here (import-time) match the prior _PROBES behaviour exactly.
SPECULATIVE_PROBES: list[tuple[str, object]] = [
    (s.name, _resolve_probe(s.probe_attr))
    for s in sorted(
        (s for s in PLATFORMS.values() if s.speculative_safe and s.probe_attr is not None),
        key=lambda s: s.speculative_order if s.speculative_order is not None else 1_000,
    )
]

# The scannable population the completeness guard reasons over: anything with a
# fetch transport (requests or playwright).
SCANNABLE_PLATFORMS: frozenset[str] = frozenset(
    n
    for n, s in PLATFORMS.items()
    if s.requests_scanner is not None or s.playwright_scanner is not None
)

# Promotion-target population for careers-link discovery: every scannable
# platform except the non-scannable stubs (jobvite/google). Replaces
# _ats_link_discovery's hand-rolled
# ``_TARGET_PLATFORMS = (SCANNERS_BY_NAME - NON_SCANNABLE) | {icims}`` — a
# careers link to a platform in this set is promotable; one to a stub is not.
SCANNABLE_TARGET_PLATFORMS: frozenset[str] = SCANNABLE_PLATFORMS - NON_SCANNABLE_PLATFORMS

# ---------------------------------------------------------------------------
# Greenhouse posting-ID extraction — SINGLE SOURCE OF TRUTH.
#
# Greenhouse posting URLs appear in several real-world shapes, all carrying the
# same stable numeric posting id:
#   https://boards.greenhouse.io/<slug>/jobs/<id>              canonical
#   https://job-boards.greenhouse.io/<slug>/jobs/<id>          newer host
#   https://job-boards.eu.greenhouse.io/<slug>/jobs/<id>       EU data region
#   https://<company>.com/careers/job/<id>?gh_jid=<id>         self-hosted redirect
#   https://boards.greenhouse.io/embed/job_app?...&token=<id>  embed flow
# ``spec.posting_id_pattern`` for greenhouse matches only the canonical/host PATH
# shape — it is the domain-anchored *shape* pattern used for id-shape validation
# and platform detection. FULL extraction (incl. the gh_jid self-hosted-redirect
# and embed shapes) MUST route through extract_greenhouse_posting_id() so that
# ats_reconciler, expiry_checker, and the backfill migration cannot diverge.
# (Previously each carried its own copy and only the reconciler's was complete —
# custom-domain and EU-host greenhouse postings silently failed to resolve, #644.)
#
# Two shape tiers:
#   DISCRIMINATING — a greenhouse.io host+path, or the branded gh_jid= query param.
#     Each uniquely identifies greenhouse, so these are safe for platform DETECTION on
#     an otherwise-unknown URL. Matching is bound to PARSED URL COMPONENTS (the real
#     hostname / path / query keys via urlsplit + parse_qs), never a substring of the
#     raw URL — so neither a crafted host (``greenhouse.io.evil.com``, a subdomain of
#     evil.com) nor an embedded value (``evil.com/p?redir=greenhouse.io/x/jobs/1``) can
#     be misdetected as greenhouse. A raw ``re.search`` over the whole URL would accept
#     both (the ``py/incomplete-url-substring-sanitization`` defect class).
#   EMBED (token=) — a bare, generic param; only trustworthy once the platform is
#     already known to be greenhouse, so it is excluded from the detection tier.
_GREENHOUSE_HOST = "greenhouse.io"
# Path shape on a greenhouse host: /<board-slug>/jobs/<numeric-id>. Applied to the
# parsed URL PATH only, so it can match nothing but the real path component.
_GREENHOUSE_PATH_ID_RE: re.Pattern = re.compile(r"/[^/]+/jobs/(\d+)", re.IGNORECASE)
# Leading-digit capture for a branded query param's value (gh_jid / token), preserving
# the ``(\d+)`` capture semantics of the prior ``[?&]key=(\d+)`` extraction.
_LEADING_DIGITS_RE: re.Pattern = re.compile(r"(\d+)")


def _split_url(url: str) -> SplitResult | None:
    """``urlsplit(url)``, transparently recovering a scheme-less ``host/path`` string
    (e.g. ``boards.greenhouse.io/x/jobs/1``) so its host lands in ``netloc`` rather than
    ``path``. Returns None if the URL cannot be parsed."""
    try:
        parts = urlsplit(url)
        if not parts.hostname and "//" not in url[:8]:
            parts = urlsplit("//" + url)
        return parts
    except ValueError:
        return None


def _is_greenhouse_host(host: str) -> bool:
    """True iff ``host`` is ``greenhouse.io`` or a subdomain of it (host-boundary match,
    so ``greenhouse.io.evil.com`` — a subdomain of evil.com — is correctly rejected)."""
    return host == _GREENHOUSE_HOST or host.endswith("." + _GREENHOUSE_HOST)


def _greenhouse_host_path_id(parts: SplitResult) -> str | None:
    """Posting id from a greenhouse-host URL's ``/<slug>/jobs/<id>`` path, else None."""
    if not _is_greenhouse_host((parts.hostname or "").lower()):
        return None
    match = _GREENHOUSE_PATH_ID_RE.search(parts.path)
    return match.group(1) if match else None


def _branded_query_id(query: str, key: str) -> str | None:
    """Leading-digit id from a REAL ``key`` query parameter. ``parse_qs`` binds ``key`` to
    an actual ``&key=`` pair, so a value that merely embeds ``gh_jid=`` / ``token=`` inside
    another parameter (a redirect/tracking wrapper) is never mistaken for the param."""
    values = parse_qs(query).get(key)
    if not values:
        return None
    match = _LEADING_DIGITS_RE.match(values[0])
    return match.group(1) if match else None


def detect_greenhouse_posting_id(url: str) -> str | None:
    """Platform-UNKNOWN detection: return the greenhouse posting id iff ``url`` carries a
    DISCRIMINATING greenhouse signal — a ``greenhouse.io`` host with a ``/jobs/<id>`` path,
    or the branded ``gh_jid=<id>`` query param.

    Excludes the generic embed ``token=`` shape, so a non-Greenhouse URL that happens to
    carry a stray ``token=`` param is never misdetected as greenhouse. Matching is bound to
    parsed URL components (host / path / query key), so a greenhouse-looking substring
    embedded in an unrelated URL cannot false-match. Use this (not the ``extract_*``
    variant) whenever the platform is not yet known — e.g. the backfill migration deciding
    which platform a stored URL belongs to.
    """
    if not url:
        return None
    parts = _split_url(url)
    if parts is None:
        return None
    return _greenhouse_host_path_id(parts) or _branded_query_id(parts.query, "gh_jid")


def extract_greenhouse_posting_id(url: str) -> str | None:
    """Extract the stable numeric Greenhouse posting id from any real-world URL shape.

    Tries the canonical/host PATH first, then the ``gh_jid=`` self-hosted-redirect
    param, then the embed ``token=`` param (ordered so the path id wins when a URL
    carries both). Matching is component-anchored (host / path / query key), never a raw
    substring. Returns None when no shape matches.

    CALLER CONTRACT: the ``token=`` shape is generic and would false-match a
    non-Greenhouse tracking param, so this variant is for callers that ALREADY know
    the platform is greenhouse (ats_reconciler, expiry_checker). Callers resolving a
    platform-unknown URL must use ``detect_greenhouse_posting_id`` instead.
    """
    if not url:
        return None
    parts = _split_url(url)
    if parts is None:
        return None
    return (
        _greenhouse_host_path_id(parts)
        or _branded_query_id(parts.query, "gh_jid")
        or _branded_query_id(parts.query, "token")
    )


# Posting-ID extraction patterns for ats_reconciler (set-diff staleness detection).
# Greenhouse is excluded here — it routes through extract_greenhouse_posting_id()
# (the multi-shape single source of truth) rather than a single dict pattern.
# Workday and SmartRecruiters are included but use completeness-gated paths in
# reconcile_company.
RECONCILER_POSTING_ID_PATTERNS: dict[str, re.Pattern] = {
    n: s.posting_id_pattern
    for n, s in PLATFORMS.items()
    if s.posting_id_pattern is not None and n in {"lever", "ashby", "workday", "smartrecruiters"}
}

# Posting-ID extraction patterns for expiry_checker (Signal 1 per-posting ATS API).
# Covers the platforms whose APIs accept a posting-id lookup via a single dict
# pattern. Greenhouse is excluded here — like the reconciler, expiry_checker routes
# it through extract_greenhouse_posting_id() so custom-domain (gh_jid) and EU-host
# postings resolve. Workday and SmartRecruiters don't expose equivalent
# single-posting endpoints; they rely on Phase B batch reconciliation via
# ats_reconciler (per expiry_checker docstring).
EXPIRY_CHECKER_POSTING_ID_PATTERNS: dict[str, re.Pattern] = {
    n: s.posting_id_pattern
    for n, s in PLATFORMS.items()
    if s.posting_id_pattern is not None and n in {"lever", "ashby"}
}

# Domain facets derived from the registry (replaces legacy literals in
# pipeline_detector._constants, careers_scraper, and domain_policy).
ATS_DOMAINS: frozenset[str] = frozenset(
    domain for spec in PLATFORMS.values() for domain in spec.domains
)

REDIRECT_DOMAINS: tuple[str, ...] = tuple(
    domain for spec in PLATFORMS.values() for domain in spec.redirect_domains
)

# ATS-HOSTED host families — the "is this candidate an ATS-hosted board rather
# than a company homepage" set. Unions EVERY ATS-host facet from the registry:
#   - domains: the sender/hostname classification set (e.g. "greenhouse.io")
#   - redirect_domains: subdomain-qualified careers-redirect hosts
#     (e.g. "boards.greenhouse.io", "jobs.lever.co")
#   - jd_fetch_domain: the canonical apply/JD host, which for some platforms is
#     a DISTINCT registrable domain deliberately kept OUT of ``domains`` so it
#     doesn't pollute email-sender/off-platform classification — Workday's
#     "myworkdayjobs.com" is the canonical case (see the PlatformSpec.domains
#     docstring). That exclusion is correct for ATS_DOMAINS' consumers
#     (pipeline_detector._off_platform / _signals), but a homepage-candidate
#     structural gate MUST reject the apply host too, or an ATS board like
#     "acmeco.wd5.myworkdayjobs.com" sails through as a "homepage".
#
# Consumers that need "is this an ATS-hosted board host?" (e.g. the
# guessed-homepage / Tier-3 / Tier-4 structural gate in homepage_discoverer)
# match a candidate host against THIS set on a label boundary — never a raw
# substring. Kept separate from ATS_DOMAINS so widening the ATS-board view here
# cannot leak into ATS_DOMAINS' email-sender consumers.
ATS_HOSTED_DOMAINS: frozenset[str] = frozenset(
    {domain for spec in PLATFORMS.values() for domain in spec.domains}
    | {domain for spec in PLATFORMS.values() for domain in spec.redirect_domains}
    | {spec.jd_fetch_domain for spec in PLATFORMS.values() if spec.jd_fetch_domain is not None}
)

# PRIORITY_DOMAINS derived from registry (ATS platforms by jd_fetch_priority order,
# plus non-ATS job boards that have no PlatformSpec entry).
# The non-ATS residual is kept in domain_policy.py as _NON_ATS_PRIORITY_DOMAINS.
PRIORITY_DOMAINS_ATS: list[str] = [
    spec.jd_fetch_domain
    for spec in sorted(
        (s for s in PLATFORMS.values() if s.jd_fetch_priority is not None),
        key=lambda s: s.jd_fetch_priority,  # type: ignore
    )
]


# ---------------------------------------------------------------------------
# Direct ATS platform predicate
# ---------------------------------------------------------------------------


def is_direct_ats_platform(platform_key: str) -> bool:
    """True iff ``platform_key`` is a direct ATS platform (not a keyword adapter).

    A source is a direct ATS platform iff its ``PlatformSpec`` has a real scanner
    (``requests_scanner`` or ``playwright_scanner``), is not a keyword adapter,
    and is not marked ``non_scannable``. This is equivalent to
    ``SCANNABLE_TARGET_PLATFORMS - KEYWORD_ADAPTER_PLATFORMS``.

    This predicate is used by the posting sub-entity upsert logic (#640) to
    determine which sightings should mint a posting descriptor. Keyword adapters
    (Amazon, Microsoft, Eightfold) and non-scannable stubs (jobvite, google,
    taleo, etc.) do NOT mint postings.

    Args:
        platform_key: The lowercase platform key (e.g. "ashby", "lever", "greenhouse").

    Returns:
        True if the platform is a direct ATS platform, False otherwise.
    """
    return (
        platform_key in SCANNABLE_TARGET_PLATFORMS
        and platform_key
        not in KEYWORD_ADAPTER_PLATFORMS  # PORT-SEAM: ruff line-length 100 vs 99 wraps this differently; pure reformat
    )

# PORTED from job_finder/web/ats_detection.py @ 9880aa404c8adde81d769cb84a2e8ba1f3c9a043 (private job-cannon). Ledger L-0012.
"""ATS URL pattern extraction and slug candidate derivation."""

import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Relative pattern strength within a URL: API/canonical traces win ties in reconciliation.
_SPECIFICITY_API = 10
_SPECIFICITY_BOARD = 5

# Version bump for PR-4: URL patterns migrated to ats_registry
# Previous: m049-v11
ATS_EXTRACTOR_VERSION = "m050-v1"


def extract_ats_from_url_best(url: str) -> tuple[str, str, int] | None:
    """Pick the strongest ATS match for a single URL.

    Prefer API/boards-api/hosted integrations over human career paths so
    tied aggregate votes break toward canonical postings feeds.

    Returns:
        Tuple (platform, slug, specificity_weight) or None.
    """
    from jobcannon.engine.ats_registry import resolve_url_match

    match = resolve_url_match(url)
    return None if match is None else match[:3]


def probe_hit_consistent_with_careers_url(hit_platform: str, careers_url: str | None) -> bool:
    """F6 — gate a speculative-probe hit against an existing careers_url.

    The speculative probe loop derives slug candidates from `name_raw` and
    hits the first ATS API that returns non-empty postings for any candidate.
    For famous-brand short slugs that collide with a small startup on a
    small ATS, this produces a false positive (e.g. 'Shopify' → Pinpoint
    tenant `shopify.pinpointhq.com`, which is a DIFFERENT company).

    This guard rejects a hit when the company already has a `careers_url`
    that positively identifies a DIFFERENT ATS platform. It does NOT catch
    cases where `careers_url` carries no ATS signature (e.g.
    `shopify.com/careers`) — that requires fetching the page and parsing for
    embedded-widget signatures, deferred to a future F7 (wide F6).

    Returns:
        True if the hit is consistent (URL infers same platform OR carries
        no ATS signature OR is absent). False only when the URL infers a
        different platform than `hit_platform`.
    """
    if not careers_url:
        return True
    inferred = extract_ats_from_url_best(careers_url)
    if inferred is None:
        return True
    return inferred[0] == hit_platform


def _default_http_get(url: str, timeout: float) -> Any:
    """Default GET used by `careers_url_is_live`. Lazy-imports `requests` so the
    pure helpers above remain importable in environments without network deps.
    """
    import requests

    from jobcannon.engine._http_constants import _HEADERS
    from jobcannon.engine.http_fetch import fetch_with_deadline

    return fetch_with_deadline(
        url, getter=requests.get, headers=_HEADERS, timeout=timeout, allow_redirects=True
    )


def careers_url_is_live(
    url: str | None,
    *,
    timeout: float = 5.0,
    _get: Callable[[str, float], Any] | None = None,
) -> bool | None:
    """Best-effort liveness probe for a careers URL.

    F6's pure helper (above) treats `careers_url`-inferred ATS as authoritative.
    That breaks when a company has migrated ATS platforms: the old careers_url
    still parses to its old ATS, but the live probe correctly rediscovers the
    new one. This helper lets the composite gate distinguish:

    Returns:
        True  — URL responded 2xx (careers_url is current — trust it).
        False — URL responded 404 or 410 (careers_url is stale — trust the probe).
        None  — ambiguous (timeout, 5xx, 403, network error, missing URL). Caller
                should treat as "couldn't determine" and preserve the conservative
                gate behavior (do not let the probe hit override a live-looking
                careers_url).

    `_get` is injected for testability; defaults to `_default_http_get`.
    """
    if not url:
        return None
    get = _get if _get is not None else _default_http_get
    try:
        resp = get(url, timeout)
    except Exception as exc:
        logger.debug("careers_url_is_live: %s raised %s — returning None", url, exc)
        return None
    status = getattr(resp, "status_code", None)
    if status is None:
        return None
    if 200 <= status < 300:
        return True
    if status in (404, 410):
        return False
    return None


def probe_hit_consistent_or_dead_url(
    hit_platform: str,
    careers_url: str | None,
    *,
    liveness_check: Callable[[str | None], bool | None] | None = None,
) -> bool:
    """F6 augmented — gate a probe hit, override rejection when careers_url is dead.

    Composes `probe_hit_consistent_with_careers_url` with `careers_url_is_live`:

    - If the pure helper accepts (no URL, no signature, or matching platform),
      return True immediately — no network call.
    - If the pure helper rejects (URL infers a *different* platform), probe the
      URL for liveness. A 404/410 means careers_url is stale (likely the
      company migrated ATS platforms) and the live probe hit is preferred —
      return True. Live or ambiguous (timeout/5xx/403/etc.) preserves the
      rejection — return False.

    This is the call-site gate for F4-resume and the scheduler's speculative
    probe loop. The brand-collision false positive (Shopify → Pinpoint with a
    live `jobs.lever.co/shopify` URL — hypothetical) is still caught. The
    migration false rejection (NimbleAI moved Lever→Greenhouse, old Lever URL
    404s) no longer fires.

    `liveness_check` is injected for testability; defaults to
    `careers_url_is_live` (which itself performs the HTTP request).
    """
    if probe_hit_consistent_with_careers_url(hit_platform, careers_url):
        return True
    check = liveness_check if liveness_check is not None else careers_url_is_live
    # careers_url disagrees with hit; accept the hit only if URL is provably dead.
    return check(careers_url) is False


def extract_ats_from_urls(source_urls: list[str]) -> tuple[str | None, str | None]:
    """Extract ATS platform and slug from a list of job source URLs.

    Checks each URL against Lever, Greenhouse, and Ashby patterns.
    Returns on first match. Ashby slug preserves exact URL casing
    (per Research Pitfall 3 — Ashby slugs are case-sensitive).

    Args:
        source_urls: List of URL strings from a job record's source_urls field.

    Returns:
        Tuple of (platform, slug). Platform is lever, greenhouse, ashby,
        workday, or smartrecruiters when matched. ``(None, None)`` if none.
    """
    for url in source_urls:
        hit = extract_ats_from_url_best(url)
        if hit:
            return hit[0], hit[1]
    return None, None


def aggregate_ats_candidates_from_job_bundles(
    job_bundles: list[dict],
) -> tuple[tuple[str, str] | None, str | None]:
    """Choose a single (platform, slug) from per-job URL lists.

    Aggregate by distinct jobs (dedup_key), not duplicate URL rows. Prefer
    more jobs supporting the same board, then stronger URL specificity, then
    more recent ``last_seen`` as a lexical tie-break. If the top two
    candidates tie on all three dimensions, abstain (no silent guess).

    Args:
        job_bundles: Each item has ``dedup_key`` (str), ``last_seen`` (str|None),
            and ``urls`` (list[str]).

    Returns:
        ``((platform, slug), None)`` on success, or ``(None, reason)`` when
        there is no evidence or confidence is insufficient.
    """
    # (platform, slug) -> job_count, max_spec_sum (sum of per-job best spec), last_seen_max
    primary: dict[tuple[str, str], dict[str, int | str]] = {}

    for bundle in job_bundles:
        dk = bundle.get("dedup_key")
        urls = bundle.get("urls") or []
        if not dk or not urls:
            continue
        last_seen = bundle.get("last_seen") or ""

        per_job_best: dict[tuple[str, str], int] = {}
        for url in urls:
            hit = extract_ats_from_url_best(url)
            if not hit:
                continue
            plat, slug, spec = hit
            key = (plat, slug)
            prev = per_job_best.get(key, 0)
            if spec > prev:
                per_job_best[key] = spec

        for key, spec in per_job_best.items():
            bucket = primary.setdefault(
                key,
                {"job_count": 0, "spec_sum": 0, "last_seen_max": ""},
            )
            bucket["job_count"] = int(bucket["job_count"]) + 1
            bucket["spec_sum"] = int(bucket["spec_sum"]) + int(spec)
            ls = str(bucket["last_seen_max"])
            if last_seen > ls:
                bucket["last_seen_max"] = last_seen

    if not primary:
        return None, "no_ats_urls"

    ranked = sorted(
        primary.items(),
        key=lambda item: (
            item[1]["job_count"],
            item[1]["spec_sum"],
            item[1]["last_seen_max"],
        ),
        reverse=True,
    )

    winner_key, winner_stats = ranked[0]
    if len(ranked) > 1:
        _, second_stats = ranked[1]
        w = (winner_stats["job_count"], winner_stats["spec_sum"], winner_stats["last_seen_max"])
        s = (second_stats["job_count"], second_stats["spec_sum"], second_stats["last_seen_max"])
        if w == s:
            return None, "ambiguous_tie"

    return winner_key, None


def derive_slug_candidates(company_name: str) -> list[str]:
    """Generate ATS slug candidates from a company name.

    Produces hyphenated and concatenated variants after stripping common
    legal suffixes. Used by probe_ats_slugs for speculative probing.

    Examples:
        "Scale AI" -> ["scale-ai", "scaleai"]
        "Stripe, Inc." -> ["stripe"]
        "OpenAI" -> ["openai"]

    Args:
        company_name: Raw company name string.

    Returns:
        List of slug candidate strings (lowercase). At least one candidate.
    """
    # Normalize: lowercase, strip legal suffixes
    name = company_name.lower()
    # Strip common suffixes (inc, llc, corp, ltd, co, company)
    name = re.sub(
        r"[,\s]+(inc\.?|llc\.?|corp\.?|corporation\.?|ltd\.?|limited\.?|co\.?|company\.?)$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()

    # Hyphenated slug (primary) — replace non-alphanumeric runs with hyphens
    hyphenated = re.sub(r"[^a-z0-9]+", "-", name).strip("-")

    # Concatenated slug (secondary) — remove all separators
    concatenated = re.sub(r"[^a-z0-9]+", "", name)

    candidates = [hyphenated]
    if concatenated and concatenated != hyphenated:
        candidates.append(concatenated)

    return candidates

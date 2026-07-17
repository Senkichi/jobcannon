"""Opaque-redirect source registry — Section 1 of
docs/superpowers/specs/2026-07-08-job-listing-verification-design.md.

Config-driven (verification.opaque_redirect_sources in config.yaml), never a
hardcoded Python list — the registry mixes two distinct provenance signals
(see is_opaque_redirect_source's docstring) so membership can't collapse into
a single flat constant the way domain_policy.BLOCKED_DOMAINS does.

Also implements the liveness-gating attribute for sources (verification.gated_sources
in config.yaml). Jobs where ALL sources are gated AND expiry_status IS NOT 'live'
are hidden from the main listing query. A job with ANY non-gated source is never
gated (corroboration wins).

No DB, no network — pure functions over a job dict/Row and a config dict.
"""

from __future__ import annotations

from urllib.parse import urlparse

from jobcannon.engine.json_utils import safe_json_load

__all__ = [
    "UNVERIFIABLE_EVIDENCE_CEILING",
    "UNVERIFIABLE_EVIDENCE_CONFIRMED",
    "UNVERIFIABLE_EVIDENCE_PREFIX",
    "first_source_url",
    "gated_sources_from_config",
    "is_gated_source",
    "is_opaque_redirect_host",
    "is_opaque_redirect_source",
    "is_opaque_redirect_url",
    "is_unverifiable_candidate",
]


def _registry(config: dict) -> list[dict]:
    return (config.get("verification") or {}).get("opaque_redirect_sources") or []


def gated_sources_from_config(config: dict) -> list[str]:
    """Return the list of gated source tags from config.

    Config-driven set of sources subject to the liveness gate in get_filtered_jobs:
    jobs where ALL sources are gated AND expiry_status IS NOT 'live' are hidden.
    A job with ANY non-gated source (e.g. jooble + linkedin) is never gated.

    Single point of enforcement for the config-derivation step: is_gated_source
    (below) and every blueprint that needs the raw gated_sources list to pass
    into get_filtered_jobs / get_liveness_stats call this instead of
    re-deriving `(config.get("verification") or {}).get("gated_sources") or []`
    inline — a hardcoded-list-adjacent duplication that would silently drift
    (issue #1058 code review).
    """
    return (config.get("verification") or {}).get("gated_sources") or []


def is_gated_source(job_row, config: dict) -> bool:
    """True if job_row's entire provenance is within the gated sources set.

    Every entry in `sources` must be in the gated_sources list. A job with even
    one source that is not gated returns False (corroboration wins). This is the
    registry-level enforcement point for the liveness gate.

    Python companion to the "all sources gated" clause of
    job_finder.db._queries._liveness_gate_sql — same predicate logic (same
    parity-companion pattern as is_target_member/target_membership_sql). It
    does NOT evaluate the expiry_status half of the gate (whether the job is
    confirmed live); that check is applied in get_filtered_jobs /
    _liveness_gate_sql. A job can be is_gated_source()==True yet still visible
    on the board because expiry_status == 'live'.
    """
    gated = set(gated_sources_from_config(config))
    if not gated:
        return False

    sources = _as_str_list(job_row, "sources")
    if not sources:
        return False

    # If ANY source is not gated, the job is not gated
    return all(src in gated for src in sources)


def _row_value(job_row, key: str):
    """Tolerant field access for plain dicts and sqlite3.Row objects.

    Same shape as direct_link._row_value — kept as a small local copy rather
    than a shared import since both are private, single-purpose helpers with
    no third consumer yet (matches this codebase's existing small-duplication
    precedent, e.g. primary_source_resolver._parse_source_urls vs.
    direct_link's inline JSON handling).
    """
    try:
        return job_row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _as_str_list(job_row, key: str) -> list[str]:
    value = _row_value(job_row, key)
    if isinstance(value, str):
        value = safe_json_load(value, default=[])
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v]


def first_source_url(job_row) -> str | None:
    """Return the first non-empty URL in job_row's source_urls, or None."""
    source_urls = _as_str_list(job_row, "source_urls")
    return source_urls[0] if source_urls else None


def is_opaque_redirect_url(url: str | None, config: dict) -> bool:
    """True if url's host (+ optional path prefix) matches a registry `domain`
    entry. Host-boundary matched (exact or subdomain), never a substring —
    same anti-spoofing shape as direct_link.is_ats_or_careers_url.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    path = parsed.path.lower().lstrip("/")
    for entry in _registry(config):
        domain = (entry.get("domain") or "").lower()
        if not domain:
            continue
        if host != domain and not host.endswith("." + domain):
            continue
        entry_path = (entry.get("path") or "").lower()
        if entry_path and not (path == entry_path or path.startswith(entry_path + "/")):
            continue
        return True
    return False


def is_opaque_redirect_host(host: str | None, config: dict | None) -> bool:
    """True if host (exact or subdomain) matches any registry `domain` entry.

    Host-boundary match ignoring path — used to suppress derived shadow-flagging
    for hosts already promoted to the YAML registry.
    """
    if not host:
        return False
    host = host.lower()
    for entry in _registry(config or {}):
        domain = (entry.get("domain") or "").lower()
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            return True
    return False


def is_opaque_redirect_source(job_row, config: dict) -> bool:
    """True if job_row's entire provenance is within the registry.

    Every entry in `sources` must be explained: either it directly matches a
    registry `source_tag` (Jooble, Adzuna — the DB's own provenance label for
    that sighting), or — for tag-less provenance (Indeed, Monster; also any
    real ATS/careers sighting like 'greenhouse') — every URL in `source_urls`
    must itself match is_opaque_redirect_url. A job with even one source that
    is neither a registered tag nor explainable by an all-opaque source_urls
    set (i.e. a real, non-registry sighting exists) returns False.
    """
    registry = _registry(config)
    tag_entries = {e["source_tag"] for e in registry if e.get("source_tag")}

    sources = _as_str_list(job_row, "sources")
    if not sources:
        return False

    untagged = [s for s in sources if s not in tag_entries]
    if not untagged:
        return True

    source_urls = _as_str_list(job_row, "source_urls")
    if not source_urls:
        return False
    return all(is_opaque_redirect_url(u, config) for u in source_urls)


# ---------------------------------------------------------------------------
# Section 4/5 shared eligibility — job-listing-verification Plan 3
# ---------------------------------------------------------------------------
#
# Every archive produced by Section 4's visibility policy is tagged with one
# of the two reasons below, sharing UNVERIFIABLE_EVIDENCE_PREFIX so the
# reversibility checks (_jobs.py's reopen-scoping fix; _direct_link.py's
# reopen-on-corroboration hook) can match either without hardcoding the
# string twice. _confirmed = one of the four specific dead-end branches
# (stale_detector.py) was satisfied; _ceiling = the age-only backstop fired
# independent of any branch.
UNVERIFIABLE_EVIDENCE_PREFIX = "unverifiable_aggregator_listing_"
UNVERIFIABLE_EVIDENCE_CONFIRMED = UNVERIFIABLE_EVIDENCE_PREFIX + "confirmed"
UNVERIFIABLE_EVIDENCE_CEILING = UNVERIFIABLE_EVIDENCE_PREFIX + "ceiling"


def is_unverifiable_candidate(job_row, config: dict) -> bool:
    """Base eligibility for Section 4's archival policy AND Section 5's
    scoring-gate deferral — THE single shared condition (spec: "the *same*
    eligibility check ... a shared helper, not a duplicated condition") so a
    job can never be deferred from scoring by one code path while invisible
    to the archival decision in another.

    True iff the job's entire provenance is within the opaque-redirect
    registry (is_opaque_redirect_source) AND it has never been corroborated
    (direct_url IS NULL — a confident match, strict or loose, disqualifies a
    job from this predicate immediately, matching the exact precondition
    every one of Section 4's four branches independently requires too).

    This is the BASE condition only. Section 4's archival policy additionally
    requires a grace period and one of four company/probe-state branches (or
    the independent hard-ceiling backstop) — those need a join against
    companies this module deliberately doesn't perform; see
    stale_detector.py's archival step (Plan 3, Chunk 2).
    """
    if _row_value(job_row, "direct_url"):
        return False
    return is_opaque_redirect_source(job_row, config)

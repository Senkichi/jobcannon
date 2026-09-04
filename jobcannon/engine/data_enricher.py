# PORTED from job_finder/web/data_enricher.py @ 5fd8807eba99f087984f1baac707b404b67c871d (private job-cannon). Ledger L-0174.
"""Data enrichment module for sparse job records.

Cost-ordered enrichment pipeline with fetch-only tiers. Each tier is only
attempted after all cheaper tiers have been exhausted. Per-job
enrichment_tier column tracks the highest tier attempted so future calls
resume from the next tier.

Enrichment tiers (in order):
  1. free      — Direct URL fetch, ATS API query, HTML careers scrape
  2. ddg       — DuckDuckGo web search + URL fetch (free, no key)
  3. serpapi   — SerpAPI Google Jobs search (paid, optional key)
  4. agentic   — Ollama-driven query gen + Playwright fetch (deepest fallback)
  5. exhausted — All tiers attempted; never re-enrich
  6. agentic_exhausted — Agentic tier tried and found nothing; bounded-retry
     eligible after a cooldown (T2.9 / D21) before it becomes terminal
  7. expired   — agentic_exhausted retry budget spent; permanently terminal

Per-field cost ceilings:
  jd_full:    escalates all the way to agentic (critical for AI scoring)
  salary_min: capped at ddg (extracted post-fetch from jd_full when present)
  salary_max: capped at ddg

The previous LLM-synthesis tiers (haiku, sonnet) were removed in Phase 2b
sub-fix RC4: they fabricated short pseudo-JDs from search-result fragments
and blocked escalation to fetch tiers that actually retrieved the real JD.
Structured-field extraction (salary, location) now happens post-fetch from
jd_full via parse_structured_fields() (Phase 2c).

Design principles:
  - Never raises — all errors are caught and logged.
  - Returns empty dict when nothing can be enriched.
  - Skips enrichment when job already has all scoring-relevant data.
  - Persists enrichment_tier with enriched fields; jd_full and salary routed
    through sanctioned helpers before the UPDATE so invariant violations in one
    field cannot discard the tier bookmark or sibling fields (I-02 / I-13).
  - Jobs with enrichment_tier set resume from the NEXT tier up.
  - Exhausted jobs are returned immediately without any API calls.

Exports:
    TIER_ORDER: Ordered list of enrichment tier names.
    enrich_job: Enrich a sparse job record with cost-ordered tier fallback.

# PORT-SEAM: run_enrichment_backfill / run_location_extraction_backfill /
# _run_inline_agentic_pass (private data_enricher.py:562-860) are NOT ported here.
# They are scheduler-driven batch wrappers whose own DIES-hop imports
# (job_finder.web.db_helpers.standalone_connection) are covered by this port's
# seams, but they also call job_finder.web.autoheal.agentic_enricher
# (find_query_candidate / run_agentic_fetch), which has no ledger row in the
# read scope for this port (L-0174/L-0182/L-0229) and no ScanServices seam yet.
# Per the ADAPT rule, calls into an un-adjudicated module are never invented as a
# copied module or a guessed seam -- left for a follow-up row once agentic_enricher
# is adjudicated. enrich_job() itself (the ledger's stated crux) does not reach
# the agentic tier directly ("Deliberately NOT run per-row" -- see enrich_job
# docstring below) so this scope cut does not affect it.
"""

import json
import logging
import sqlite3
import time
from typing import Any

# PORT-SEAM: db._direct_link.set_direct_url is an OPTIONAL ScanServices
# seam (svc.set_direct_url) rather than a direct import -- its Postgres-
# native SQL cannot run against the bare sqlite3 connections tests/engine/
# uses, even though it is safe against a real connection_factory connection
# in production (internal conn.raw unwrap).
from jobcannon.engine.jd_content_contract import _is_jd_truncated
from jobcannon.db._locations import apply_location_observation
from jobcannon.engine.enrichment_states import (
    LOW_SIGNAL_TERMINAL,
    EnrichmentTier,
    # PORT-SEAM: backfill_skip_sql dropped -- used only by run_enrichment_backfill,
    # which this port deliberately does not carry (see the scope-cut comment below).
    resume_index,
)
from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.services import get_services
from jobcannon.engine.direct_link import pick_direct_link
from jobcannon.engine.enrichment_sources import merge_apply_urls, parse_source_urls

# PORT-SEAM: enrichment_tiers.* (L-0178, landed except scrape_careers_tier)
# accessed via svc.<name>() below,
# not imported directly here.
from jobcannon.engine.primary_source_merge import merge_primary_posting_fields
# PORT-SEAM: job_finder.web.enrichment_tiers is L-0178. 7 of its 8 functions
# (fetch_direct_jd, query_ats_api, search_ddg_web, fetch_ddg_jds,
# search_duckduckgo, search_serpapi, parse_structured_fields) are landed,
# split across jobcannon/engine/_enrichment_{jd_fetch,ats_tier,search_tiers,
# ddg_web_tier,structured_fields}.py; the 8th, scrape_careers (bound as
# svc.scrape_careers_tier), stays HOLD -- it depends on careers_crawler.py
# (L-0167, PR #369), not yet merged. All 8 are called via svc.<name>(...)
# ScanServices hooks instead of a directly-imported module.
# job_finder.sources._error_envelope is L-0111
# (HOLD) -- VendorAccountError is exposed as svc.vendor_account_error (an
# exception TYPE, not a callable) with a local placeholder type so the
# `except` clause always has something concrete to catch when unset.
# job_finder.web.autoheal.health_monitor has no ledger row in this port's
# read scope; its 2 functions are seamed the same way as run_detection
# (already an existing ScanServices field for the same private module).
# job_finder.db._jd_full.set_jd_full is the required ScanServices.set_jd_full
# seam (matches the existing upsert_job precedent: writer functions with
# host-specific persistence semantics go through the seam, not a direct
# jobcannon.db import). job_finder.config.JD_STORAGE_MAX_CHARS is
# ScanServices.jd_storage_max_chars (already a required field for exactly
# this constant).


class _NoVendorAccountError(Exception):
    """Placeholder exception type. Used when the host has not wired
    svc.vendor_account_error (L-0111 HOLD, no public counterpart) so the
    `except` clause below always has a concrete type -- it simply never
    matches, degrading vendor-account errors to the generic Exception path.
    """


logger = logging.getLogger(__name__)

# #1856: retry the enrichment persist UPDATE on `database is locked` so a
# transient write-lock contention (the #1320 starvation pattern) does not
# silently drop already-paid-for enrichment work. The connection's busy_timeout
# (30s) was already exceeded when the lock error surfaces, so a short backoff
# gives the contending writer time to release before retrying. Only
# `database is locked` is retried — a trigger violation (I-02/I-13) or any
# other error falls straight through to the tier-only fallback (unchanged).
_PERSIST_LOCK_MAX_ATTEMPTS = 4  # 1 initial attempt + 3 retries
_PERSIST_LOCK_BACKOFF_S = (0.5, 1.0, 2.0)


def _is_lock_error(exc: Exception) -> bool:
    """True for a SQLite ``database is locked`` OperationalError."""
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _persist_tier_fallback(conn: Any, dedup_key: str, tier_name: str) -> None:
    """Last-resort tier-only bookmark write, retried on ``database is locked``.

    Called from :func:`_persist` only when the primary UPDATE failed with a
    **non-lock** error (trigger violation I-02/I-13, etc.) — re-fetching would
    reproduce the same DB-invariant rejection, so the tier-only bookmark
    prevents an infinite re-fetch loop. When the primary failure was a lock
    error, :func:`_persist` skips this fallback entirely so the job requeues
    via its existing ``enrichment_tier`` (NULL or lower) instead of silently
    losing the enriched fields the primary UPDATE was carrying (#1856).

    Retried on ``database is locked`` (#1856) so a transient write-lock
    contention does not silently drop the bookmark and re-cost the enrichment
    work on the next run.
    """
    for attempt in range(_PERSIST_LOCK_MAX_ATTEMPTS):
        try:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = ? WHERE dedup_key = ?",
                (tier_name, dedup_key),
            )
            conn.commit()
            return
        except Exception as tier_e:
            if not _is_lock_error(tier_e) or attempt == _PERSIST_LOCK_MAX_ATTEMPTS - 1:
                logger.warning(
                    "_persist: tier fallback also failed for '%s': %s", dedup_key, tier_e
                )
                return
            backoff = _PERSIST_LOCK_BACKOFF_S[attempt]
            logger.warning(
                "_persist: tier fallback database locked for '%s' "
                "(attempt %d/%d, retrying in %.1fs)",
                dedup_key,
                attempt + 1,
                _PERSIST_LOCK_MAX_ATTEMPTS,
                backoff,
            )
            time.sleep(backoff)


def _maybe_reconcile_ats_identity(
    conn: Any,
    job_row: dict,
    config: dict | None,
    *,
    reason: str,
) -> None:
    """After ``source_urls`` gains ATS links, reconcile company ATS identity.

    Logs at WARNING (not DEBUG) on exception because ``reconcile_company_ats``
    returns a status dict for operator-meaningful outcomes (``slug_collision``,
    ``verify_failed``, ``abstain_conflict``) rather than raising. Any exception
    that reaches this handler is therefore a programmer/infra error (DB lock,
    AttributeError on a malformed row, import failure) that an operator needs
    to see. The swallow is kept so a single reconcile failure does not fail
    the surrounding enrichment run.
    """

    if conn is None:
        return
    cid = job_row.get("company_id")
    if cid is None:
        return
    try:
        svc = get_services()  # PORT-SEAM: existing ScanServices field
        if svc.reconcile_company_ats is None:
            return
        svc.reconcile_company_ats(conn, int(cid), reason=reason, config=config)
    except Exception as exc:
        logger.warning(
            "ATS identity reconcile failed (company_id=%s dedup_key=%s reason=%s): %s",
            cid,
            job_row.get("dedup_key"),
            reason,
            exc,
        )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Strict cost ordering: free (URL -> ATS -> careers) -> DDG -> SerpAPI -> agentic.
# Backed by jobcannon.engine.enrichment_states (single source of truth, F1 fix). Kept as a
# list of raw strings for backward-compatible callers/tests that index by tier name.
TIER_ORDER = [
    EnrichmentTier.FREE.value,
    EnrichmentTier.DDG.value,
    EnrichmentTier.SERPAPI.value,
    EnrichmentTier.AGENTIC.value,
    EnrichmentTier.EXHAUSTED.value,
]

# Allowlist of jobs table columns that _persist() may write directly. Prevents
# AI-extracted dict keys from injecting arbitrary column names into dynamic SQL
# SET clauses. ``location`` is deliberately NOT here: an extracted location is
# routed through ``apply_location_observation`` (the D-5 single-writer funnel)
# rather than side-door-written to the ``location`` column — that side-door
# write with an empty ``locations_raw`` was the S4 wipe (next crawler
# re-sighting rebuilt ``location`` from ``locations_raw=[]`` and reverted it).
_ENRICHABLE_COLUMNS = frozenset({"jd_full", "salary_min", "salary_max"})

# Per-field cost ceilings: highest tier allowed to search for this field.
# After this tier fails for a field, it is abandoned (not escalated further).
FIELD_TIER_CEILINGS = {
    "jd_full": "agentic",  # escalate all the way — critical for downstream scoring
    "salary_min": "ddg",  # cap at ddg — extracted post-fetch from jd_full
    "salary_max": "ddg",
}

# Minimum acceptable jd_full length when accepting a fetched JD from the
# agentic tier. Real fetched job postings are virtually always >= 200 chars;
# anything shorter is residual auth-wall noise that slipped past
# is_short_auth_page() (which uses < 2000 chars + signal-keyword detection).
# Apply ONLY to the agentic branch — earlier tiers have their own length
# guards (fetch_ddg_jds requires >= 200 chars, fetch_direct_jd is unbounded
# but already filters auth walls).
MIN_FETCH_JD_CHARS = 200

# Minimum character length for jd_full to be considered a real job description
# (not a stub title-restatement). Used by _is_stub_jd() to gate _find_missing_fields
# and _resolve_from_fragments so the pipeline escalates past title-only stubs.
_MIN_JD_LENGTH = 200

# String values of LOW_SIGNAL_TERMINAL tiers, derived once from the single source
# of truth (enrichment_states) for an O(1) membership test in enrich_job's
# auto-promote step. A content-reject of a truncated description resets one of
# these terminal tiers to NULL (issue #1374); detecting the reset here keeps the
# tail fallback _persist from clobbering it back within the same call.
_LOW_SIGNAL_TERMINAL_VALUES: frozenset[str] = frozenset(tier.value for tier in LOW_SIGNAL_TERMINAL)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_job(
    job_row: dict,
    serpapi_key: str | None = None,
    conn: Any = None,
    config: dict | None = None,
    careers_memo: Any | None = None,
    timeout: float | None = None,
) -> dict:
    """Enrich a sparse job record using the cost-ordered tier pipeline.

    Tiers: free (URL -> ATS -> careers) -> DDG -> SerpAPI -> agentic.
    Resumes from the next tier after job_row['enrichment_tier'] if set.
    Returns {} immediately for exhausted jobs.

    Persists enrichment_tier + enriched fields atomically to DB after each
    tier that produces data (if conn is provided). Returns the enriched dict.

    Args:
        job_row: Job record dict. Must have 'title' and 'company'.
        serpapi_key: Optional SerpAPI API key for SerpAPI tier.
        conn: Optional SQLite connection for DB persistence and cost recording.
        config: Optional application config dict.
        careers_memo: Optional shared CareersPageMemo for careers-page scraping.
        timeout: Optional per-call provider timeout override (seconds),
            forwarded to the post-fetch structured-extraction LLM call (the
            only ``call_model`` reachable from this function -- the free-tier
            careers scrape deliberately calls its own quick-tier fallback
            without conn/config and never reaches the network model path).
            Callers on a wall-clock budget (e.g. the onboarding wizard's
            eager first-score loop, issue #1413) should pass the remaining
            budget so a single slow/unreachable provider call cannot alone
            exceed it. Defaults to None (provider default timeout), unchanged
            for all other callers (backfill, nightly enrichment).

    Returns:
        Dict of enriched fields to UPDATE into the jobs table.
        Returns empty dict if nothing was enriched or job already has data.
    """
    if config is None:
        config = {}

    svc = get_services()  # PORT-SEAM: ScanServices seam (L-0174)

    try:
        # Exhausted jobs: skip immediately
        current_tier = job_row.get("enrichment_tier")
        if current_tier == "exhausted":
            return {}

        # Auto-promote long descriptions to jd_full (DQ-02) — routed through
        # set_jd_full() (Phase 46.03) for the content-density + truncation gate.
        #
        # Issue #1374: a truncated description (trailing ellipsis / too-short) is
        # content-rejected inside _set_jd_full, and _record_jd_content_reject
        # resets a LOW_SIGNAL_TERMINAL enrichment_tier (exhausted / agentic /
        # agentic_exhausted) to NULL so the row re-enters the regular pipeline.
        # That DB reset happens INSIDE this call, but ``job_row`` (and the local
        # ``current_tier``) still hold the stale terminal value — so the tier
        # cascade below skips every tier (resume_index past agentic) and the
        # terminal fallback _persist at the tail would write "exhausted" right
        # back, clobbering the NULL reset within the same call. The
        # ``_content_reject_requeued`` flag records that a reset happened so the
        # fallback is skipped.
        _content_reject_requeued = False
        if (
            not job_row.get("jd_full")
            and job_row.get("description")
            and len(job_row["description"]) > 200
        ):
            original_jd_full = job_row.get("jd_full")
            job_row["jd_full"] = job_row["description"]
            if conn is not None and job_row.get("dedup_key"):
                try:
                    wrote_desc = svc.set_jd_full(  # PORT-SEAM: ScanServices seam
                        conn,
                        job_row["dedup_key"],
                        job_row["description"][
                            : svc.jd_storage_max_chars
                        ],  # PORT-SEAM: JD_STORAGE_MAX_CHARS -> svc.jd_storage_max_chars
                        source="data_enricher",
                        title=job_row.get("title"),
                        config=config,
                    )
                    if not wrote_desc:
                        job_row["jd_full"] = original_jd_full
                        # A content-reject of a truncated body resets a terminal
                        # tier to NULL in the DB. Detect it (only meaningful when
                        # the row was at a LOW_SIGNAL_TERMINAL tier going in) so
                        # the tail fallback _persist does not clobber the reset.
                        if current_tier in _LOW_SIGNAL_TERMINAL_VALUES:
                            db_tier = conn.execute(
                                "SELECT enrichment_tier FROM jobs WHERE dedup_key = ?",
                                (job_row["dedup_key"],),
                            ).fetchone()
                            if db_tier is not None and db_tier["enrichment_tier"] is None:
                                _content_reject_requeued = True
                                job_row["enrichment_tier"] = None
                except Exception as e:
                    logger.debug("Description promotion DB write failed: %s", e)

        # Check if enrichment is needed
        missing = _find_missing_fields(job_row, config)
        if not missing:
            return {}

        # Determine start tier (resume from next tier after last attempted)
        start_idx = _start_tier_index(current_tier)

        title = job_row.get("title", "")
        company = job_row.get("company", "")

        # Accumulate fragments across tiers (each tier adds its text/data)
        fragments: dict = {}

        # ---------------------------------------------------------------
        # Tier 0: free — URL fetch + ATS API + careers scrape
        # ---------------------------------------------------------------
        if start_idx <= TIER_ORDER.index("free"):
            try:
                ats_result: dict = {}
                careers_result: dict = {}

                # Sub-tier A: Direct URL fetch
                source_urls = parse_source_urls(job_row.get("source_urls"))
                for url in source_urls:
                    jd_text = svc.fetch_direct_jd(url)  # PORT-SEAM: L-0178, landed
                    if jd_text:
                        fragments["url_jd"] = jd_text
                        break

                # Sub-tier B: ATS API query (if company has confirmed ATS slug)
                if conn is not None and job_row.get("company_id"):
                    ats_result = svc.query_ats_api(
                        job_row, conn, config
                    )  # PORT-SEAM: L-0178, landed
                    if ats_result:
                        fragments.update(ats_result)

                # Sub-tier C: HTML careers scrape (if company has homepage_url)
                if conn is not None and job_row.get("company_id"):
                    careers_result = svc.scrape_careers_tier(  # PORT-SEAM: L-0178 HOLD
                        job_row, conn, config, careers_memo=careers_memo
                    )
                    if careers_result:
                        # Don't overwrite ATS result
                        for k, v in careers_result.items():
                            if k not in fragments:
                                fragments[k] = v

                # Capture the direct company-posting link from data the ATS
                # scan / careers scrape already fetched (zero new network).
                if conn is not None and job_row.get("dedup_key"):
                    direct = pick_direct_link(source_urls, ats_result, careers_result)
                    if (
                        direct and svc.set_direct_url is not None
                    ):  # PORT-SEAM: db._direct_link.set_direct_url seam
                        svc.set_direct_url(conn, job_row["dedup_key"], direct[0], direct[1])

                    # Strict-matched primary posting: fold its authoritative
                    # fields (salary metadata, posted date, locations, the ATS
                    # URL itself) into the row via the canonical upsert merge.
                    primary_posting = ats_result.get("_primary_posting") or careers_result.get(
                        "_primary_posting"
                    )
                    if primary_posting:
                        merge_primary_posting_fields(conn, job_row, primary_posting, config=config)

                # Resolve what free tier found
                enriched = _resolve_from_fragments(fragments, missing, job_row, config)
                if enriched:
                    enriched = _apply_post_fetch_extraction(
                        enriched, job_row, conn, config, timeout=timeout
                    )
                    _persist(conn, job_row, enriched, "free", config)
                    return enriched

            except Exception as e:
                logger.debug("Free tier enrichment failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 1: ddg — DuckDuckGo Instant Answer API
        # ---------------------------------------------------------------
        # Check if remaining missing fields are all below DDG (nothing to do)
        remaining = _find_missing_fields(
            {**job_row, **_resolve_from_fragments(fragments, missing, job_row, config)},
            config,
        )
        if not remaining:
            enriched = _resolve_from_fragments(fragments, missing, job_row, config)
            enriched = _apply_post_fetch_extraction(
                enriched, job_row, conn, config, timeout=timeout
            )
            _persist(conn, job_row, enriched, "free", config)
            return enriched

        if start_idx <= TIER_ORDER.index("ddg"):
            try:
                ddg_result = svc.search_ddg_web(title, company)  # PORT-SEAM: L-0178, landed
                ddg_text = ddg_result.get("ddg_snippet", "")

                ddg_jd, ddg_source_url = svc.fetch_ddg_jds(  # PORT-SEAM: L-0178, landed
                    ddg_result.get("ddg_urls", [])
                )
                if ddg_jd:
                    fragments["url_jd"] = ddg_jd

                query = f"{title} {company} job description"
                fallback_text = svc.search_duckduckgo(query)  # PORT-SEAM: L-0178, landed
                ddg_parts = [text for text in (ddg_text, fallback_text) if text]
                if ddg_parts:
                    fragments["ddg"] = "\n\n".join(ddg_parts)

                if ddg_source_url and conn and job_row.get("dedup_key"):
                    merge_apply_urls(conn, job_row["dedup_key"], [ddg_source_url])
                    _maybe_reconcile_ats_identity(
                        conn, job_row, config, reason="enrichment_ddg_apply_url"
                    )

                # Resolve + persist what DDG found (mirrors the free tier).
                # _resolve_from_fragments maps fragments["url_jd"] -> jd_full
                # and applies _is_stub_jd's truncation gate; a stub yields
                # enriched == {} so escalation to SerpAPI/agentic proceeds.
                enriched = _resolve_from_fragments(fragments, missing, job_row, config)
                if enriched:
                    enriched = _apply_post_fetch_extraction(
                        enriched, job_row, conn, config, timeout=timeout
                    )
                    _persist(conn, job_row, enriched, "ddg", config)
                    return enriched

            except Exception as e:
                logger.debug("DDG tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Tier 2: serpapi — Google Jobs search (paid)
        # ---------------------------------------------------------------
        # SerpAPI only runs if JD is still missing (salary ceiling is ddg)
        jd_still_missing = not (
            job_row.get("jd_full") or fragments.get("url_jd") or fragments.get("jd_full")
        )

        # Gate 1: sources.serpapi.enabled must be true (or absent — treat absent
        # as enabled for backward-compat with configs predating this key).
        _serpapi_cfg = (config or {}).get("sources", {}).get("serpapi", {})
        _serpapi_enabled = _serpapi_cfg.get("enabled", True)

        # Gate 2: optional daily call cap (config key sources.serpapi.daily_call_cap).
        # Absent or 0 means uncapped.  Checked against the scoring_costs ledger so
        # the cap survives Flask restarts (mirrors the google_cse_source pattern).
        _daily_cap: int = int(_serpapi_cfg.get("daily_call_cap", 0))
        _cap_reached = bool(_daily_cap > 0 and _serpapi_daily_calls_used(conn) >= _daily_cap)

        # Gate 3: 429 rate-limit cooldown (config key sources.serpapi.rate_limit_cooldown_hours).
        # Absent or 0 means no cooldown. Reads source_health so the cooldown is shared
        # across ingestion and enrichment.
        _rate_limit_cooldown_hours: int = int(_serpapi_cfg.get("rate_limit_cooldown_hours", 24))
        _rate_limit_active = False
        if _rate_limit_cooldown_hours > 0 and conn:
            if svc.is_source_rate_limited is not None:  # PORT-SEAM: autoheal.health_monitor
                _rate_limit_active = svc.is_source_rate_limited(
                    conn, "serpapi", _rate_limit_cooldown_hours
                )

        if start_idx <= TIER_ORDER.index("serpapi") and serpapi_key and jd_still_missing:
            if not _serpapi_enabled:
                logger.debug("SerpAPI tier skipped for '%s': sources.serpapi.enabled=false", title)
            elif _rate_limit_active:
                logger.warning(
                    "SerpAPI tier skipped for '%s': 429 rate-limit cooldown active", title
                )
            elif _cap_reached:
                logger.warning(
                    "SerpAPI tier skipped for '%s': daily_call_cap=%d reached", title, _daily_cap
                )
            else:
                try:
                    query = f"{title} {company}"
                    serpapi_result, apply_url_list = (
                        svc.search_serpapi(  # PORT-SEAM: L-0178, landed
                            query, serpapi_key
                        )
                    )
                    _record_serpapi_call(conn)
                    if conn and job_row.get("dedup_key") and apply_url_list:
                        merge_apply_urls(conn, job_row["dedup_key"], apply_url_list)
                        _maybe_reconcile_ats_identity(
                            conn, job_row, config, reason="enrichment_serpapi_apply_urls"
                        )

                    if serpapi_result:
                        for k, v in serpapi_result.items():
                            if k not in fragments:
                                fragments[k] = v

                        enriched = _resolve_from_fragments(
                            {**fragments, **serpapi_result}, missing, job_row, config
                        )
                        if enriched:
                            enriched = _apply_post_fetch_extraction(
                                enriched, job_row, conn, config, timeout=timeout
                            )
                            _persist(conn, job_row, enriched, "serpapi", config)
                            return enriched

                except svc.vendor_account_error or _NoVendorAccountError as e:
                    # PORT-SEAM: svc.vendor_account_error is L-0111 HOLD;
                    # svc.record_source_error is autoheal.health_monitor (no ledger
                    # row in this port's read scope), seamed like run_detection.
                    if conn and svc.record_source_error is not None:
                        svc.record_source_error(conn, "serpapi", str(e))
                    logger.warning("SerpAPI tier failed for '%s': %s", title, e)
                except Exception as e:
                    logger.debug("SerpAPI tier failed for '%s': %s", title, e)

        # ---------------------------------------------------------------
        # Deepest fallback: agentic (Ollama query-gen + Playwright fetch)
        # ---------------------------------------------------------------
        # Deliberately NOT run per-row here. The agentic tier is Playwright-heavy
        # and used to spin up a FRESH Chromium per row from this synchronous
        # cascade (the 2026-06-22 process-spawn storm: run_enrichment_backfill
        # has no limit, so one scheduled run launched N browsers). Rows that
        # exhaust the cheaper tiers fall through to 'exhausted' and are served by
        # the BATCHED agentic pass, which launches ONE browser and reuses it
        # across all rows — driven inline at the tail of run_enrichment_backfill
        # (so the JD still lands in the same cycle, no cross-run delay) and
        # nightly by the agentic_backfill scheduled job.
        #
        # Issue #1374: if the auto-promote step above content-rejected a truncated
        # description, _record_jd_content_reject already reset a LOW_SIGNAL_TERMINAL
        # enrichment_tier to NULL (re-queuing the row for the regular pipeline).
        # Do NOT clobber that reset back to a terminal value within this same call —
        # the row must stay re-queued so the next backfill pass re-attempts from the
        # free tier, rather than being re-marooned at "exhausted".
        if _content_reject_requeued:
            return {}
        _persist(conn, job_row, {}, "exhausted", config)
        return {}

    except Exception as e:
        logger.warning("enrich_job failed for '%s': %s", job_row.get("title"), e)
        return {}


# PORT-SEAM: _run_inline_agentic_pass / run_enrichment_backfill /
# run_location_extraction_backfill dropped from this port -- see module
# docstring (L-0174 agentic_enricher blocker) for why.


def _is_stub_jd(
    jd_text: str | None,
    title: str = "",
    company: str = "",
    config: dict | None = None,
) -> bool:
    """Return True if jd_text is a stub (falsy or truncated snippet).

    Stubs are treated as missing jd_full so the pipeline escalates to richer tiers
    that may provide a real job description, rather than persisting noise.
    Truncation detection (length floor + trailing ellipsis/…) is config-driven.

    Args:
        jd_text: The jd_full text to check.
        title:   Job title (carried for API symmetry; unused in current check).
        company: Company name (carried for API symmetry; unused in current check).
        config:  Optional app config for the truncation thresholds.
    """
    if not jd_text:
        return True
    return _is_jd_truncated(jd_text, config) is not None


def _find_missing_fields(job_row: dict, config: dict | None = None) -> list:
    """Return list of missing scoring-relevant field names.

    A job needs enrichment if any of these are missing:
    - jd_full: full job description (needed for AI scoring). Stubs (title
      restatements shorter than _MIN_JD_LENGTH chars) are treated as missing.
    - salary_min: minimum salary
    - location: canonical location string (D-5; empty string counts as missing)

    Returns empty list if all fields are present (no enrichment needed).
    """
    missing = []
    if _is_stub_jd(
        job_row.get("jd_full"),
        job_row.get("title", ""),
        job_row.get("company", ""),
        config=config,
    ):
        missing.append("jd_full")
    if job_row.get("salary_min") is None:
        missing.append("salary_min")
    if not job_row.get("location"):
        # Empty string or NULL — location joins the enrichment contract (D-5, #388)
        missing.append("location")
    return missing


def _serpapi_daily_calls_used(conn: Any) -> int:
    """Return today's SerpAPI enrichment call count from the scoring_costs ledger.

    Uses the same calendar-day window as google_cse_source: UTC timestamps
    stored by utc_now_iso(), compared via local_day_utc_window() so the reset
    aligns with the user's clock.  Falls back to 0 on any DB error so a
    quota-read failure never blocks enrichment outright.

    Args:
        conn: SQLite connection (may be None — returns 0 immediately).
    """
    if conn is None:
        return 0
    try:
        from jobcannon.engine.json_utils import local_day_utc_window

        start, end = local_day_utc_window()
        row = conn.execute(
            "SELECT COUNT(*) FROM scoring_costs "
            "WHERE provider=? AND timestamp >= ? AND timestamp < ?",
            ("serpapi_enrichment", start, end),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as exc:
        logger.warning(
            "SerpAPI daily quota read failed (%s); quota gate skipped",
            type(exc).__name__,
        )
        return 0


def _record_serpapi_call(conn: Any) -> None:
    """Append a quota-ledger row to scoring_costs for one SerpAPI enrichment call.

    Uses provider='serpapi_enrichment' and cost_usd=0 (cost is real but
    untracked per-call — this row exists only as a daily quota counter,
    mirroring the google_cse_source pattern).  Silent no-op when conn is
    None or the INSERT fails (best-effort; never raises).

    Args:
        conn: SQLite connection (may be None — skips silently).
    """
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO scoring_costs "
            "(job_id, purpose, model, input_tokens, output_tokens, cost_usd, timestamp, provider) "
            "VALUES (NULL, ?, ?, 0, 0, 0, ?, ?)",
            ("serpapi_enrichment", "serpapi_enrichment", utc_now_iso(), "serpapi_enrichment"),
        )
        conn.commit()
    except Exception as exc:
        logger.warning(
            "SerpAPI quota ledger write failed (%s); call not counted against cap",
            type(exc).__name__,
        )


def _filter_non_none(d: dict) -> dict:
    """Return a new dict with None values removed."""
    return {k: v for k, v in d.items() if v is not None}


def _start_tier_index(current_tier: str | None) -> int:
    """Return the index in TIER_ORDER to start from based on current_tier.

    Thin wrapper over ``jobcannon.engine.enrichment_states.resume_index`` (the single
    source of truth, F1 fix). If current_tier is None, start from 0; a known tier
    resumes from the NEXT tier; an unknown tier is treated as terminal (fail-closed)
    and logs a warning. Retained as a module-level name for backward-compatible
    callers/tests that import ``_start_tier_index``.

    Args:
        current_tier: The enrichment_tier value from the job row.

    Returns:
        Index into TIER_ORDER to start enrichment from.
    """
    return resume_index(current_tier)


def _resolve_from_fragments(
    fragments: dict,
    missing: list,
    job_row: dict,
    config: dict | None = None,
) -> dict:
    """Build an enriched dict from fragments for the fields that are missing.

    Looks for direct matches: fragments['jd_full'] -> jd_full,
    fragments['url_jd'] -> jd_full, fragments['salary_min'] -> salary_min, etc.

    Rejects stub jd_full values (truncated snippets) via _is_stub_jd() — same
    gate as _find_missing_fields() — so stubs from cheaper tiers don't block
    escalation to richer tiers that may have the real JD.

    Args:
        fragments: Dict of collected data from free-tier sources.
        missing: List of field names that are still missing.
        job_row: Original job row for reference.
        config: Optional app config for the truncation thresholds.

    Returns:
        Dict of {field: value} for fields that fragments can satisfy.
    """
    title = job_row.get("title", "")
    company = job_row.get("company", "")
    enriched = {}
    for field in missing:
        # jd_full: prefer an explicit fragments['jd_full'], fall back to
        # fragments['url_jd'] (the URL-fetched body). A truncated snippet
        # in either slot is skipped so a richer source can still win.
        if field == "jd_full":
            candidates = [
                fragments.get("jd_full"),
                fragments.get("url_jd"),
            ]
            for candidate in candidates:
                if candidate and not _is_stub_jd(candidate, title, company, config):
                    enriched["jd_full"] = candidate
                    break
            continue

        # Direct key match for non-jd_full fields
        if field in fragments and fragments[field] is not None:
            enriched[field] = fragments[field]

    return _filter_non_none(enriched)


def _apply_post_fetch_extraction(
    enriched: dict,
    job_row: dict,
    conn: Any,
    config: dict,
    timeout: float | None = None,
) -> dict:
    """Augment ``enriched`` with structured fields parsed from the fetched JD.

    Runs ``parse_structured_fields`` exactly once per successful cascade tier
    when (a) a jd_full is now available (from this tier or already on the row)
    and (b) at least one of salary_min/salary_max/location is still empty in
    BOTH ``enriched`` and ``job_row``. Returned values fill ONLY empty fields
    — never overwrite existing values from the row or from this tier.

    Returns a NEW dict (immutability — does not mutate the input ``enriched``).

    Replaces the salary-extraction side-effect of the deleted Haiku/Sonnet
    synthesis tiers (Phase 2b sub-fix RC4). See parse_structured_fields()
    docstring for the no-summarize guarantee.

    Args:
        timeout: Optional provider-call timeout override (seconds), forwarded
            to ``parse_structured_fields``. See ``enrich_job``'s ``timeout``
            docstring (issue #1413).
    """
    # Effective jd_full: prefer the freshly-enriched value, fall back to the row
    effective_jd = enriched.get("jd_full") or job_row.get("jd_full")
    if not effective_jd or len(effective_jd) < MIN_FETCH_JD_CHARS:
        return dict(enriched)

    # An "empty" structured field is missing from BOTH enriched and job_row
    structured_fields = ("salary_min", "salary_max", "location")

    def _is_empty(field: str) -> bool:
        return enriched.get(field) is None and not job_row.get(field)

    # #1202: also run the LLM call when the residency-constraint check hasn't
    # run yet (has_subcountry_constraint is NULL on the row and not already
    # in enriched). The column defaults to NULL; the LLM sets it to 0 or 1,
    # so subsequent passes skip the check (idempotent).
    needs_residency_check = (
        job_row.get("has_subcountry_constraint") is None
        and "has_subcountry_constraint" not in enriched
    )

    if not any(_is_empty(f) for f in structured_fields) and not needs_residency_check:
        return dict(enriched)

    merged = dict(enriched)

    # Fast path: deterministic regex salary extraction. Runs first so
    # the common-format JDs ($120K-$150K, "salary range: 120K-150K",
    # USD 120,000-150,000, etc.) don't burn an LLM call. Only fills
    # salary_{min,max} both-or-neither — the regex helper guarantees
    # both-present-or-both-None semantics.
    # PORT-SEAM: salary_extractor is L-0253 (DIES) -- svc.extract_salary_from_text.
    svc = get_services()

    if (
        _is_empty("salary_min")
        and _is_empty("salary_max")
        and svc.extract_salary_from_text is not None
    ):
        regex_min, regex_max = svc.extract_salary_from_text(effective_jd)
        if regex_min is not None and regex_max is not None:
            merged["salary_min"] = regex_min
            merged["salary_max"] = regex_max
            # P1.5 (D-4): tag the writer class so the reconciler can rank this
            # JD-regex extraction (rank 3) against the stored pair — it must not
            # overwrite an ats_structured (rank 4) pair.
            merged["salary_provenance"] = "jd_regex"

    # Recompute is-empty after regex pass — the LLM only needs to run
    # if there's something it can still help with (location, or salary
    # the regex couldn't find), OR the residency-constraint check hasn't
    # run yet (#1202).
    def _still_empty(field: str) -> bool:
        return merged.get(field) is None and not job_row.get(field)

    if not any(_still_empty(f) for f in structured_fields) and not needs_residency_check:
        return merged

    # BUGFIX (this port, not present upstream): every other optional
    # ScanServices hook in this module is guarded with an `is not None`
    # check before invocation (see svc.extract_salary_from_text above,
    # svc.reconcile_company_ats, svc.update_pipeline_status,
    # svc.is_source_rate_limited, svc.record_source_error) so a host that
    # legitimately leaves an optional hook unset degrades gracefully. This
    # call site was missing that guard: parse_structured_fields is an
    # Optional[Callable] (None default, services.py "L-0178 HOLD"), so any
    # host without it wired -- and, concretely, this engine unit-test
    # harness's ScanServices fixture -- hit a bare `TypeError: 'NoneType'
    # object is not callable` here. Because this whole block runs inside
    # enrich_job()'s per-tier `try/except Exception`, the crash was silently
    # swallowed and mistaken for "this tier found nothing", so the row fell
    # through every remaining tier to 'exhausted' instead of persisting the
    # jd_full/salary fields the tier actually found. Restoring the same
    # None-check used by every sibling hook in this file fixes it.
    if svc.parse_structured_fields is None:
        return merged

    parsed = svc.parse_structured_fields(  # PORT-SEAM: L-0178, landed
        jd_full=effective_jd,
        job_row=job_row,
        conn=conn,
        config=config,
        timeout=timeout,
    )
    if not parsed:
        return merged

    filled_salary_from_llm = False
    for field, value in parsed.items():
        if field not in structured_fields:
            # #1202: residency_location and has_subcountry_constraint are
            # not "fill-only-empty" fields — they are always passed through
            # when the LLM provides them. residency_location is routed to
            # apply_location_observation by _persist; has_subcountry_constraint
            # is written to its column. has_subcountry_constraint is always
            # emitted (0 or 1) so the column moves from NULL to a definitive
            # value, preventing re-runs.
            if field == "residency_location":
                merged["residency_location"] = value
            elif field == "has_subcountry_constraint":
                merged["has_subcountry_constraint"] = value
            continue
        if _still_empty(field):  # only fill empty fields — never overwrite
            merged[field] = value
            if field in ("salary_min", "salary_max"):
                filled_salary_from_llm = True
    # P1.5 (D-4): an LLM-extracted salary is provenance 'llm_extract' (rank 2).
    # Only tag it when the LLM (not the regex fast-path above) supplied the
    # value, so the reconciler ranks each enrichment write correctly.
    if filled_salary_from_llm and "salary_provenance" not in merged:
        merged["salary_provenance"] = "llm_extract"
    return merged


def _mutate_unresolved_reason(
    conn: Any,
    dedup_key: str,
    reason: str,
    *,
    add: bool,
) -> None:
    """Surgically add or remove one ``unresolved_reasons`` code, preserving others.

    A list-rebuild (not a wholesale overwrite) so a row carrying multiple reasons
    keeps the unrelated ones (D-9: quarantine via the existing surface). Idempotent
    — a no-op when the code is already present (add) or already absent (remove).
    Never raises: a reason-sync failure must not abort the enrichment persist.

    Reads the CURRENT ``unresolved_reasons`` from the DB rather than trusting a
    caller-supplied ``job_row`` snapshot. ``_persist`` Step 1 routes ``jd_full``
    through ``_set_jd_full``, which atomically clears the I-18 reason codes
    (e.g. ``jd_full_truncated``) on a successful write; the ``job_row`` dict
    ``_persist`` received still holds that pre-write snapshot. If Step 2's salary
    sync read the stale snapshot, it would write back a list that resurrects the
    just-cleared code (issue #1374 round-2 regression). Re-SELECTing at this
    single write chokepoint makes that ordering hazard unrepresentable regardless
    of how many prior mutations happened in the same ``_persist`` call.
    """
    try:
        row = conn.execute(
            "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        raw = (row["unresolved_reasons"] if row is not None else None) or "[]"
        try:
            reasons = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            reasons = []
        if not isinstance(reasons, list):
            reasons = []
        present = reason in reasons
        if add and not present:
            updated = [*reasons, reason]
        elif not add and present:
            updated = [r for r in reasons if r != reason]  # surgical list-remove
        else:
            return  # already in the desired state
        conn.execute(
            "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
            (json.dumps(updated), dedup_key),
        )
        conn.commit()
    except Exception as exc:
        logger.warning(
            "_persist: could not %s unresolved reason %r for '%s': %s",
            "add" if add else "clear",
            reason,
            dedup_key,
            exc,
        )


def _append_enrichment_salary_observation(
    conn: Any,
    dedup_key: str,
    sal_min: int | None,
    sal_max: int | None,
    provenance: str | None,
    resolution: str | None = None,
) -> None:
    """Append an enrichment salary observation to the row's lossless log (D-1).

    Evidence is retained regardless of whether the value won the canonical slot,
    so a quarantined / out-ranked extraction is still visible in
    ``salary_observations`` and ``/admin/review``. Routed through the same
    ``_merge_salary_observations`` helper the upsert path uses, so the array
    stays deduped (by provenance/raw_text/min/max) and capped. Never raises —
    a failed observation append must not abort the enrichment persist.
    """
    if sal_min is None and sal_max is None:
        return
    try:
        import json

        from jobcannon.db._jobs import _merge_salary_observations
        from jobcannon.engine.json_utils import safe_json_load

        row = conn.execute(
            "SELECT salary_observations FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        stored = safe_json_load(row["salary_observations"], default=[]) if row else []
        if not isinstance(stored, list):
            stored = []
        obs: dict = {
            "min_value": sal_min,
            "max_value": sal_max,
            "period": "unknown",
            "currency": "USD",
            "provenance": provenance,
            "raw_text": None,
        }
        if resolution is not None:
            obs["resolution"] = resolution  # P1.6: stamp the salvage verdict (D-3/D-9)
        incoming = [obs]
        merged, changed = _merge_salary_observations(stored, incoming)
        if changed:
            conn.execute(
                "UPDATE jobs SET salary_observations = ? WHERE dedup_key = ?",
                (json.dumps(merged), dedup_key),
            )
            conn.commit()
    except Exception as e:
        logger.warning("_persist: salary observation append failed for '%s': %s", dedup_key, e)


def _persist(
    conn: Any,
    job_row: dict,
    enriched: dict,
    tier_name: str,
    config: dict | None = None,
) -> None:
    svc = get_services()  # PORT-SEAM: ScanServices seam (L-0174)
    """Persist enriched fields + enrichment_tier, routing each field through its
    sanctioned write path so m078 invariant violations cannot silently discard
    the full enrichment.

    Write order:
    1. ``jd_full`` — routed through ``_set_jd_full()`` (I-13 junk gate + truncation
       gate) as a separate commit. A junk/truncated JD is logged and skipped;
       ``_set_jd_full`` never raises, so a bad JD cannot abort the remaining writes.
    2. ``salary_min`` / ``salary_max`` — reconciled via
       ``_reconcile_salary_for_write()`` before the UPDATE (I-02 inversion fix).
       A single new value that would invert against the existing stored
       counterpart is dropped (keeping existing) so the I-02 trigger cannot abort
       the persist; a both-field same-unit inversion is swapped; an extreme
       mismatch drops the incoming pair. Drops log a WARNING.
    3. Remaining fields (``location``, normalised salary) + ``enrichment_tier``
       in one UPDATE.  If that UPDATE fails unexpectedly, a fallback tier-only
       UPDATE ensures the tier bookmark is always recorded so the job is not
       re-fetched indefinitely.  Both the primary and the fallback UPDATE are
       retried on ``database is locked`` (with backoff) so a transient
       write-lock contention does not silently drop already-paid-for enrichment
       work (#1856).  When the primary UPDATE exhausts all lock retries, the
       fallback is **skipped** so the job requeues via its existing (NULL or
       lower) ``enrichment_tier`` instead of silently losing the enriched
       fields the primary UPDATE was carrying — only non-lock failures (trigger
       violations) use the fallback bookmark, since re-fetching would reproduce
       the same rejection.

    Only writes to DB if conn is provided. If enriched is empty, still
    updates enrichment_tier to track progress (unless conn is None).

    Args:
        conn: Open SQLite connection. If None, skip persistence.
        job_row: Job row dict (must have 'dedup_key').
        enriched: Dict of {column_name: value} to update.
        tier_name: The enrichment tier name to record.
        config: Optional app config for the truncation thresholds.
    """
    if conn is None:
        return

    dedup_key = job_row.get("dedup_key")
    if not dedup_key:
        return

    # --- Step 0: location — route through the D-5 single-writer funnel ---
    # An extracted location is an *observation*, not a direct column write. The
    # funnel merges it into locations_raw + rewrites all five canonical location
    # columns atomically, so a later crawler re-sighting (empty incoming
    # location) cannot wipe it (the S4 bug). Pop it before the allowlist filter
    # so it is neither dropped-as-unknown nor written directly.
    if enriched:
        location_obs = enriched.get("location")
        if location_obs and str(location_obs).strip():
            apply_location_observation(conn, dedup_key, str(location_obs), source="llm_extract")

        # #1202: residency_location — a JD-prose residency/country constraint
        # the structured fields missed. Routed through the SAME D-5 funnel as
        # the primary location, but tagged source="llm_extract_residency" so
        # the provenance is distinguishable in logs. The funnel merges it into
        # locations_structured so compute_location_fit's rule table picks the
        # correct row (e.g. Row 3 for a remote-in-foreign-country posting).
        residency_obs = enriched.get("residency_location")
        if residency_obs and str(residency_obs).strip():
            apply_location_observation(
                conn, dedup_key, str(residency_obs), source="llm_extract_residency"
            )

        # #1202: has_subcountry_constraint — boolean gate for constraints finer
        # than country/region/city (e.g. a state-list restriction). Written
        # directly to its column (not through the location funnel — the
        # constraint cannot be represented as a raw_location string). The
        # column defaults to NULL; the LLM sets it to 0 or 1 so subsequent
        # enrichment passes skip the residency check.
        subcountry = enriched.pop("has_subcountry_constraint", None)
        if subcountry is not None:
            try:
                conn.execute(
                    "UPDATE jobs SET has_subcountry_constraint = ? WHERE dedup_key = ?",
                    (1 if subcountry else 0, dedup_key),
                )
                conn.commit()
            except Exception as exc:
                logger.warning(
                    "_persist: has_subcountry_constraint write failed for '%s': %s",
                    dedup_key,
                    exc,
                )

    # P1.5 (D-4): the writer-class tag for an extracted salary rides in the
    # enriched dict as 'salary_provenance' ('jd_regex' rank 3 / 'llm_extract'
    # rank 2). Pop it before the allowlist filter (it is reconciliation
    # metadata, written only as part of the pair-atomic salary write below).
    incoming_salary_provenance = enriched.pop("salary_provenance", None) if enriched else None

    if enriched:
        # Filter to allowlisted columns only — prevents AI-extracted keys from
        # injecting arbitrary column names into the dynamic SQL SET clause.
        # ``location`` and ``residency_location`` are intentionally excluded
        # (handled by the D-5 funnel above); ``has_subcountry_constraint`` was
        # already popped and written directly. Drop these silently rather than
        # logging them as unknown columns.
        safe_enriched = {k: v for k, v in enriched.items() if k in _ENRICHABLE_COLUMNS}
        unknown = (
            set(enriched)
            - _ENRICHABLE_COLUMNS
            - {
                "location",
                "residency_location",
            }
        )
        if unknown:
            logger.warning("_persist: dropping non-allowlisted columns: %s", unknown)
    else:
        safe_enriched = {}

    # --- Step 1: jd_full — routed through set_jd_full() (I-13 junk gate) ---
    # Extracted from the multi-column UPDATE so a junk JD trigger (I-13) cannot
    # abort and discard the enrichment_tier bookmark and all sibling fields.
    # _set_jd_full() handles its own commit; it never raises.
    jd_full_value = safe_enriched.pop("jd_full", None)
    if jd_full_value is not None:
        try:
            svc.set_jd_full(  # PORT-SEAM: ScanServices seam
                conn,
                dedup_key,
                jd_full_value,
                source="data_enricher._persist",
                title=job_row.get("title"),
                config=config,
            )
        except Exception as e:
            logger.warning("_persist: jd_full write failed for '%s': %s", dedup_key, e)

    # --- Step 2: salary — reconcile before writing (I-02 inversion fix) ---
    # _reconcile_salary_for_write() validates the EFFECTIVE pair the I-02 trigger
    # will see: a single-field update leaves the unset column at its stored
    # value, so a new value that inverts against the existing counterpart trips
    # tg_jobs_salary_range and aborts the whole persist. The helper drops such an
    # incoming value (keeping existing) rather than letting the trigger fire.
    sal_min = safe_enriched.pop("salary_min", None)
    sal_max = safe_enriched.pop("salary_max", None)
    salary_cols: dict = {}
    if sal_min is not None or sal_max is not None:
        # P1.5 (D-4): pass the incoming + stored provenance so a strictly-lower-
        # rank enrichment write (llm_extract=2 / jd_regex=3) cannot overwrite a
        # stored ats_structured (4) pair. A NULL stored provenance ranks 0
        # (legacy/unranked) so any genuine extraction can still fill it.
        # PORT-SEAM: job_finder.db._jobs._reconcile_salary_for_write has no
        # public counterpart -- jobcannon/db/_jobs.py deliberately ships a
        # simpler fill-if-null policy for upsert_job's own salary write
        # (Wave-1 divergence #3, see that file's module docstring). Mirrored
        # here rather than reintroducing the trust-ranked reconciler: fill
        # only when BOTH stored fields are empty; otherwise drop the
        # incoming value and keep the stored pair untouched.
        existing_min = job_row.get("salary_min")
        existing_max = job_row.get("salary_max")
        if existing_min is None and existing_max is None:
            salary_cols = {
                k: v
                for k, v in {"salary_min": sal_min, "salary_max": sal_max}.items()
                if v is not None
            }
            dropped = False
        else:
            salary_cols = {}
            dropped = True
        if dropped:
            logger.warning(
                "_persist: salary dropped for '%s' (lower-rank provenance or would "
                "invert the stored range; incoming min=%s max=%s prov=%s, existing "
                "min=%s max=%s prov=%s)",
                dedup_key,
                sal_min,
                sal_max,
                incoming_salary_provenance,
                job_row.get("salary_min"),
                job_row.get("salary_max"),
                job_row.get("salary_provenance"),
            )
        # When the extraction won the canonical slot, stamp its provenance so the
        # next writer can rank against it (single pair-atomic write).
        if salary_cols and incoming_salary_provenance:
            salary_cols = {**salary_cols, "salary_provenance": incoming_salary_provenance}
        # P1.6 (D-2/D-3): classify the incoming pair through the single normalizer
        # so the observation log and the quarantine reason both reflect one verdict.
        from jobcannon.engine.salary_normalizer import SalaryObservation, normalize_observation

        resolution = normalize_observation(
            SalaryObservation(
                min_value=sal_min,
                max_value=sal_max,
                period="unknown",
                currency="USD",
                provenance=incoming_salary_provenance or "feed_string",
                raw_text=None,
            )
        ).resolution
        # Append the lossless observation regardless of who won the slot (D-1):
        # evidence is never discarded. Routed through the upsert helper so the
        # array stays deduped + capped.
        _append_enrichment_salary_observation(
            conn,
            dedup_key,
            sal_min,
            sal_max,
            incoming_salary_provenance,
            resolution,
        )
        safe_enriched.update(salary_cols)

        # P1.6 (D-3/D-9): keep the salary_implausible quarantine code in sync with
        # the canonical outcome of this pass.
        #   * A plausible pair won the slot  -> clear the code (the loop closes: the
        #     row now has a canonical salary and leaves /admin/review).
        #   * The pass yielded only an implausible observation and the canonical
        #     pair is still NULL -> set the code so the row stays quarantined and
        #     re-enters enrichment (salary_min IS NULL selection).
        canonical_written = (
            salary_cols.get("salary_min") is not None or salary_cols.get("salary_max") is not None
        )
        if canonical_written:
            _mutate_unresolved_reason(conn, dedup_key, "salary_implausible", add=False)
        elif resolution == "implausible" and (
            job_row.get("salary_min") is None and job_row.get("salary_max") is None
        ):
            _mutate_unresolved_reason(conn, dedup_key, "salary_implausible", add=True)

    # --- Step 3: remaining fields + enrichment_tier ---
    # enrichment_tier is always written — even when every enriched field was
    # junk-gated or dropped — so the job is not re-fetched on the next backfill.
    # The fallback tier-only UPDATE in the except handler is the last resort for
    # any unexpected violation that slips past the Python-layer guards above.
    # #1856: both the primary and the fallback UPDATE are retried on
    # `database is locked` (with backoff) so a transient write-lock contention
    # does not silently drop already-paid-for enrichment work.
    salary_write_succeeded = False
    primary_exc: Exception | None = None
    for attempt in range(_PERSIST_LOCK_MAX_ATTEMPTS):
        try:
            if safe_enriched:
                set_clauses = ", ".join(f"{k} = ?" for k in safe_enriched)
                set_clauses += ", enrichment_tier = ?"
                values = list(safe_enriched.values()) + [tier_name, dedup_key]
                conn.execute(
                    f"UPDATE jobs SET {set_clauses} WHERE dedup_key = ?",
                    values,
                )
            else:
                conn.execute(
                    "UPDATE jobs SET enrichment_tier = ? WHERE dedup_key = ?",
                    (tier_name, dedup_key),
                )
            conn.commit()
            # Only the primary write branch above actually lands salary_cols (if
            # any) into the row -- the except handler below falls back to a
            # tier-only bookmark UPDATE that never writes salary_max. Gating the
            # T4.2/D20 re-eval on this flag (not merely "we have salary_cols in
            # memory") avoids a spurious un-dismiss when the real write failed.
            salary_write_succeeded = True
            break
        except Exception as e:
            primary_exc = e
            # Only `database is locked` is retried. A trigger violation
            # (I-02/I-13) or any other error will reject every retry, so fall
            # straight through to the tier-only fallback (unchanged behavior).
            if not _is_lock_error(e) or attempt == _PERSIST_LOCK_MAX_ATTEMPTS - 1:
                break
            backoff = _PERSIST_LOCK_BACKOFF_S[attempt]
            logger.warning(
                "_persist: database locked for '%s' (attempt %d/%d, retrying in %.1fs)",
                dedup_key,
                attempt + 1,
                _PERSIST_LOCK_MAX_ATTEMPTS,
                backoff,
            )
            time.sleep(backoff)

    if not salary_write_succeeded and primary_exc is not None:
        # Primary path failed. Log the failure (never-raises invariant).
        logger.warning("Failed to persist enrichment for '%s': %s", dedup_key, primary_exc)
        # #1856: When the primary failure was a lock error (all retries
        # exhausted), do NOT write the tier-only fallback bookmark. Writing
        # only the tier would mark the job as "done up to this tier" while
        # silently discarding the enriched fields (salary canonical slot, etc.)
        # that the primary UPDATE was carrying — exactly the paid-for-data loss
        # #1320's starvation pattern causes. By leaving enrichment_tier at its
        # previous value (NULL or lower), the job re-enters the pipeline on the
        # next backfill and re-attempts the enrichment, recovering the fields.
        # (jd_full was already persisted in Step 1 via _set_jd_full, and salary
        # observations were appended in Step 2 — both are lossless, so only the
        # canonical slot + tier bookmark need the requeue.)
        #
        # Non-lock errors (trigger violations I-02/I-13, etc.) still fall
        # through to the tier-only fallback: the enriched fields were rejected
        # by a DB invariant, so re-fetching would produce the same rejection.
        # The bookmark prevents an infinite re-fetch loop.
        if not _is_lock_error(primary_exc):
            _persist_tier_fallback(conn, dedup_key, tier_name)

    # --- T4.2/D20: re-evaluate a stale salary_floor dismissal ---
    # This is a SEPARATE write path from job_finder.db._jobs.upsert_job (this
    # module reconciles + writes salary via _reconcile_salary_for_write + the
    # direct UPDATE above, never through upsert_job), so it needs its own
    # copy of the re-evaluation upsert_job performs at its reconciliation
    # boundary -- see the detailed comment on that block for the confirmed
    # mechanism (SCORABLE_CANDIDATE_WHERE structurally excludes dismissed
    # rows from ever re-entering should_exclude()). Mirrored here rather than
    # centralized because the two paths commit through genuinely different
    # SQL statements; both call the same clears_salary_floor() predicate so
    # the floor arithmetic itself cannot drift between them.
    if (
        salary_write_succeeded
        and salary_cols
        and "salary_max" in salary_cols
        and config is not None
    ):
        try:
            dismissed_row = conn.execute(
                "SELECT pipeline_status, excluded_reason FROM jobs WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            if (
                dismissed_row
                and dismissed_row["pipeline_status"] == "dismissed"
                and dismissed_row["excluded_reason"] == "salary_floor"
            ):
                # PORT-SEAM: job_finder.db._persistence.update_pipeline_status -> svc.update_pipeline_status
                from jobcannon.engine.exclusion_filter import clears_salary_floor

                min_salary = (config.get("profile") or {}).get("min_salary")
                reconciled_currency = job_row.get("salary_currency") or "USD"
                if (
                    clears_salary_floor(salary_cols["salary_max"], reconciled_currency, min_salary)
                    and svc.update_pipeline_status is not None
                ):
                    # PORT-SEAM: job_finder.db._persistence.update_pipeline_status has
                    # no public counterpart and no ledger row in this port's read
                    # scope -- seamed rather than invented as a copied module.
                    svc.update_pipeline_status(
                        conn,
                        dedup_key,
                        "discovered",
                        source="ingestion",
                        evidence="salary_floor_cleared",
                    )
                    conn.execute(
                        "UPDATE jobs SET excluded_reason = NULL WHERE dedup_key = ?",
                        (dedup_key,),
                    )
                    conn.commit()
        except Exception as e:
            # Never abort enrichment persist for this bonus re-eval (module
            # design principle) -- e.g. a minimal/legacy jobs table missing
            # pipeline_status/excluded_reason columns must not crash the tier
            # bookmark write that already committed above.
            logger.warning("_persist: salary_floor re-eval failed for '%s': %s", dedup_key, e)

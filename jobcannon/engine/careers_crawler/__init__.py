# PORTED from job_finder/web/careers_crawler/__init__.py @ 46d5a2a1f27179d075efc5572efeded3ba2a0266 (private job-cannon). Ledger L-0461.
"""Active careers page crawler — re-discovers and originates job discovery.

Provides crawl_careers_batch() — a daily scheduled job that selects companies
in two lanes (#220):
1a. RE-DISCOVERY (uncapped): companies that have ever had a high-scoring job
    (classification IN ('apply','consider')), due for a re-crawl.
1b. ORIGINATION (capped at careers_crawl.origination_batch_limit, default 25):
    never-crawled companies that have a careers_url but no apply/consider
    history yet — typically NULL-ATS companies the crawler has never touched.
    This lets the crawler *originate* discovery rather than only re-discover
    proven-relevant companies.
2. Multi-tier extraction: cached API -> static HTML -> URL param search ->
   Playwright with interaction (load-more, scroll, pagination, search)
3. Feeds matched jobs into the existing upsert/score pipeline

Architecture:
- Thin orchestrator (design note PR-4 split): the per-company tier-escalation
  chain lives in ``_escalation.py`` (``_crawl_companies``), the summary-dict
  shape in ``_summary.py``. This module owns only the two-lane company
  selection query, the escalation-chain dispatch, score-triggering, and the
  activity-feed entry.
- Browser launched per invocation (inside ``_escalation._crawl_companies``),
  not kept alive between runs.
- Zero API cost — all extraction is mechanical (JSON-LD, link matching,
  form interaction, API interception).

# PORT-SEAM: the private module's re-export block (every tier/helper symbol
# re-imported here purely so ``job_finder.web.careers_crawler.X`` and test
# patches on the package namespace resolved) and its ``__all__`` list are
# DROPPED. Each symbol now lives in, and is imported from, its own definer
# module directly (``jobcannon.engine.careers_crawler._static_tier``,
# ``._playwright_tier``, etc.) — this package has no barrel-file convention
# (see CLAUDE.md). Any test that patched the package namespace for one of
# these symbols needs repointing at the real definer module.

# PORT-SEAM: the ai-navigation tier (private Tier 4, ``_try_ai_navigation``
# from the never-ported ``_ai_nav_tier.py``) is DELETED throughout this
# split. ``ai_career_navigator`` DIES (ledger L-0133): the
# ``_AI_NAV_FAILURE_THRESHOLD`` constant, the four ``ai_nav_*`` summary keys
# and ``ai_nav_failure_reasons`` dict (see ``_summary.py``), the "Discovery
# failure-rate telemetry for checkpointing" block that derived
# ``ai_nav_failure_rate``/``ai_nav_failure_threshold``/
# ``ai_nav_failure_threshold_exceeded``, and every ai_nav-referencing field
# in the final ``logger.info`` summary line are all removed, not stubbed.

# PORT-SEAM: ``crawl_careers_batch``'s own cross-submodule imports
# (``_bench_predicate``, ``_escalation``, ``_scoring``, ``_summary``,
# ``services``, ``json_utils``) are FUNCTION-SCOPED below, not module-level.
# This package's ``__init__.py`` must stay import-inert at module scope: a
# real cycle exists where ``jobcannon.engine.ats_platforms`` (via
# ``_platforms_phenom.py``) imports
# ``jobcannon.engine.careers_crawler._title_filters`` at ITS OWN module
# scope — which first runs this package's ``__init__.py``. If this
# ``__init__.py`` then imported ``_escalation`` (-> ``_cohort_legitimacy``
# -> ``_static_tier`` -> ``jobcannon.engine.ats_platforms``) at module scope
# too, that closes the loop on a module still mid-initialization
# (reproduced: ``tests/engine/test_boundary.py`` fails all 6 cases with
# "cannot import name '_title_matches' from partially initialized module").
# Matches both CLAUDE.md's "No barrel files; __init__.py files are mostly
# empty" convention and the private source's own established pattern of
# dodging this exact class of cycle via function-scoped imports (see
# ``_escalation.py``'s ``_is_blocklisted_scrape_host`` import).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRESHNESS_DAYS = 1  # Re-crawl daily to catch new postings early
# Origination lane: never-crawled companies with a careers_url but no
# apply/consider history yet (often NULL-ATS). Capped per run so the
# crawler can *originate* discovery without flooding (#220). At 25/run a
# ~1,000-company backlog drains in ~6 weeks.
_ORIGINATION_BATCH_LIMIT = 25


def __getattr__(name: str):
    """Lazy-load playwright symbols on first access (PEP 562).

    ``playwright`` is an optional heavy dependency (multi-hundred-MB browser
    download). It must not be imported at module load time so that
    ``job-cannon --help`` and ``job-cannon --version`` work in environments
    where playwright is not installed.

    The name is stored in ``globals()`` after the first import so subsequent
    accesses and ``unittest.mock.patch`` restore both work without
    re-triggering this hook.
    """
    if name == "sync_playwright":
        try:
            from playwright.sync_api import sync_playwright as _sp
        except ImportError as exc:
            raise ImportError(
                "Playwright is not installed. "
                "Install it with: pipx inject jobcannon playwright && playwright install chromium"
            ) from exc
        globals()["sync_playwright"] = _sp
        return _sp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Lane query builders
# ---------------------------------------------------------------------------
#
# Extracted to module level (rather than inlined in crawl_careers_batch)
# so tests/host/test_crawl_batch_pg_predicates.py can import and execute the
# ACTUAL production SQL string instead of a hand-copied duplicate that could
# silently drift from it (#380 review round 1, finding B1).
#
# PORT-SEAM: three shared re-shapes vs. the private SQLite queries, applied
# identically in both lanes (#380). These notes are deliberately kept as
# Python comments OUTSIDE the SQL string, never inside it: EngineCompatConnection
# .execute() (db/pool.py) runs engine_sql_to_host() on the whole string with no
# comment stripping, so a `?` quoted literally inside a `--` SQL comment gets
# counted by qmark_to_format's placeholder rewrite (db/compat.py) same as a
# real bind placeholder (#380 review round 1, finding B1: an earlier revision
# of this fix put the datetime note below inside the SQL text and broke Lane 1
# at execute time with "the query has 2 placeholders but 1 parameters").
#   - `ats_probe_status IS NOT 'hit'` -> `IS DISTINCT FROM 'hit'`
#     (Postgres-valid null-safe form; SQLite's IS NOT accepts any RHS,
#     Postgres's IS [NOT] only accepts {TRUE,FALSE,UNKNOWN,NULL}).
#   - `merged_into_id IS NULL` omitted (L-0461; same carve-out as
#     L-0018/L-0019/L-0020 -- column absent from the public schema).
#   - Lane 1's `datetime('now', ? || ' days')` (bound with a pre-negated
#     string) is rewritten to `datetime('now', '-' || ? || ' days')` (bound
#     with a plain positive int) -- the shape db/compat.py engine_sql_to_host()
#     translates for Postgres via _DATETIME_REWRITES.
#   - `c.careers_nav_recipe` dropped from both lane SELECT lists (public
#     #385 fix unit finding, filed separately). The private column
#     (`job_finder/web/migrations/m037_careers_nav_recipe.py`) has zero
#     readers anywhere in this port: its only private consumers
#     (`_ai_nav_tier.py:123`, `_tier_cache.py:99`) belong to the ai_nav
#     tier, which this package's own module docstring already documents as
#     DELETED (`ai_career_navigator` DIES, ledger L-0133). Migrating in a
#     column nothing reads would silently widen scope past #385's actual
#     ask (`careers_api_endpoint` + `careers_crawl_tier`, both of which ARE
#     read by `_escalation.py`) for no behavioral benefit.


def _lane1_query_sql(select_cols: str, bench_predicate_sql: str) -> str:
    """Lane 1 (re-discovery): proven-relevant companies due for a re-crawl."""
    return f"""SELECT {select_cols}
               FROM companies c
               WHERE c.careers_url IS NOT NULL
                 AND c.careers_scan_enabled = TRUE
                 AND c.ats_probe_status IS DISTINCT FROM 'hit'
                 AND c.careers_crawl_flag_reason IS NULL
                 AND (c.careers_crawl_last_at IS NULL
                      OR c.careers_crawl_last_at < datetime('now', '-' || ? || ' days'))
                 AND EXISTS (
                     SELECT 1 FROM jobs j
                     WHERE j.company_id = c.id
                       AND j.classification IN ('apply', 'consider')
                 )
                 AND {bench_predicate_sql}
               ORDER BY c.careers_crawl_last_at ASC NULLS FIRST"""


def _lane2_query_sql(select_cols: str, bench_predicate_sql: str) -> str:
    """Lane 2 (origination): never-crawled companies with a careers_url and
    no apply/consider history. Capped by the caller; ordered by id for
    determinism."""
    return f"""SELECT {select_cols}
               FROM companies c
               WHERE c.careers_url IS NOT NULL
                 AND c.careers_scan_enabled = TRUE
                 AND c.ats_probe_status IS DISTINCT FROM 'hit'
                 AND c.careers_crawl_flag_reason IS NULL
                 AND c.careers_crawl_last_at IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM jobs j
                     WHERE j.company_id = c.id
                       AND j.classification IN ('apply', 'consider')
                 )
                 AND {bench_predicate_sql}
               ORDER BY c.id ASC
               LIMIT ?"""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def crawl_careers_batch(config: dict) -> dict:
    """Crawl careers pages for companies with multi-tier active extraction.

    TESTING guard: returns early when config.get('TESTING') is True.

    Flow:
    1. Load batch of miss companies with careers_url, ordered by staleness
    2. For each company: escalate through the tier chain (``_escalation.py``)
    3. For each matched job: create Job object and upsert
    4. Score new jobs via the unified v3.0 scorer
    5. Log activity and update company timestamps

    Args:
        config: Application config dict.

    Returns:
        Summary dict with companies_crawled, jobs_found, jobs_new,
        scored, classified_apply, classified_consider, classified_skip,
        classified_reject, playwright_rendered, errors.
    """
    # PORT-SEAM: function-scoped, not module-level — see module docstring.
    from jobcannon.engine.careers_crawler._bench_predicate import (
        build_bench_predicate_sql,
        resolve_bench_decay_days,
    )
    from jobcannon.engine.careers_crawler._escalation import _crawl_companies
    from jobcannon.engine.careers_crawler._scoring import _score_new_jobs
    from jobcannon.engine.careers_crawler._summary import _new_summary
    from jobcannon.engine.json_utils import utc_now_iso
    from jobcannon.engine.services import get_services

    if config.get("TESTING"):
        logger.debug("crawl_careers_batch: TESTING mode — skipping")
        return _new_summary()

    profile_cfg = config.get("profile", {})
    target_titles = profile_cfg.get("target_titles", [])
    exclusions_cfg = profile_cfg.get("exclusions", {})
    title_exclusions = (
        exclusions_cfg.get("title_keywords", []) if isinstance(exclusions_cfg, dict) else []
    )

    summary: dict[str, Any] = _new_summary()
    all_new_job_keys: list[str] = []

    svc = get_services()

    # Two-lane company selection (#220):
    #   Lane 1 — RE-DISCOVERY (unchanged, uncapped): companies that have ever
    #     had a high-scoring job (classification IN ('apply','consider')).
    #   Lane 2 — ORIGINATION (new, capped): never-crawled companies that have a
    #     careers_url but NO apply/consider history yet.
    # Both lanes share the same hard gates (careers_url present, scan_enabled,
    # not an ATS 'hit', and not in the 5-strike penalty box). Lane 2 is bounded
    # by `careers_crawl.origination_batch_limit` (default 25/run).
    with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0461)
        freshness_days = config.get("careers_crawl", {}).get("freshness_days", _FRESHNESS_DAYS)
        origination_limit = config.get("careers_crawl", {}).get(
            "origination_batch_limit", _ORIGINATION_BATCH_LIMIT
        )
        # T2.3/D9: strike-decay window, configurable via
        # careers_crawl.bench_strike_decay_days. Built once per batch and
        # interpolated into both lane queries below so the two lanes cannot
        # drift on the decay window.
        bench_decay_days = resolve_bench_decay_days(config)
        bench_predicate_sql, bench_predicate_params = build_bench_predicate_sql(bench_decay_days)

        select_cols = (
            "c.id, c.name_raw, c.careers_url, c.careers_api_endpoint, c.careers_crawl_tier"
        )

        # Lane 1: re-discovery — proven-relevant companies due for a re-crawl.
        # Lane 1's SQL text places the freshness placeholder
        # (`careers_crawl_last_at < datetime(...)`) BEFORE the bench
        # predicate's own placeholder, so params are ordered
        # (freshness_days, *bench_predicate_params) to match.
        rediscovery = conn.execute(
            _lane1_query_sql(select_cols, bench_predicate_sql),
            (freshness_days, *bench_predicate_params),
        ).fetchall()

        # Lane 2: origination — never-crawled companies with a careers_url and
        # no apply/consider history. Capped and ordered by id for determinism.
        # Lane 2's SQL text places the bench predicate's placeholder BEFORE
        # the trailing `LIMIT ?`, so params are ordered
        # (*bench_predicate_params, origination_limit) to match — reversed
        # from Lane 1's order because the two lanes place the bench
        # predicate at different points in their own WHERE clause.
        origination = conn.execute(
            _lane2_query_sql(select_cols, bench_predicate_sql),
            (*bench_predicate_params, origination_limit),
        ).fetchall()

    # Re-discovery runs first (proven relevance), then the capped origination
    # cohort. De-dup defensively in case a company qualifies for both lanes.
    seen_ids: set[int] = set()
    companies = []
    for row in (*rediscovery, *origination):
        if row[0] in seen_ids:
            continue
        seen_ids.add(row[0])
        companies.append(row)

    if not companies:
        logger.info("careers_crawler: no companies due for crawling")
        return summary

    logger.info(
        "careers_crawler: %d companies in batch (%d re-discovery, %d origination)",
        len(companies),
        len(rediscovery),
        len(origination),
    )

    merged_summary, merged_keys = _crawl_companies(
        companies,
        config,
        target_titles,
        title_exclusions,
    )
    # Merge worker results into top-level summary
    for key in merged_summary:
        if key == "errors":
            summary["errors"].extend(merged_summary["errors"])
        else:
            summary[key] += merged_summary.get(key, 0)
    all_new_job_keys.extend(merged_keys)

    # --- Score newly discovered jobs (v3.0 unified scorer) ---
    if all_new_job_keys:
        _score_new_jobs(config, all_new_job_keys, summary)

    # --- Activity feed entry ---
    try:
        with svc.connection_factory() as conn:  # PORT-SEAM: seam (L-0461)
            conn.execute(
                """INSERT INTO runs
                   (timestamp, source, jobs_fetched, jobs_new, jobs_scored)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    utc_now_iso(),
                    "careers_crawl",
                    summary["jobs_found"],
                    summary["jobs_new"],
                    summary["scored"],
                ),
            )
            conn.commit()
    except Exception as e:
        logger.warning("Failed to insert careers_crawl activity entry: %s", e)

    logger.info(
        "careers_crawler complete: %d crawled, %d found, %d new, "
        "%d playwright, %d interactive, %d api-cached, %d sitemap, "
        "%d url-param, %d embedded-json, "
        "%d ats-link-promoted, %d legitimacy-flagged, "
        "%d scored (apply=%d, consider=%d, skip=%d, reject=%d)",
        summary["companies_crawled"],
        summary["jobs_found"],
        summary["jobs_new"],
        summary["playwright_rendered"],
        summary.get("interactive", 0),
        summary.get("api_cached", 0),
        summary.get("sitemap_hits", 0),
        summary.get("url_param_hits", 0),
        summary.get("embedded_json_hits", 0),
        summary.get("ats_link_promoted", 0),
        summary.get("legitimacy_flagged", 0),
        summary["scored"],
        summary.get("classified_apply", 0),
        summary.get("classified_consider", 0),
        summary.get("classified_skip", 0),
        summary.get("classified_reject", 0),
    )
    return summary

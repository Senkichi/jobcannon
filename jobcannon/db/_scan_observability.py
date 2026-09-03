"""PORTED from job_finder/db/_dashboard_queries.py @ 0db233db03855928377bd512bdd64c60b2631c43
(private job-cannon). Ledger L-0067.
# PORT-SEAM: this row's adjudicated seam splits the private module's 17
# functions across two surfaces: personal kanban/pipeline-summary functions
# (which land on jobcannon/db/_feed.py + _user_actions.py) and this
# operator-facing observability surface (crawl_latency_sli,
# off_platform_miss_log, surfaced_concentration, get_liveness_stats -- named
# explicitly in the seam). This module ports the observability surface's
# genuinely portable subset only -- see the per-function PORT-SEAM notes
# below for exactly what landed and why the rest did not.
#
# NOT PORTED (this file): get_liveness_stats, get_pipeline_summary,
# get_jobs_by_status, get_jobs_by_status_page, get_recent_activity,
# get_recent_pipeline_events, get_dashboard_stats -- every one of these
# reads `pipeline_events` (a table this host does not have) and/or private's
# rich multi-stage `jobs.pipeline_status` vocabulary (discovered, applied,
# phone_screen, ..., archived, dismissed, rejected, withdrawn). This host's
# `pipeline_status` table is per-user with a 2-value vocabulary
# (`_user_actions.py`'s `_PIPELINE_STATUSES = frozenset({"dismissed",
# "applied"})`) and no stage-transition history table at all -- the same
# architectural gap L-0066 hit for callback_rate (see
# jobcannon/db/_conversion_metrics.py's module docstring), generalized here
# across every kanban-shaped read. Building a Kanban board on this host's
# pipeline model is a product/architecture decision (how many stages, does a
# board even belong here), not a mechanical dialect port -- out of this row's
# reach. get_excluded_jobs_counts / get_excluded_jobs are also NOT ported:
# there is no `excluded_reason` column on `postings` and no writer of one on
# this host (a query-filter-ledger feature private has and this host does
# not yet build), a schema gap rather than an architecture gap but still not
# somthing this row's port can create out of nothing.
"""

from __future__ import annotations

import math
from typing import Any

from jobcannon.engine.ats_registry import (
    SCANNABLE_TARGET_PLATFORMS,
)  # PORT-SEAM: replaces private's job_finder.web.ats_registry import (same concept, host module path)
from jobcannon.engine.constants import (
    SUB_SCORE_KEYS,
)  # PORT-SEAM: used to build the sub-score mean SQL expression programmatically (CLAUDE.md rule 9: no hardcoded column lists), replacing private's already-materialized sub_score_sum column reference
from jobcannon.engine.location_normalizer import (
    normalize_for_display,
    normalize_location,
)  # PORT-SEAM: replaces private's job_finder.web.location_normalizer import (same functions, host module path)

_DEFAULT_COLD_START_EXCLUDE_DAYS = 30
# PORT-SEAM: private's _EXCLUDED_WHERE constant + get_excluded_jobs_counts /
# get_excluded_jobs are NOT ported here -- see module docstring ("no
# excluded_reason column on postings and no writer of one on this host").


def _normalized_hhi(counts: list[int]) -> float | None:
    """Compute normalized Herfindahl-Hirschman Index from group counts.

    HHI* = (Σ pᵢ² − 1/n) / (1 − 1/n) where pᵢ = countᵢ / total.
    Ranges 0 (perfectly even) → 1 (one group holds everything).
    Returns None if total == 0. Returns 1.0 if n == 1.

    Args:
        counts: List of group counts (non-negative integers).

    Returns:
        Normalized HHI (0-1) or None if total is zero.
    """
    total = sum(counts)
    if total == 0:
        return None
    n = len(counts)
    if n == 1:
        return 1.0

    # Compute sum of squared proportions
    sum_p_squared = sum((c / total) ** 2 for c in counts)

    # Normalize to [0, 1]
    numerator = sum_p_squared - (1 / n)
    denominator = 1 - (1 / n)
    return numerator / denominator


def _shannon_entropy(counts: list[int]) -> tuple[float, float] | None:
    """Compute Shannon entropy and normalized entropy from group counts.

    H = −Σ pᵢ log₂ pᵢ
    Normalized entropy = H / log₂(n)
    Returns None if total == 0 or n == 1.

    Args:
        counts: List of group counts (non-negative integers).

    Returns:
        Tuple of (entropy, normalized_entropy) or None if total is zero or n == 1.
    """
    total = sum(counts)
    if total == 0:
        return None
    n = len(counts)
    if n == 1:
        return None

    # Compute Shannon entropy
    entropy = 0.0
    for c in counts:
        if c > 0:
            p = c / total
            entropy -= p * math.log2(p)

    # Normalize by log₂(n)
    max_entropy = math.log2(n)
    normalized = entropy / max_entropy if max_entropy > 0 else 0.0

    return (entropy, normalized)


# PORT-SEAM: replaces private's target_membership_sql/surfaced_classification_sql
# (job_finder/db/_queries.py), which at this ledger row's carry_range depended
# on a materialized sub_score_sum column -- m0015's own docstring already
# documents that column was deliberately not ported ("private-side
# materialization for a sort path with no host consumer"). This host computes
# the mean of the six sub-scores directly from postings.sub_scores_json
# (jsonb, m0015) instead, built from SUB_SCORE_KEYS so the column list is
# never hand-typed (CLAUDE.md rule 9).
def _sub_score_mean_sql() -> str:
    terms = " + ".join(f"(sub_scores_json->>'{key}')::numeric" for key in SUB_SCORE_KEYS)
    return f"(({terms}) / {float(len(SUB_SCORE_KEYS))})"


def _target_membership_sql(fit_floor: float) -> str:
    """SQL boolean expression for target-set membership -- scored, mean
    sub-score >= fit_floor, classification not a hard negative
    (reject/low_signal). fit_floor is coerced to float, never
    string-interpolated raw. (# PORT-SEAM: drops private's
    json_valid(sub_scores_json) = 1 guard -- jsonb is validated at write
    time on this host, a column typed jsonb cannot hold invalid JSON the way
    a sqlite3 TEXT column could, so there is no malformed-JSON case to guard
    against here.)"""
    return (
        f"sub_scores_json IS NOT NULL AND "
        f"{_sub_score_mean_sql()} >= {float(fit_floor)} AND "
        f"classification NOT IN ('reject', 'low_signal')"
    )


_SURFACED_CLASSIFICATIONS = ("apply", "consider")


def get_crawl_latency_sli(conn: Any, config: dict) -> dict:
    """Return crawl latency SLI metrics (p50/p95/p99 days between posted_date
    and first_seen) for ATS-direct sources (exact posted_date_precision),
    excluding same-day copy artifacts and cold-start backlog.

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching this package's dispatch convention.
            (# PORT-SEAM: private said "Open sqlite3 connection".)
        config: App config dict (reads metrics.crawl_latency.cold_start_exclude_days).

    Returns dict: p50_days/p95_days/p99_days (float|None), sample_n (int),
    total_dated (int), exact_coverage_pct (float), cold_start_exclude_days (int).
    # PORT-SEAM: private's pre-m095 "missing posted_date_precision column"
    # except/fallback branch is dropped -- posted_date_precision has existed
    # on this host's postings table since m0001, there is no pre-migration
    # DB state to guard against here.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    cold_start_days = (
        config.get("metrics", {})
        .get("crawl_latency", {})
        .get("cold_start_exclude_days", _DEFAULT_COLD_START_EXCLUDE_DAYS)
    )

    total_dated = raw.execute(
        "SELECT COUNT(*) AS n FROM postings WHERE posted_date IS NOT NULL"  # PORT-SEAM: postings replaces private's jobs table
    ).fetchone()["n"]

    # PORT-SEAM: julianday(first_seen) - julianday(posted_date) (sqlite)
    # replaced by EXTRACT(EPOCH FROM ...) / 86400.0 (postgres) for the same
    # fractional-day latency; posted_date is a date column so it is promoted
    # to a timestamptz before subtraction from the timestamptz first_seen.
    # PORT-SEAM: both the same-day-copy check and the posted_date->timestamptz
    # promotion explicitly go through `AT TIME ZONE 'UTC'` rather than a bare
    # `::date` / `::timestamptz` cast -- a bare cast reads the CONNECTION's
    # session timezone (pool.py pins pooled connections to UTC, but this
    # function also accepts a bare psycopg connection per its own dispatch
    # convention, and a caller-supplied connection is not guaranteed to have
    # that pin). A bare `date::timestamptz` cast (and, confirmed empirically,
    # a bare `date AT TIME ZONE 'UTC'` with no intermediate `::timestamp`
    # cast) both interpret midnight in the session's local zone rather than
    # UTC, which would silently skew every latency value by the session's
    # UTC offset. `posted_date::timestamp AT TIME ZONE 'UTC'` is the
    # confirmed-correct form (verified against a non-UTC session: LA vs.
    # UTC): the `::timestamp` cast first produces a naive midnight, which
    # `AT TIME ZONE 'UTC'` then correctly interprets as UTC midnight rather
    # than session-local midnight. Explicit UTC keeps both this and the
    # same-day check correct regardless of the calling connection's session
    # timezone, matching the "store UTC" half of this codebase's
    # UTC-storage/local-render split.
    rows = raw.execute(
        "SELECT EXTRACT(EPOCH FROM (first_seen - (posted_date::timestamp AT TIME ZONE 'UTC'))) / 86400.0 AS lat "
        "FROM postings "
        "WHERE posted_date_precision = 'exact' "
        "AND posted_date IS NOT NULL "
        "AND posted_date <> (first_seen AT TIME ZONE 'UTC')::date "
        "AND EXTRACT(EPOCH FROM (first_seen - (posted_date::timestamp AT TIME ZONE 'UTC'))) / 86400.0 >= 0 "
        "AND EXTRACT(EPOCH FROM (first_seen - (posted_date::timestamp AT TIME ZONE 'UTC'))) / 86400.0 <= %s "
        "ORDER BY lat",
        (cold_start_days,),
    ).fetchall()

    latencies = [row["lat"] for row in rows]
    qualifying = len(latencies)

    p50_days = p95_days = p99_days = None
    if qualifying > 0:
        latencies.sort()
        p50_idx = math.ceil(0.50 * qualifying) - 1
        p95_idx = math.ceil(0.95 * qualifying) - 1
        p99_idx = math.ceil(0.99 * qualifying) - 1
        p50_days = latencies[p50_idx]
        p95_days = latencies[p95_idx]
        p99_days = latencies[p99_idx]

    exact_coverage_pct = round(100 * qualifying / total_dated, 1) if total_dated else 0.0

    return {
        "p50_days": p50_days,
        "p95_days": p95_days,
        "p99_days": p99_days,
        "sample_n": qualifying,
        "total_dated": total_dated,
        "exact_coverage_pct": exact_coverage_pct,
        "cold_start_exclude_days": cold_start_days,
    }


def get_target_set_size(conn: Any, fit_floor: float) -> int:
    """Count of postings that are target-set members (scored, mean sub-score
    >= fit_floor, not a hard negative). The single sanctioned count for
    downstream metrics, matching private's `get_target_set_size` docstring
    claim, now backed by ``_target_membership_sql`` (see above)."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    where_clause = _target_membership_sql(fit_floor)
    row = raw.execute(
        f"SELECT COUNT(*) AS n FROM postings WHERE {where_clause}"
    ).fetchone()  # PORT-SEAM: postings replaces private's jobs table
    return row["n"] if row else 0


def get_surfaced_concentration(conn: Any) -> dict:
    """Concentration metrics (normalized HHI + Shannon entropy) for surfaced
    postings (classification IN ('apply', 'consider')), grouped by_employer
    (company_id, NULL -> '_unlinked') and by_platform (companies.ats_platform
    via LEFT JOIN, NULL/empty -> '_unknown').
    # PORT-SEAM: the '_unlinked' sentinel branch is retained for structural
    # parity with private (whose jobs.company_id was nullable) but is
    # currently unreachable on this host -- m0001 declares
    # `postings.company_id bigint NOT NULL REFERENCES companies(id)`, so
    # every posting is FK-linked to a companies row at write time. Kept
    # rather than removed: harmless defensive code, and the COALESCE would
    # need to come back out if that NOT NULL constraint is ever relaxed.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    surfaced_clause = "classification IN ('apply', 'consider')"  # PORT-SEAM: private's surfaced_classification_sql() helper inlined (see module comment above _SURFACED_CLASSIFICATIONS)

    employer_rows = raw.execute(
        f"SELECT COALESCE(company_id::text, '_unlinked') AS group_key, COUNT(*) AS cnt "  # PORT-SEAM: company_id::text cast -- postings.company_id is bigint, private's was a sqlite3 dynamically-typed column that COALESCE'd against a text sentinel without needing an explicit cast
        f"FROM postings WHERE {surfaced_clause} GROUP BY group_key"
    ).fetchall()
    employer_counts = [row["cnt"] for row in employer_rows]
    employer_total = sum(employer_counts)

    employer_metrics = {
        "hhi": _normalized_hhi(employer_counts),
        "entropy": None,
        "entropy_norm": None,
        "n_groups": len(employer_counts),
        "total": employer_total,
    }
    if employer_total > 0:
        entropy_result = _shannon_entropy(employer_counts)
        if entropy_result:
            employer_metrics["entropy"], employer_metrics["entropy_norm"] = entropy_result

    # PORT-SEAM: blank-line-for-blank-line match with private's spacing here (no code change)
    platform_rows = raw.execute(
        f"SELECT COALESCE(NULLIF(c.ats_platform, ''), '_unknown') AS group_key, COUNT(*) AS cnt "
        f"FROM postings p LEFT JOIN companies c ON p.company_id = c.id "  # PORT-SEAM: postings/p replaces private's jobs/j
        f"WHERE {surfaced_clause} GROUP BY group_key"
    ).fetchall()
    platform_counts = [row["cnt"] for row in platform_rows]
    platform_total = sum(platform_counts)

    platform_metrics = {
        "hhi": _normalized_hhi(platform_counts),
        "entropy": None,
        "entropy_norm": None,
        "n_groups": len(platform_counts),
        "total": platform_total,
    }
    if platform_total > 0:
        entropy_result = _shannon_entropy(platform_counts)
        if entropy_result:
            platform_metrics["entropy"], platform_metrics["entropy_norm"] = entropy_result

    # PORT-SEAM: blank-line-for-blank-line match with private's spacing here (no code change)
    return {
        "by_employer": employer_metrics,
        "by_platform": platform_metrics,
    }  # PORT-SEAM: dict literal kept multi-line to match private's wrapping; no semantic difference


def get_off_platform_miss_log(conn: Any, fit_floor: float | None = None) -> dict:
    """Off-platform miss log with reachability classification. Postings
    whose ``sources`` jsonb array contains 'off_platform_email' are bucketed
    by reachability: reachable (tracked, scannable ATS, scan enabled --
    potential funnel-leak bug), unreachable_untracked (no companies row),
    unreachable_unsupported (tracked but ATS not scannable), or
    unreachable_scan_disabled (scannable but scan disabled). Only
    'reachable' cases are potential discovery-stage bugs.

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection. (# PORT-SEAM: private said "Open sqlite3 connection".)
        fit_floor: optional mean sub-score threshold; when supplied,
            ``reachable_above_fit_floor`` is populated via
            ``_target_membership_sql`` (else stays None).
    # PORT-SEAM: private's companies.ats_scan_enabled column is read here as
    # companies.scan_enabled -- this host has not split ats_scan_enabled/
    # careers_scan_enabled (WI-13, tracked separately as ledger L-0040's
    # precondition); the single merged scan_enabled column is the closest
    # available signal for "would this company's ATS be scanned" and is
    # already the column jobcannon/engine/stale_detector.py reads for the
    # same reachability-style branch.
    # PORT-SEAM: the unreachable_untracked bucket (company_id IS NULL) is
    # retained for structural parity with private but is currently
    # unreachable on this host for the same reason noted on
    # get_surfaced_concentration's '_unlinked' sentinel -- m0001's
    # `postings.company_id` is NOT NULL and FK-enforced, so the LEFT JOIN's
    # `c.id` can never come back NULL in practice today.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn

    rows = raw.execute(
        "SELECT p.dedup_key, p.company, p.first_seen, "
        "c.id AS company_id, c.ats_platform, c.scan_enabled "
        "FROM postings p LEFT JOIN companies c ON p.company_id = c.id "  # PORT-SEAM: postings/p replaces private's jobs/j
        "WHERE p.sources @> '[\"off_platform_email\"]'::jsonb"  # PORT-SEAM: jsonb containment replaces private's EXISTS (SELECT 1 FROM json_each(j.sources) WHERE value = ...)
    ).fetchall()

    # PORT-SEAM: blank-line-for-blank-line match with private's spacing here (no code change)
    buckets = {
        "reachable": 0,
        "unreachable_untracked": 0,
        "unreachable_unsupported": 0,
        "unreachable_scan_disabled": 0,
    }
    cases = []

    for row in rows:
        dedup_key = row["dedup_key"]
        company = row["company"]
        company_id = row["company_id"]
        ats_platform = row["ats_platform"]
        scan_enabled = row[
            "scan_enabled"
        ]  # PORT-SEAM: companies.ats_scan_enabled -> companies.scan_enabled, see docstring note above
        first_seen = row["first_seen"]

        # Classify reachability
        if company_id is None:
            # No matching companies row
            bucket = "unreachable_untracked"
        elif ats_platform is None or ats_platform not in SCANNABLE_TARGET_PLATFORMS:
            # Company tracked but ATS not scannable (NULL or unsupported platform)
            bucket = "unreachable_unsupported"
        elif (
            scan_enabled is False
        ):  # PORT-SEAM: scan_enabled is a Postgres boolean (m0003), not sqlite3's 0/1 integer -- `is False` replaces `== 0`
            bucket = "unreachable_scan_disabled"
        else:
            # Reachable (potential funnel leak)
            bucket = "reachable"

        buckets[bucket] += 1
        cases.append(
            {
                "dedup_key": dedup_key,
                "company": company,
                "ats_platform": ats_platform,
                "scan_enabled": scan_enabled,  # PORT-SEAM: ats_scan_enabled -> scan_enabled key rename, see docstring note above
                "bucket": bucket,
                "first_seen": first_seen,
            }
        )

    total = sum(buckets.values())

    # Optional: of the reachable misses, how many are actual target-set members
    # (scored, mean sub-score >= fit_floor, not a hard negative) — i.e. reachable
    # misses that are REAL discovery bugs worth acting on, not just noise. The caller
    # supplies fit_floor (module convention: see get_target_set_size); it stays None
    # when the caller has no config, so the field degrades to None rather than lying.
    reachable_above_fit_floor = None
    if fit_floor is not None:
        reachable_dedup_keys = [c["dedup_key"] for c in cases if c["bucket"] == "reachable"]
        if reachable_dedup_keys:
            where_clause = _target_membership_sql(fit_floor)
            row = raw.execute(
                f"SELECT COUNT(*) AS n FROM postings WHERE dedup_key = ANY(%s) AND {where_clause}",  # PORT-SEAM: = ANY(%s) replaces private's IN (?,?,...) placeholder expansion
                (reachable_dedup_keys,),
            ).fetchone()
            reachable_above_fit_floor = (
                row["n"] if row else 0
            )  # PORT-SEAM: row["n"] replaces row[0] -- this module's rows are dict-like (RealDictRow), not sqlite3's positional Row

    return {
        "total": total,
        "reachable": buckets["reachable"],
        "unreachable_untracked": buckets["unreachable_untracked"],
        "unreachable_unsupported": buckets["unreachable_unsupported"],
        "unreachable_scan_disabled": buckets["unreachable_scan_disabled"],
        "reachable_above_fit_floor": reachable_above_fit_floor,
        "cases": cases,
    }


def get_recent_runs(conn: Any, limit: int = 10) -> list[dict]:
    """Recent ingestion run records, newest first, excluding standing
    maintenance/observability sweeps (``metadata->>'kind' = 'maintenance'``).
    Reads the ``runs`` table this same ledger group's L-0073 port created
    (jobcannon/db/_persistence.py::log_run / jobcannon/db/migrations/m0016).
    # PORT-SEAM: private's runs.timestamp -> this host's runs.run_at
    # (m0016 renamed the column to avoid a reserved-word identifier, see
    # jobcannon/db/migrations/m0016_persistence_writer_columns.py). private's
    # json_extract(metadata, '$.kind') IS NOT 'maintenance' (sqlite3, NULL-safe
    # IS NOT) replaced by metadata->>'kind' IS DISTINCT FROM 'maintenance'
    # (postgres's NULL-safe equivalent -- a legacy/ingestion row's absent
    # 'kind' key stays included, matching private's own NULL-safety intent).
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    rows = raw.execute(
        "SELECT id, run_at, source, jobs_fetched, jobs_new, jobs_scored "
        "FROM runs WHERE metadata->>'kind' IS DISTINCT FROM 'maintenance' "
        "ORDER BY run_at DESC LIMIT %s",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_distinct_locations(conn: Any) -> list[str]:
    """Normalized, lower-case-deduped location values for the filter
    dropdown, sourced from per-entry ``locations_raw`` (jsonb array per
    posting), NOT the merged ``location`` column -- avoids the pollution
    where every unique multi-location combination becomes its own dropdown
    entry. Each entry is run through ``normalize_location`` (write-side) then
    ``normalize_for_display`` (display-side); deduplicated case-insensitively,
    first-seen casing wins.
    # PORT-SEAM: private read a JSON-encoded TEXT column and json.loads()'d
    # it in Python; postings.locations_raw is jsonb (m0001), so the pooled
    # driver already returns it as a Python list -- no json.loads needed.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    rows = raw.execute(
        "SELECT locations_raw FROM postings WHERE locations_raw IS NOT NULL AND locations_raw != '[]'::jsonb"  # PORT-SEAM: postings replaces private's jobs table; != '[]'::jsonb replaces != '' (private's TEXT-column empty-string check)
    ).fetchall()

    by_lower_key: dict[str, str] = {}
    for row in rows:
        locs = row["locations_raw"]
        if not isinstance(locs, list):
            continue
        for loc in locs:
            if not isinstance(loc, str):
                continue
            normalized = normalize_location(loc)
            if normalized is None:
                continue
            display = normalize_for_display(normalized)
            if display is None:
                continue
            key = display.lower()
            by_lower_key.setdefault(key, display)

    return sorted(by_lower_key.values(), key=str.lower)

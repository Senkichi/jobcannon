"""ADAPTED from job_finder/web/nightly_monitor/_morning.py
(run_nightly_morning_review, _compute_window_coverage, _checkpoint_summary)
@ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8 (private job-cannon). Ledger
L-0387.

Morning driver: the once-daily orchestrator that ties together audit_stage,
error_budget, review_stage, issue_filer and report into one run -- the host
equivalent of private's single 2400-line run_nightly_morning_review, split
across five files per the design note's "Files touched" section.

# PORT-SEAM: private computed window_coverage / checkpoint_summary by
# scanning local files -- ticks.jsonl for observed_ticks, checkpoint_*.json
# for observed_checkpoints and _checkpoint_summary's rejected_reasons/
# by_verdict. Neither substrate exists here. This driver re-derives a
# DEGRADED-BUT-HONEST version from the two durable sources this unit
# actually has:
#
#   - observed_ticks: COUNT of nightly_sampler procrastinate_jobs terminal
#     events (succeeded/failed) in the window -- a tick that ran at all,
#     regardless of the checkpoint verdict it produced.
#   - longest_gap_s / coverage_gap: computed via itertools.pairwise over
#     those same terminal-event timestamps (including the window edges),
#     same algorithm private used, compared against
#     monitor_cfg["coverage_gap_threshold_s"] (already ported in config.py
#     for exactly this purpose) rather than an expected-ticks-from-a-
#     hardcoded-cron-interval ratio (private's coverage_ratio) -- deriving
#     "expected" tick count from a cron string is exactly the kind of
#     hardcoded-and-must-be-kept-in-sync-by-hand element global rule #9
#     forbids, and threshold-on-longest-gap needs no such constant.
#   - observed_checkpoints / rejected_reasons: sampler.py's own module
#     docstring establishes that ONLY a FAIL verdict is durably recorded
#     (one scan_health_log ERROR row per (job, run_id), fire-once) --
#     PASS/ANOMALY verdict detail, including rejected_reasons counts, is
#     never persisted (the sampler is a PASSIVE, non-flooding observer).
#     So checkpoint_summary here reports `fail_count` (a real, queryable
#     count of nightly_sampler-sourced ERROR rows in the window) and
#     `rejected_reasons: None` (genuinely unavailable, not a fabricated
#     zero) -- disclosed in the PR body as a fidelity gap, not silently
#     invented against data that was never captured.
#
# The retry-after-reset APScheduler DateTrigger mechanism (private:
# `_ROLLING_WINDOW_RETRY_STATE`, a single in-process resubscription that
# re-ran the review a bounded time after a mid-run reset) is DROPPED
# outright, not ported: there is no APScheduler DateTrigger here, and the
# procrastinate periodic itself already retries naturally at the next
# scheduled cron tick if this run aborts -- a second, hand-rolled retry
# path duplicates what the scheduler already provides for free.
#
# ``call_model=None`` is passed explicitly to run_audit_stage/
# run_review_stage, following jobcannon.host.nightly.sampler.py's own
# established convention (checkpoint_verdict(..., call_model=None, ...)):
# a background/system periodic has no live user_id-scoped dispatcher to
# resolve, so this driver does not attempt to pull the global call_model
# off jobcannon.host.services -- see wiring.py's build_scan_services,
# which wires call_model for request-scoped work, not this periodic.
#
# state.save_state for the report fields is called IMMEDIATELY after the
# report text is finalized, BEFORE the exception-prone issue-filing tail --
# same ordering rationale as private's D12 note: a crash while filing
# issues must not also produce a false deadman alarm for a report that
# was, in fact, produced and is already durable.
"""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from jobcannon.host.health_recorder import record_scan_health
from jobcannon.host.nightly import state as _state
from jobcannon.host.nightly.audit_stage import run_audit_stage
from jobcannon.host.nightly.config import nightly_monitor_config, nightly_monitor_enabled
from jobcannon.host.nightly.disagreement_baseline import is_anomalous, rate_band, record_rate
from jobcannon.host.nightly.error_budget import build_nightly_error_budget, markdown_section
from jobcannon.host.nightly.issue_filer import cross_check_prior_filings, file_issue, list_open_issues
from jobcannon.host.nightly.report import build_report_md
from jobcannon.host.nightly.review_stage import run_review_stage

logger = logging.getLogger(__name__)

_WINDOW_HOURS = 24
_MAX_MISSED_DATES = 14

# Two different namespaces that happen to share a base string -- kept as
# separate constants deliberately (a #325/#337-adjacent bug class this
# unit's own boundary guard doesn't catch, since both are plain strings,
# not imports). ``procrastinate_jobs.task_name`` is the task's
# fully-qualified dotted path (procrastinate.tasks.Task.name defaults to
# ``full_path``, verified empirically against 3.9.0 -- see tasks.py's own
# "Registry note" docstring on `app.tasks`), NOT the bare function name;
# ``scan_health_log.payload->>'source'`` is whatever string the recording
# call passed as its own ``source=`` kwarg, which sampler.py's own
# record_scan_health call site sets to the bare "nightly_sampler" (see
# jobcannon/host/nightly/sampler.py's ``record_scan_health(source=
# "nightly_sampler", ...)`` call). tests/host/test_nightly_morning.py
# pins _SAMPLER_TASK_NAME against the live tasks.nightly_sampler.name as
# a positive control so a future rename of the task function (or a move
# to a different module) fails loudly here instead of silently zeroing
# observed_ticks/observer_offline.
_SAMPLER_TASK_NAME = "jobcannon.host.tasks.nightly_sampler"
_SAMPLER_HEALTH_SOURCE = "nightly_sampler"


def _utcnow() -> datetime:
    """Seam for tests; UTC wall clock, matching every other module in this unit."""
    return datetime.now(UTC).replace(tzinfo=None)


def _raw(conn: Any):
    return conn.raw if hasattr(conn, "raw") else conn


def _sampler_tick_timestamps(
    conn: Any, window_start: datetime, window_end: datetime
) -> list[datetime]:
    rows = (
        _raw(conn)
        .execute(
            """
            SELECT MAX(e.at) AS finished_at
            FROM procrastinate_jobs j
            JOIN procrastinate_events e ON e.job_id = j.id
            WHERE j.task_name = %s
              AND e.type IN ('succeeded', 'failed')
              AND e.at >= %s AND e.at < %s
            GROUP BY j.id
            ORDER BY finished_at
            """,
            (_SAMPLER_TASK_NAME, window_start, window_end),
        )
        .fetchall()
    )
    return [r["finished_at"] for r in rows if r["finished_at"] is not None]


def _fail_count(conn: Any, window_start: datetime, window_end: datetime) -> int:
    row = (
        _raw(conn)
        .execute(
            """
            SELECT count(*) AS n
            FROM scan_health_log
            WHERE recorded_at >= %s AND recorded_at < %s
              AND payload->>'source' = %s
              AND payload->>'level' = 'ERROR'
            """,
            (window_start, window_end, _SAMPLER_HEALTH_SOURCE),
        )
        .fetchone()
    )
    return int(row["n"]) if row else 0


def compute_window_coverage(
    conn: Any, monitor_cfg: dict, *, window_start_utc: datetime, window_end_utc: datetime
) -> tuple[dict, bool]:
    """Degraded-but-honest re-derivation of private's _compute_window_coverage.

    Returns (window_coverage, coverage_gap) -- see the module docstring's
    PORT-SEAM for why this reports a longest-gap-vs-threshold flag instead
    of private's expected-vs-observed ratio.
    """
    ticks = _sampler_tick_timestamps(conn, window_start_utc, window_end_utc)
    observed_ticks = len(ticks)

    longest_gap_s = 0.0
    if observed_ticks == 0:
        longest_gap_s = (window_end_utc - window_start_utc).total_seconds()
    else:
        edges = [window_start_utc, *ticks, window_end_utc]
        for a, b in itertools.pairwise(edges):
            gap = (b - a).total_seconds()
            longest_gap_s = max(longest_gap_s, gap)

    threshold = monitor_cfg["coverage_gap_threshold_s"]
    coverage_gap = longest_gap_s > threshold

    window_coverage = {
        "window_start_utc": window_start_utc.isoformat(),
        "window_end_utc": window_end_utc.isoformat(),
        "observed_ticks": observed_ticks,
        "longest_gap_s": longest_gap_s,
        "coverage_gap_threshold_s": threshold,
        "coverage_ratio": (1.0 if observed_ticks else 0.0)
        if not ticks
        else max(0.0, 1.0 - (longest_gap_s / (window_end_utc - window_start_utc).total_seconds())),
    }
    return window_coverage, coverage_gap


def checkpoint_summary(conn: Any, window_start_utc: datetime, window_end_utc: datetime) -> dict:
    """Degraded re-derivation of private's _checkpoint_summary -- see the
    module docstring's PORT-SEAM: only FAIL verdicts are durably queryable
    on this host, so ``rejected_reasons`` is None (genuinely unavailable),
    not a fabricated zero."""
    return {
        "fail_count": _fail_count(conn, window_start_utc, window_end_utc),
        "rejected_reasons": None,
        "by_verdict": {},
    }


def run_morning_review(*, _now: datetime | None = None) -> dict:
    """One out-of-process morning review run; never raises into the host
    periodic worker.

    Checks the kill switch and opens its own pooled connection, mirroring
    run_sampler_tick / run_deadman_check's convention (jobcannon.host.tasks
    owns the periodic wrapper).
    """
    if not nightly_monitor_enabled():
        return {"skipped": "disabled"}
    try:
        from jobcannon.db import connection_factory

        with connection_factory() as conn:
            return _run(conn, _now=_now)
    except Exception:
        logger.warning("nightly morning review failed", exc_info=True)
        return {"skipped": "review failed"}


def _run(conn: Any, *, _now: datetime | None = None) -> dict:
    monitor_cfg = nightly_monitor_config()
    now = _now or _utcnow()
    window_end_utc = now
    window_start_utc = window_end_utc - timedelta(hours=_WINDOW_HOURS)
    date_str = now.strftime("%Y-%m-%d")

    state = _state.load_state(conn)
    base_state = state

    window_coverage, coverage_gap = compute_window_coverage(
        conn, monitor_cfg, window_start_utc=window_start_utc, window_end_utc=window_end_utc
    )
    observer_offline = window_coverage["observed_ticks"] == 0 or coverage_gap
    ckpt_summary = checkpoint_summary(conn, window_start_utc, window_end_utc)

    audit_summary = run_audit_stage(conn, monitor_cfg, call_model=None, config={}, rng=None)

    error_budget = build_nightly_error_budget(
        conn, monitor_cfg, window_start_utc=window_start_utc, window_end_utc=window_end_utc
    )

    audit_cfg = monitor_cfg["audit"]
    review_cfg = monitor_cfg["review"]
    history = record_rate(
        state.get("disagreement_rate_history"),
        audit_summary.get("disagreement_rate"),
        audit_summary.get("audited", 0),
        min_sample=audit_cfg["min_sample_size_for_rate"],
        window_runs=audit_cfg["disagreement_baseline_window_nights"],
    )
    band = rate_band(history, min_history=audit_cfg["disagreement_baseline_min_nights"])
    rate = audit_summary.get("disagreement_rate")
    disagreement_rate_anomalous = (
        is_anomalous(
            rate,
            band,
            tolerance=audit_cfg["disagreement_baseline_tolerance"],
            absolute_floor=audit_cfg["disagreement_baseline_absolute_floor"],
        )
        if isinstance(rate, (int, float))
        else False
    )

    repo = review_cfg.get("repo")
    prior_filed = state.get("last_filed_issues") or []
    if repo:
        token = review_cfg.get("token")
        open_issues = list_open_issues(repo, token) if token else {
            "status": "unavailable",
            "reason": "JC_NIGHTLY_GH_TOKEN not set",
            "issues": [],
        }
        open_issues = cross_check_prior_filings(open_issues, prior_filed)
    else:
        open_issues = {"status": "unavailable", "reason": "JC_NIGHTLY_ISSUE_REPO not set", "issues": []}

    review_result = run_review_stage(
        date_str=date_str,
        conn=conn,
        config={},
        call_model=None,
        audit_summary=audit_summary,
        checkpoint_summary=ckpt_summary,
        window_coverage=window_coverage,
        observer_offline=observer_offline,
        error_budget=error_budget,
        disagreement_alarm_rate=audit_cfg["disagreement_alarm_rate"],
        disagreement_alarm_min_sample=audit_cfg["disagreement_alarm_min_sample"],
        disagreement_rate_anomalous=disagreement_rate_anomalous,
        open_issues=open_issues,
        prior_filed=prior_filed,
    )

    missed_dates: list[str] = list(state.get("last_missed_report_dates") or [])
    last_date = state.get("last_report_date")
    if last_date and last_date != date_str:
        try:
            gap_days = (
                datetime.strptime(date_str, "%Y-%m-%d") - datetime.strptime(last_date, "%Y-%m-%d")
            ).days
        except ValueError:
            gap_days = 0
        if gap_days > 1:
            missed_dates = (missed_dates + [f"gap after {last_date}"])[-_MAX_MISSED_DATES:]

    report_md = build_report_md(
        date_str=date_str,
        observer_offline=observer_offline,
        window_coverage=window_coverage,
        audit_summary=audit_summary,
        review_result=review_result,
        error_budget_md=markdown_section(error_budget),
        missed_report_dates=missed_dates,
    )

    status = "incomplete" if review_result.get("incomplete") else "ok"
    new_state = {
        **state,
        "last_report_at": now.isoformat(),
        "last_report_date": date_str,
        "last_morning_status": status,
        "last_audit_summary": audit_summary,
        "last_missed_report_dates": missed_dates,
        "disagreement_rate_history": history,
        "disagreement_baseline_prior": band,
    }
    _state.save_state(conn, new_state, base_state)

    record_scan_health(
        source="nightly_monitor",
        kind="nightly_morning_report",
        level="INFO",
        date=date_str,
        status=status,
        disputes=audit_summary.get("disputes", 0),
        audited=audit_summary.get("audited", 0),
    )

    # Filing requires BOTH a repo and a token to have resolved cleanly AND
    # a trustworthy dedup reference (open_issues fetched ok, including the
    # #1506 cross_check_prior_filings check above) -- any one missing means
    # "do not file," matching require_issue_repo/require_issue_token's
    # fail-loud contract without re-fetching config already read above.
    filed: list[dict] = []
    token = review_cfg.get("token")
    if repo and token and open_issues.get("status") == "ok":
        for issue in review_result.get("issues_to_file", []):
            filed.append(
                file_issue(repo, token, issue["title"], issue["body"], issue.get("labels") or [])
            )
    elif review_result.get("issues_to_file"):
        logger.warning(
            "nightly review: %d issue(s) proposed but not filed (repo=%s token=%s open_issues=%s)",
            len(review_result["issues_to_file"]),
            bool(repo),
            bool(token),
            open_issues.get("status"),
        )
    if filed:
        _state.save_state(
            conn,
            {**new_state, "last_filed_issues": filed},
            new_state,
        )

    return {
        "date": date_str,
        "status": status,
        "observer_offline": observer_offline,
        "audit": audit_summary,
        "issues_filed": len(filed),
        "report_md": report_md,
    }

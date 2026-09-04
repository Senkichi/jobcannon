"""ADAPTED from job_finder/web/nightly_monitor/_sampler.py
@ e1f47695b07f928e6c91cc64767c97a99645d68f (private job-cannon).
Ledger L-0471.

The sampler tick. PASSIVE OBSERVER -- this module must never import or call
any scheduler mutation API (defer/cancel/retry against a job it is merely
observing) and never writes rows outside scan_health_log (through the
sanctioned jobcannon.host.health_recorder.record_scan_health) and
nightly_monitor_state (through jobcannon.host.nightly.state, this unit's
sole writer for that table).

# PORT-SEAM: private read app.log + run_events.jsonl incrementally by byte
# offset. There is no file to tail on this host; the two watermarks in
# nightly_monitor_state (scan_health_watermark_id, procrastinate_watermark_id
# -- see state.py's module docstring) are DB cursors over
# scan_health_log.id and procrastinate_jobs.id instead. Log rotation logic
# (offset > size => reset) has nothing left to reset: a bigserial id cursor
# has no rotation.
#
# monitored / unmapped: private tracked a static id->ledger-name mapping and
# an "unmapped streak" counter for scheduler job ids that hadn't been added
# to that mapping yet (issue #1175's fix, then generalized). There is
# nothing to map here: a procrastinate task's `task_name` (procrastinate_
# jobs.task_name) IS the canonical job identity, already unique and stable
# by construction -- no id ambiguity exists to track a streak against, so
# unmapped_streaks and monitored_ledger_names are absent, not ported.
# Every terminal procrastinate_jobs row this tick reads is processed;
# "monitored" is simply "every task this host runs."
#
# Concurrent-run log attribution (_run_window / _concurrent_windows /
# _run_log_excerpts / _hits_for_run / _parse_run_id_start_ts) is DELETED,
# not ported: that machinery existed to answer "which lines of one shared
# app.log belong to which of several concurrently-running jobs," a question
# that does not exist here. jobcannon.host.health_recorder.record_scan_health
# writes one structured jsonb row per call, and the writer already names
# itself in the payload (every existing caller passes `source=...`, e.g.
# jobcannon.host.tasks.db_storage_check's `record_scan_health(source=
# "db_storage_check", **status)`). Attributing a scan_health_log row to a
# job is therefore a payload-field lookup against `source`, not a
# time-window reconstruction -- so db_delta_attributable is always True and
# shared_signature_hits/concurrent_context are always empty on this host
# (checkpoint_packet.py's module docstring covers why those fields still
# exist on the packet rather than being removed).
#
# error / result: private's run_end events carried the scheduler's own
# `error`/`result` fields, captured at the point of failure/completion.
# procrastinate's job table (verified against the pinned 3.9.0 schema,
# .venv/Lib/site-packages/procrastinate/sql/schema.sql) persists no
# exception text or return value anywhere queryable -- only
# procrastinate_events(job_id, type, at) timestamps and the terminal
# `status` enum. Both packet fields are therefore always None here; this is
# a genuine hosted-substrate gap (distinct from the db_delta gap in
# checkpoint_packet.py) and is listed as a follow-up, not invented.
#
# call_model stays unwired (None) on every checkpoint_verdict call below --
# see checkpoint_verdict.py's module docstring for why that is the correct,
# structurally-enforced "no LLM spend" behavior for this dark rollout, not
# an oversight. The signature registry passed to match_signatures is `[]`
# for the same reason signatures.py's module docstring gives: no registry
# is configured on this host yet, so this tick's signature-matching code
# path is exercised but currently inert -- correctness now, real signal the
# day a registry is added, matching this port's "wire it correctly, do not
# invent config that has no consumer yet" rule elsewhere in this unit.
#
# FAIL escalation writes one scan_health_log ERROR row (through
# record_scan_health) per (job, run_id), fire-once via
# jobcannon.host.nightly.state's notified list -- replacing private's
# desktop-toast `notify()` call, which has no meaning on a multi-tenant
# server, and its issue-filing path, which this dark unit does not carry
# (see signatures.py's module docstring: "no LLM spend / no issues filed").
"""

from __future__ import annotations

import logging
import time
from typing import Any

from jobcannon.host.health_recorder import record_scan_health
from jobcannon.host.nightly import state as _state
from jobcannon.host.nightly.baselines import duration_band
from jobcannon.host.nightly.checkpoint_packet import (
    LOG_EXCERPT_STATUS_CAPTURED_EMPTY,
    LOG_EXCERPT_STATUS_CAPTURED_NON_EMPTY,
    build_packet,
)
from jobcannon.host.nightly.checkpoint_verdict import checkpoint_verdict
from jobcannon.host.nightly.config import nightly_monitor_config, nightly_monitor_enabled
from jobcannon.host.nightly.signatures import match_signatures

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = ("succeeded", "failed")
_STATUS_TO_DISPOSITION = {"succeeded": "completed", "failed": "failed"}

# Generous cap on how many new terminal jobs one tick will even fetch from
# the DB, independent of max_events_per_tick (which caps how many get a
# verdict computed). This just bounds one query's result size; in practice
# a 4-minute-cadence tick will never see this many terminal jobs at once.
_FETCH_CAP = 200

# Currently-configured signature registry. Empty by construction on this
# host today -- see module docstring.
_SIGNATURE_REGISTRY: list[dict] = []


def _raw(conn: Any):
    return conn.raw if hasattr(conn, "raw") else conn


def _terminal_jobs_with_duration(
    conn: Any, *, task_name: str | None, since_id: int, limit: int
) -> list[dict]:
    """Terminal (succeeded/failed) procrastinate_jobs rows, each carrying a
    computed ``duration_s`` from procrastinate_events' 'started' ->
    terminal-event timestamps.

    Two call shapes:

    * ``task_name=None`` -- every task, ``id > since_id``, ascending: "what's
      new since the watermark," the tick's own checkpoint queue.
    * ``task_name=<name>`` -- one task, most recent ``limit`` rows
      (``since_id`` ignored, always 0), returned in ascending id order:
      history for ``duration_band``, which expects chronological order.

    A job with no matching 'started' event (should not happen in practice,
    but procrastinate's event trigger is out of this port's control) gets
    ``duration_s: None``; ``duration_band`` already treats a non-numeric
    duration as unusable for banding.
    """
    raw = _raw(conn)
    query = (
        "SELECT j.id, j.task_name, j.status, "
        "MIN(e.at) FILTER (WHERE e.type = 'started') AS started_at, "
        "MAX(e.at) FILTER (WHERE e.type IN ('succeeded', 'failed')) AS finished_at "
        "FROM procrastinate_jobs j "
        "JOIN procrastinate_events e ON e.job_id = j.id "
        "WHERE j.status = ANY(%s) AND {filter} "
        "GROUP BY j.id, j.task_name, j.status "
        "ORDER BY j.id {order} LIMIT %s"
    )
    if task_name is None:
        rows = raw.execute(
            query.format(filter="j.id > %s", order="ASC"),
            (list(_TERMINAL_STATUSES), since_id, limit),
        ).fetchall()
    else:
        rows = list(
            reversed(
                raw.execute(
                    query.format(filter="j.task_name = %s", order="DESC"),
                    (list(_TERMINAL_STATUSES), task_name, limit),
                ).fetchall()
            )
        )
    out = []
    for row in rows:
        started = row["started_at"]
        finished = row["finished_at"]
        duration_s = (finished - started).total_seconds() if started and finished else None
        out.append(
            {
                "id": row["id"],
                "task_name": row["task_name"],
                "status": row["status"],
                "duration_s": duration_s,
            }
        )
    return out


def _as_run_end_event(job_row: dict) -> dict:
    """Shape one terminal-job row as the run_end-event dict duration_band expects."""
    return {
        "event": "run_end",
        "job": job_row["task_name"],
        "disposition": _STATUS_TO_DISPOSITION.get(job_row["status"], job_row["status"]),
        "duration_s": job_row["duration_s"],
    }


def _new_scan_health_hits(
    conn: Any, since_id: int, registry: list[dict]
) -> tuple[list[dict], int]:
    """Match ``registry`` against new scan_health_log payloads since ``since_id``.

    Returns ``(hits, new_watermark)``. Each hit carries the row's own
    ``payload["source"]`` (the writer's self-identification -- see module
    docstring) so the tick loop below can attribute it to a job without any
    log-window reconstruction. ``new_watermark`` is the highest id read this
    tick (or ``since_id`` unchanged when there were no new rows), independent
    of ``registry`` being empty -- the watermark must still advance so an
    empty registry does not cause the same rows to be re-read forever.
    """
    raw = _raw(conn)
    rows = raw.execute(
        "SELECT id, payload FROM scan_health_log WHERE id > %s ORDER BY id LIMIT %s",
        (since_id, _FETCH_CAP),
    ).fetchall()
    if not rows:
        return [], since_id
    hits: list[dict] = []
    if registry:
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            for hit in match_signatures(payload, registry):
                hits.append({**hit, "source": payload.get("source")})
    return hits, rows[-1]["id"]


def run_sampler_tick() -> dict | None:
    """One tick; never raises into the host periodic worker.

    Checks the kill switch and opens its own pooled connection -- callers
    should not pass one in, since the flag check exists specifically to
    avoid connecting to the database at all when the monitor is disabled
    (see jobcannon.host.tasks.nightly_sampler, the periodic wrapper).
    """
    if not nightly_monitor_enabled():
        return None
    try:
        from jobcannon.db import connection_factory

        with connection_factory() as conn:
            return _tick(conn)
    except Exception:
        logger.warning("nightly sampler tick failed", exc_info=True)
        return None


def _tick(conn: Any) -> dict:
    cfg = nightly_monitor_config()
    state = _state.load_state(conn)
    base_state = state

    hits, new_scan_health_watermark = _new_scan_health_hits(
        conn, state["scan_health_watermark_id"], _SIGNATURE_REGISTRY
    )

    max_events = cfg["max_events_per_tick"]
    budget = cfg["tick_budget_seconds"]
    # A tick_budget_seconds of 0.0 disables the wall-clock budget.
    budget_end = time.monotonic() + budget if budget > 0 else float("inf")

    terminal_jobs = _terminal_jobs_with_duration(
        conn, task_name=None, since_id=state["procrastinate_watermark_id"], limit=_FETCH_CAP
    )

    checkpoints: list[str] = []
    checkpoint_count = 0
    processed_job_id = state["procrastinate_watermark_id"]
    drained = True

    for job_row in terminal_jobs:
        if checkpoint_count >= max_events or time.monotonic() >= budget_end:
            drained = False
            break

        job_name = job_row["task_name"]
        run_id = str(job_row["id"])
        disposition = _STATUS_TO_DISPOSITION.get(job_row["status"], job_row["status"])

        # Exclude the run being judged from its own baseline (same rationale
        # as private: self-inclusion biases the band toward the outlier).
        history = _terminal_jobs_with_duration(
            conn, task_name=job_name, since_id=0, limit=cfg["baseline_window_runs"] + 1
        )
        history_events = [_as_run_end_event(h) for h in history if h["id"] != job_row["id"]]
        band = duration_band(
            job_name,
            history_events,
            window_runs=cfg["baseline_window_runs"],
            min_history=cfg["baseline_min_history"],
        )

        own_hits = [h for h in hits if h.get("source") == job_name]
        log_excerpt_status = (
            LOG_EXCERPT_STATUS_CAPTURED_NON_EMPTY
            if own_hits
            else LOG_EXCERPT_STATUS_CAPTURED_EMPTY
        )
        log_excerpt = "\n".join(str(h) for h in own_hits)

        packet = build_packet(
            {
                "job": job_name,
                "run_id": run_id,
                "disposition": disposition,
                "duration_s": job_row["duration_s"],
                "db_delta": None,
                "result": None,
                "error": None,
            },
            hits=own_hits,
            log_excerpt=log_excerpt,
            band=band,
            tolerance=cfg["out_of_band_tolerance"],
            absolute_floor_s=cfg["out_of_band_absolute_floor_s"],
            db_delta_tracked=None,
            log_excerpt_status=log_excerpt_status,
        )
        verdict = checkpoint_verdict(packet, call_model=None, conn=conn)
        checkpoints.append(verdict["verdict"])

        if verdict["verdict"] == "FAIL":
            incident_key = f"nightly_checkpoint_fail:{job_name}:{run_id}"
            if not _state.already_notified(state, incident_key):
                record_scan_health(
                    source="nightly_sampler",
                    level="ERROR",
                    job=job_name,
                    run_id=run_id,
                    verdict=verdict["verdict"],
                    reasons=verdict["reasons"],
                    forced=verdict["forced"],
                )
                state = _state.mark_notified(state, incident_key)

        checkpoint_count += 1
        processed_job_id = job_row["id"]

    # Advance the watermark only up to the last row this tick actually
    # checkpointed -- a capped/budget-truncated tick leaves it short so the
    # next tick resumes at the same job, same rule as private's
    # app_log_offset/run_events_offset advance-only-on-drained-tick.
    new_procrastinate_watermark = (
        terminal_jobs[-1]["id"] if drained and terminal_jobs else processed_job_id
    )

    state = {
        **state,
        "scan_health_watermark_id": new_scan_health_watermark,
        "procrastinate_watermark_id": new_procrastinate_watermark,
    }
    _state.save_state(conn, state, base_state)

    return {
        "new_scan_health_rows": new_scan_health_watermark - base_state["scan_health_watermark_id"],
        "new_terminal_jobs": len(terminal_jobs),
        "checkpoints": checkpoints,
        "capped": not drained,
    }

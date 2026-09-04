"""ADAPTED from job_finder/web/nightly_monitor/_checkpoint.py (packet half)
@ e1f47695b07f928e6c91cc64767c97a99645d68f (private job-cannon).
Ledger L-0471.

Checkpoint evidence-packet assembly. checkpoint_verdict.py (the other half
of this port's file split) consumes build_packet's output and calls the
injected verdict model.

"Unchanged" / "byte-identical" below refer to logic, verified by a
name-and-body extraction diff of every top-level function against the
private _checkpoint.py source at the pinned SHA (zero body drift beyond the
documented seams). Docstrings and comments are NOT verbatim -- they are
rewrapped to this repo's line length and re-cited to its ``issue #N``
convention (private used bare ``#N``) -- so a raw text diff against the
private original is expected to show prose-only hunks beyond the split
itself.

# PORT-SEAM: private's build_packet took a single run_end: dict, assembled
# from job_finder/web/run_events.jsonl by an event writer outside
# nightly_monitor/. There is no run_events.jsonl on this host; the hosted
# caller (jobcannon.host.nightly.sampler) assembles the equivalent shape
# from a procrastinate_jobs row (job/run_id/disposition/duration_s/result/
# error) plus scan_health_log rows (signature hits, log excerpt text) and
# passes it to build_packet in the same run_end shape, so the function
# body below is otherwise unchanged.
#
# db_delta / jd_full_loss_excess: private's run_end carried a db_delta dict
# -- jobs-table counter deltas (total_jobs, scoring_backlog,
# classification_null, missing_jd_full, first_seen_today) computed by
# whatever wrote the run_end event -- and a jd_full_loss_excess invariant
# (job_finder.web.run_events.jd_full_loss_excess) derived from it. Nothing
# in this ledger unit computes an equivalent counter-delta on this host
# (that would need a before/after query around each task's window against
# the jobs table -- unscoped, separate work), so db_delta arrives as None
# from every hosted caller today and jd_full_loss_excess is DROPPED
# outright rather than stubbed: a stub that always returns 0 would look
# implemented when it is not. The guard machinery below
# (_db_delta_summary, and checkpoint_verdict.py's _sanitize_verdict /
# _guard_non_attributable_db_delta / _guard_new_row_backlog) stays
# byte-identical -- it is a pure function over whatever the packet
# carries, degrades safely to "unchanged" labels when db_delta is absent,
# and picks up real signal for free the day a later unit wires a db_delta
# source in.
#
# concurrent_run_ids / shared_signature_hits / concurrent_context: private
# needed these because multiple job processes wrote to ONE shared app.log,
# so a log-tail excerpt could contain another job's lines and a db_delta
# diff could inherit another job's writes.
# jobcannon.host.health_recorder.record_scan_health writes one structured
# jsonb row per scan -- there is no shared log to reconstruct ownership
# from, so nothing on this host needs to compute a "concurrent window."
# The parameters and packet fields stay (build_packet still accepts and
# emits them; checkpoint_verdict.py's guards branch on them unchanged) so
# the guard logic is never hand-edited to remove a branch -- every hosted
# caller today simply passes nothing for them, which makes
# db_delta_attributable always True and shared_signature_hits/
# concurrent_context always empty. That is a caller fact, not a shape
# change.
#
# log_excerpt_status: private's three-state capture outcome (issue #2013)
# distinguished a correctly-scoped-but-quiet run (captured_empty) from a
# run whose ownership window could not be established at all
# (capture_unavailable) -- a distinction that existed because log-tail
# capture could fail structurally. On this host there is no log-tail
# capture step to fail that way: a scan_health_log row either carries
# excerpt text or it does not. The hosted caller therefore only ever
# passes captured_non_empty or captured_empty; capture_unavailable has no
# hosted producer today but is kept as a value (and the tri-state
# normalization below is kept) because checkpoint_verdict.py's guards
# branch on all three, and a future capture-failure mode (e.g. a task
# whose scan_health_log row never lands) may need it.
"""

from __future__ import annotations

from jobcannon.host.nightly.baselines import out_of_band

_LOG_EXCERPT_CAP = 4000

# Three-state capture outcome for log_excerpt (see module docstring).
LOG_EXCERPT_STATUS_CAPTURED_NON_EMPTY = "captured_non_empty"
LOG_EXCERPT_STATUS_CAPTURED_EMPTY = "captured_empty"
LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE = "capture_unavailable"
LOG_EXCERPT_STATUS_VALUES = frozenset(
    {
        LOG_EXCERPT_STATUS_CAPTURED_NON_EMPTY,
        LOG_EXCERPT_STATUS_CAPTURED_EMPTY,
        LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE,
    }
)
# log_excerpt_is_job_scoped is derived: true only when the capture produced a
# correctly-scoped excerpt (non-empty or genuinely-empty), false when the
# capture was unavailable.
_LOG_EXCERPT_STATUS_SCOPED = frozenset(
    {LOG_EXCERPT_STATUS_CAPTURED_NON_EMPTY, LOG_EXCERPT_STATUS_CAPTURED_EMPTY}
)

# Jobs-table counters a db_delta MAY carry (see module docstring: no hosted
# caller populates these yet). The direction is the one that represents an
# improvement for the user: more jobs / first-seen jobs are good; fewer
# unclassified, unscored, or jd_full-missing jobs are good.
_DB_DELTA_COUNTER_KEYS = (
    "total_jobs",
    "scoring_backlog",
    "classification_null",
    "missing_jd_full",
    "first_seen_today",
)

_DB_DELTA_IMPROVEMENT: dict[str, str] = {
    "total_jobs": "increase",
    "first_seen_today": "increase",
    "scoring_backlog": "decrease",
    "classification_null": "decrease",
    "missing_jd_full": "decrease",
}

# Decrease-direction jobs-table counters whose growth can be the arithmetic
# consequence of newly inserted rows: a freshly ingested row is by
# construction unclassified and jd_full-missing, so each of these rises by
# at most the number of new rows the same run inserted (issue #1893).
# scoring_backlog is deliberately excluded: it only counts rows that already
# have jd_full, which a freshly inserted row does not, so its growth is
# never new-row-bounded.
_NEW_ROW_BOUNDED_COUNTERS = frozenset({"classification_null", "missing_jd_full"})


def _new_row_count(raw: dict) -> int:
    """Number of freshly inserted job rows in this run's window.

    Measured by the positive increase in the increase-direction counters
    (``total_jobs`` / ``first_seen_today``). Both measure new rows --
    ``first_seen_today`` directly, ``total_jobs`` assuming no deletions in
    the window -- so the max is a safe upper bound on the rows a
    decrease-direction counter can be expected to gain. Returns 0 when
    neither rose, so any increase in a bounded counter is then genuine
    backlog accumulation.
    """
    best = 0
    for key in ("total_jobs", "first_seen_today"):
        val = raw.get(key, 0)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            best = max(best, int(val))
    return best


def _db_delta_summary(
    raw_delta: dict | None, tracked: bool | None, attributable: bool = True
) -> dict:
    """Convert raw signed db_delta into labelled, direction-aware facts.

    The returned ``tracked`` value is the explicit ``db_delta_tracked`` flag
    from the caller when it is a boolean; otherwise it is ``None`` to signal
    that the verdict layer must use the rest of the packet to decide.

    When ``attributable`` is false, the delta is a database-wide counter
    diff that overlapped at least one other concurrently-running job, so the
    movement cannot be attributed to this run. Every counter is labelled
    ``not_attributable`` instead of ``improved_by_N`` / ``worsened_by_N`` so
    the verdict model and the post-return reason guard cannot reason from a
    movement this run did not necessarily cause (issue #1734).

    For an attributable delta, a decrease-direction counter in
    ``_NEW_ROW_BOUNDED_COUNTERS`` whose increase is no larger than the run's
    new-row growth (``total_jobs`` / ``first_seen_today`` increase) is
    labelled ``pending_from_new_rows_N``: the movement is the arithmetic
    consequence of newly created rows (each freshly inserted row is
    unclassified and jd_full-missing), not backlog accumulation. Growth
    exceeding the new-row count is genuine backlog and is labelled
    ``worsened_by_N`` with ``N`` the *excess* over new rows, so the model
    never sees a misleading full-count ``worsened_by_N`` (issue #1893).
    """
    raw = raw_delta if isinstance(raw_delta, dict) else {}
    summary: dict = {"tracked": tracked, "attributable": attributable, "by_counter": {}}
    new_rows = _new_row_count(raw) if attributable else 0
    for key in _DB_DELTA_COUNTER_KEYS:
        val = raw.get(key, 0)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            val = 0
        direction = _DB_DELTA_IMPROVEMENT.get(key)
        if not attributable:
            # The delta is a database-wide counter diff that overlapped at
            # least one other run; every counter is non-attributable,
            # including zeros (a zero is not evidence of "no work" any more
            # than a non-zero is evidence of this run's work).
            label = "not_attributable"
        elif val == 0:
            label = "unchanged"
        elif (direction == "increase" and val > 0) or (direction == "decrease" and val < 0):
            label = f"improved_by_{abs(int(val))}"
        elif direction == "decrease" and key in _NEW_ROW_BOUNDED_COUNTERS and new_rows > 0:
            # Bounded downstream-backlog growth (issue #1893): the counter
            # rose because new rows were inserted, not because existing rows
            # lost coverage. The portion within new_rows is pending
            # (non-escalating); any excess is genuine backlog accumulation.
            ival = int(val)
            if ival <= new_rows:
                label = f"pending_from_new_rows_{ival}"
            else:
                label = f"worsened_by_{ival - new_rows}"
        else:
            label = f"worsened_by_{abs(int(val))}"
        summary["by_counter"][key] = {
            "raw_delta": int(val),
            "label": label,
            "improvement_direction": direction,
        }
    # Preserve any future/unknown counters so the prompt is not lossy.
    for key, val in raw.items():
        if key not in _DB_DELTA_COUNTER_KEYS:
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                label = "not_attributable" if not attributable else (f"changed_by_{abs(int(val))}")
                summary["by_counter"][key] = {
                    "raw_delta": int(val),
                    "label": label,
                    "improvement_direction": None,
                }
    return summary


def job_tracks_db_delta_from_history(job_name: str, events: list[dict]) -> bool | None:
    """Return whether ``job_name`` has historically moved the jobs-table counters.

    Jobs whose legitimate work does not affect the jobs-table counters
    (backup, company linkage, registry hygiene, etc.) will consistently show
    all-zero ``db_delta`` and are marked ``False``. Ingestion, enrichment,
    scoring, and other jobs that do move these counters will show at least
    one non-zero value across history and are marked ``True``.

    Only *attributable* run_ends are considered: a run whose window
    overlapped another concurrently-running job (``concurrent_run_ids``
    non-empty) has a database-wide counter diff that may inherit the other
    job's writes, so its non-zero delta is not evidence that *this* job
    moves the counters (issue #1734). Run_ends without a
    ``concurrent_run_ids`` field (older events) are treated as attributable
    -- the overlap signal was not captured at emission time.

    Returns ``None`` when ``events`` contains no attributable run_end
    records for the job, so the caller can treat tracking status as unknown
    rather than guessed. No hosted caller populates this today (see module
    docstring); it is kept for the day a run-history source exists.
    """
    seen = False
    for e in events:
        if e.get("event") != "run_end" or e.get("job") != job_name:
            continue
        # A non-attributable run_end's delta may belong to a concurrent job;
        # it cannot establish that this job tracks db_delta.
        if e.get("concurrent_run_ids"):
            continue
        seen = True
        delta = e.get("db_delta")
        if not isinstance(delta, dict):
            continue
        for k, v in delta.items():
            if (
                k in _DB_DELTA_COUNTER_KEYS
                and isinstance(v, (int, float))
                and not isinstance(v, bool)
                and v != 0
            ):
                return True
    return None if not seen else False


def build_packet(
    run_end: dict,
    *,
    hits: list[dict],
    shared_signature_hits: list[dict] | None = None,
    log_excerpt: str,
    concurrent_context: str | None = None,
    band: dict,
    tolerance: float | None = None,
    absolute_floor_s: float | None = None,
    db_delta_tracked: bool | None = None,
    log_excerpt_status: str = LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE,
) -> dict:
    """Assemble the evidence packet for one run_end-shaped event.

    ``log_excerpt_status`` records the capture outcome as a three-state
    value (issue #2013): ``captured_non_empty`` (run-owned lines found and
    present), ``captured_empty`` (capture ran against a correctly
    identified window but the job emitted no matching lines), or
    ``capture_unavailable`` (no run-owned window could be established --
    see module docstring for which of these the hosted caller can actually
    emit). The legacy ``log_excerpt_is_job_scoped`` boolean is derived from
    this: true only for the two ``captured_*`` states, false for
    ``capture_unavailable``. ``concurrent_context`` carries lines from the
    same time window that also overlap another concurrently-running job's
    window; it is cross-job noise and must not be treated as this run's own
    output (issue #1488).

    ``band_assessment`` is a three-state classification of the
    deterministic duration band relative to this run:

    * ``insufficient_history`` - the band is not usable (too few good-run
      samples or no numeric duration for this run). A null ``out_of_band``
      is NOT a clearance.
    * ``in_band`` - the band is usable and the run's duration is within it.
    * ``out_of_band`` - the band is usable and the run's duration is
      outside it (``out_of_band`` is ``fast`` or ``slow``).

    ``db_delta_tracked`` is passed through to the packet as-is; ``None``
    means the caller has no historical tracking signal for this job.

    ``db_delta_attributable`` is true only when the run had the scheduler
    to itself (``concurrent_run_ids`` on ``run_end`` is empty/absent). When
    false, ``db_delta`` is a database-wide counter diff that overlapped at
    least one other run, so every counter in ``db_delta_summary`` is
    labelled ``not_attributable`` and checkpoint_verdict.py's post-return
    guard drops verdict reasons citing a counter movement (issue #1734).
    """
    duration_s = run_end.get("duration_s")
    oob = out_of_band(
        duration_s,
        band,
        tolerance=tolerance,
        absolute_floor_s=absolute_floor_s,
    )
    if band.get("status") != "ok" or not isinstance(duration_s, (int, float)):
        band_assessment = "insufficient_history"
    elif oob is not None:
        band_assessment = "out_of_band"
    else:
        band_assessment = "in_band"
    raw_delta = run_end.get("db_delta")
    concurrent_run_ids = list(run_end.get("concurrent_run_ids") or [])
    db_delta_attributable = not concurrent_run_ids
    # Normalize the status: an unknown/invalid value degrades to
    # capture_unavailable so the packet never carries a status the verdicter
    # or downstream consumers do not understand.
    status = (
        log_excerpt_status
        if log_excerpt_status in LOG_EXCERPT_STATUS_VALUES
        else LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE
    )
    log_excerpt_is_job_scoped = status in _LOG_EXCERPT_STATUS_SCOPED
    return {
        "job": run_end.get("job"),
        "run_id": run_end.get("run_id"),
        "disposition": run_end.get("disposition"),
        "duration_s": duration_s,
        "db_delta": raw_delta,
        "db_delta_summary": _db_delta_summary(
            raw_delta, db_delta_tracked, attributable=db_delta_attributable
        ),
        "db_delta_tracked": db_delta_tracked,
        "db_delta_attributable": db_delta_attributable,
        "concurrent_run_ids": concurrent_run_ids,
        "result": run_end.get("result"),
        "error": run_end.get("error"),
        "signature_hits": hits,
        "shared_signature_hits": list(shared_signature_hits or []),
        "baseline": band,
        "out_of_band": oob,
        "band_assessment": band_assessment,
        "in_band": band_assessment == "in_band",
        "log_excerpt": (log_excerpt or "")[-_LOG_EXCERPT_CAP:],
        "concurrent_context": (concurrent_context or "")[-_LOG_EXCERPT_CAP:],
        "log_excerpt_status": status,
        "log_excerpt_is_job_scoped": log_excerpt_is_job_scoped,
    }

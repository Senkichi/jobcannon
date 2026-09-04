"""PORTED from job_finder/web/scoring_runner.py @ 5c6e4603623dd796b9074d2c694ba45dc85250a9
(private job-cannon). Ledger L-0263.

# PORT-SEAM: residence is host, not engine -- this module opens pooled
# connections and drives an in-process thread pool, and calls
# jobcannon.host.scoring_orchestrator.score_and_persist_job (a sibling host
# module, already landed via L-0259/#361). Lands as
# jobcannon/host/scoring_runner.py, scheduled by a new `score` procrastinate
# task in jobcannon/host/tasks.py (mirrors the existing `scan` task).

Scoring runner -- unified v3.0 scoring orchestration (Phase 34 Plan 4).

Single entry point: ``run_scoring`` calls ``score_and_persist_job`` per
dedup_key, preserving the pre-score liveness gate per CONTEXT D-11.

Parallel workers (issue #1036): when configured, scoring runs in a thread
pool with N workers, each with its own pooled DB connection.
# PORT-SEAM: private derived N from config + a local VRAM probe
# (job_finder.web.vram_query.derive_worker_count, rated DIES -- no local
# GPU to probe on a hosted worker). This port reads N from a single host
# concurrency env var (JC_SCORE_WORKERS, design note Q-B) instead, keeping
# the in-process thread pool: decomposing into per-job procrastinate tasks
# would restructure this module's whole body into unclassified hunks and
# forfeit the fidelity-diff this row is signed for (filed as FU-3, not
# applied here).

# PORT-SEAM: private's ``update_pipeline_status`` auto-dismiss (exclusion)
# and auto-archive (expired liveness) legs are DROPPED here as delete-hunks
# -- ``update_pipeline_status`` has no public target. jobcannon.db._persistence
# 's own module docstring (L-0073) already excludes it by name: writing it
# would add a second writer to `pipeline_status`, which
# jobcannon.db._user_actions declares itself the sole writer of. That table
# is also per-user (user_id, posting_id PK) with a closed {dismissed,
# applied} vocabulary -- structurally incompatible with private's global,
# system-driven, open-vocabulary (discovered/dismissed/archived/...) usage
# here. jobcannon.host.scan_tasks.run_expiry_check_task/run_stale_detect_task
# independently RESERVE the same functionality citing the identical root
# cause ("gates on per-user pipeline_status ... deferred until that
# [multi-tenant] redesign is ticketed") -- a third leg quietly writing
# anyway would be the inconsistency. This is the design note's own
# authorized alternative ("...or gate that leg", L-0263 seam #4), not a
# deviation from it. Filed as a modularity follow-up (PR body), not
# ported inline.

# PORT-SEAM: private's legitimacy-scan UPDATE (`UPDATE jobs SET
# legitimacy_note = ? WHERE dedup_key = ? AND legitimacy_note IS NULL`) is
# dropped here too -- `postings` has no `legitimacy_note` column yet
# (grep-confirmed through m0015; jobcannon.db._assessment_writer's own
# docstring names this exact omission and probes for the column's future
# arrival). No column exists to write into, so `scan_legitimacy`'s call
# site is a delete-hunk, same class as the already-accepted `set_postings`/
# `run_events.mark` drops in the sibling L-0259 PR (#361).

# PORT-SEAM: private's post-score "Count unresolved location policies per
# run" re-SELECT (`SELECT location_policy_verdict FROM jobs ...`) is
# dropped -- `_assessment_writer.py`'s own docstring states the location
# policy verdict JSON it consumes "is not itself persisted as a column" on
# this host, and `score_and_persist_job` does not return the LocationPolicy
# it computed internally. No public seam exists to source this counter from
# without re-implementing location-policy computation here (unclassified
# restructuring, not a mechanical seam) -- delete-hunk, flagged as a
# fidelity/behavior item in the PR body, not a silent drop.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from jobcannon.db._persistence import persist_job_expiry_state
from jobcannon.db.pool import connection_factory
from jobcannon.engine.exclusion_filter import should_exclude
from jobcannon.engine.expiry_checker import EXPIRED as _EXPIRED
from jobcannon.engine.expiry_checker import check_job_liveness
from jobcannon.engine.job_scorer import scoring_precheck
from jobcannon.engine.opaque_redirect_candidates import record_signal0_outcome
from jobcannon.engine.source_registry import first_source_url, is_unverifiable_candidate
from jobcannon.host.scoring_orchestrator import score_and_persist_job

logger = logging.getLogger(__name__)

# Liveness prefetch bound: how many jobs ahead to prefetch (issue #1038)
_LIVENESS_PREFETCH_BOUND = 4


def _worker_count() -> int:
    """Single host concurrency setting (design note Q-B), replacing private's
    VRAM-gated ``derive_worker_count(config)``. Unparseable/non-positive
    values fall back to 1 (serial) rather than raising -- a config typo must
    degrade the scoring path, never crash it."""
    raw = os.environ.get("JC_SCORE_WORKERS", "1")
    try:
        n = int(raw)
    except ValueError:
        return 1
    return n if n > 0 else 1


def _prefetch_liveness(
    dedup_keys: list[str],
    config: dict,
    prefetch_dict: OrderedDict[str, str],
    stop_event: threading.Event,
    prefetch_lock: threading.Lock,
    consumed_index: list[int],
) -> None:
    """Background thread that prefetches liveness checks ahead of the scoring loop.

    Walks k+1...k+4 ahead of the current scoring position, running
    check_job_liveness for each job and storing the result in a bounded
    OrderedDict. The main loop consumes these verdicts in order and performs
    persist_job_expiry_state on its own connection (per-item commits and
    archive-before-score semantics unchanged).

    Implements backpressure via a shared consumed_index counter: for indices
    past the initial window, the producer *waits* (polling stop_event) until
    the consumer has advanced enough that producing index i would not exceed
    _LIVENESS_PREFETCH_BOUND jobs of lead over the consumer. Every index is
    therefore either prefetched or the thread was stopped -- no index is
    ever silently skipped/abandoned (issue #1038).

    # PORT-SEAM: `db_path: str` parameter dropped -- host has no single
    # sqlite path; `standalone_connection(db_path)` -> `connection_factory()`
    # (pooled, zero positional args by ScanServices contract).
    """
    try:
        with connection_factory() as conn:
            raw = conn.raw if hasattr(conn, "raw") else conn
            for i, dedup_key in enumerate(dedup_keys):
                if stop_event.is_set():
                    break

                # Only prefetch ahead: skip the first few jobs that the main
                # loop is already processing or about to process
                if i < _LIVENESS_PREFETCH_BOUND:
                    continue

                # Backpressure: wait (do not skip/abandon) until the consumer
                # has advanced enough that producing index i keeps our lead
                # within _LIVENESS_PREFETCH_BOUND. Bail out only if asked to
                # stop.
                with prefetch_lock:
                    lead = i - consumed_index[0]
                while lead >= _LIVENESS_PREFETCH_BOUND:
                    if stop_event.is_set():
                        return
                    time.sleep(0.05)
                    with prefetch_lock:
                        lead = i - consumed_index[0]

                with prefetch_lock:
                    if dedup_key in prefetch_dict:
                        continue

                # PORT-SEAM: `jobs` -> `postings`, `SELECT {JOBS_ALL_COLUMNS}`
                # -> `SELECT *` (matches _jobs.py idiom; carries every column
                # including jd_adjudicated_version -- see run_scoring below).
                row = raw.execute(
                    "SELECT * FROM postings WHERE dedup_key = %s",
                    (dedup_key,),
                ).fetchone()
                if row is None:
                    continue

                job = dict(row)

                # Run liveness check (shadow opaque-redirect candidates skip
                # Signal-0 direct GET when conn is available). `conn` here is
                # the wrapped connection_factory() connection -- engine
                # functions issue their own qmark SQL, translated by
                # jobcannon.db.compat.
                liveness = check_job_liveness(job, config, conn=conn)

                with prefetch_lock:
                    prefetch_dict[dedup_key] = liveness
                    # Safety net only: the wait-based backpressure above
                    # already keeps prefetch_dict within
                    # _LIVENESS_PREFETCH_BOUND entries, so this eviction
                    # should never actually trigger.
                    if len(prefetch_dict) > _LIVENESS_PREFETCH_BOUND:
                        prefetch_dict.popitem(last=False)

                logger.debug("Prefetched liveness for '%s': %s", dedup_key, liveness)
    except Exception as e:
        logger.warning("Liveness prefetch thread error: %s", e)


def _increment_summary(summary: dict, key: str, lock: threading.Lock | None) -> None:
    """Increment a summary counter, optionally under a lock."""
    if lock:
        with lock:
            summary[key] += 1
    else:
        summary[key] += 1


def _process_one_job(
    dedup_key: str,
    config: dict,
    run_id: str | None,
    summary_lock: threading.Lock | None,
    summary: dict,
    prefetch_dict: OrderedDict[str, str],
    prefetch_lock: threading.Lock,
    timeout: float | None = None,
) -> None:
    """Process a single job through the scoring pipeline.

    This function runs the full per-item scoring pipeline
    (exclusion -> deferral -> liveness -> scoring) and updates the shared
    summary dict. It creates its own pooled connection for thread safety.

    Used by both serial and parallel scoring paths to ensure consistent
    behavior (issue #1036).

    Args:
        dedup_key: Job dedup_key to score.
        config: Application config dict.
        run_id: Run envelope correlation id (or None).
        summary_lock: Lock protecting the shared summary dict (None for
            serial path).
        summary: Shared summary dict (counters updated under lock if
            provided).
        prefetch_dict: OrderedDict of prefetched liveness results (issue
            #1038).
        prefetch_lock: Lock protecting prefetch_dict (all callers pass a
            real lock).
        timeout: Optional provider-call timeout override (seconds),
            forwarded to score_and_persist_job -> score_job -> call_model.
            Defaults to None (provider default).

    # PORT-SEAM: `db_path: str` parameter dropped -- see _prefetch_liveness.
    """
    with connection_factory() as conn:
        raw = conn.raw if hasattr(conn, "raw") else conn
        try:
            # PORT-SEAM: `jobs` -> `postings`, `SELECT {JOBS_ALL_COLUMNS}` ->
            # `SELECT *`. This is the REFUTER FLAG job-dict (#361 round-2):
            # a bare `SELECT *` carries every postings column, including
            # `jd_adjudicated_version` (m0009) and `jd_content_verdict`
            # (m0009) -- the two columns scoring_precheck's D5 gate reads --
            # so a stamped posting's job dict reaches score_and_persist_job
            # (and scoring_precheck) with jd_adjudicated_version populated,
            # never silently dropped by a narrower column list.
            row = raw.execute(
                "SELECT * FROM postings WHERE dedup_key = %s",
                (dedup_key,),
            ).fetchone()
            if row is None:
                logger.warning(
                    "run_scoring worker: job '%s' not found in DB -- skipping",
                    dedup_key,
                )
                return

            job = dict(row)

            # Exclusion filter. # PORT-SEAM: private's auto-dismiss
            # update_pipeline_status(...) call is dropped here (see module
            # docstring) -- excluded jobs are simply skipped, not persisted
            # as 'dismissed'. This matches the established read-time-filter
            # pattern already used elsewhere on this host (exclusion_filter.
            # count_scorable/is_scorable, ledger L-0179): exclusion gates
            # scoring, it does not write a status.
            profile = config.get("profile") or {}
            excluded, _rule_tag, _detailed_text = should_exclude(
                job,
                profile.get("exclusions") or {},
                min_salary=profile.get("min_salary"),
                config=config,
            )
            if excluded:
                _increment_summary(summary, "skipped_no_jd", summary_lock)
                return

            # Unverifiable-aggregator deferral gate
            if is_unverifiable_candidate(job, config):
                _increment_summary(summary, "deferred", summary_lock)
                return

            # Scoring precheck (completeness gates, incl. D5 jd-adjudication)
            precheck_reason = scoring_precheck(job)
            if precheck_reason is not None:
                _increment_summary(summary, "skipped_no_jd", summary_lock)
                return

            # Liveness gate (D-11)
            # PORT-SEAM: private's sqlite3 `expiry_checked_at` is a naive-UTC
            # TEXT column, always a str at this point. Postgres stores
            # `timestamptz` (m0016) and psycopg auto-decodes it to a
            # timezone-aware `datetime`, so this branch accepts both shapes
            # -- a str is parsed exactly as private did; a datetime is used
            # as-is (the common host-path case).
            ttl_hours = (config.get("scoring") or {}).get("liveness_recheck_hours", 24)
            expiry_checked_at = job.get("expiry_checked_at")
            should_check_liveness = True
            if expiry_checked_at:
                try:
                    checked_dt = expiry_checked_at
                    if isinstance(checked_dt, str):
                        checked_dt = datetime.fromisoformat(checked_dt.replace("Z", "+00:00"))
                    if checked_dt.tzinfo is None:
                        checked_dt = checked_dt.replace(tzinfo=UTC)
                    ttl_threshold = datetime.now(UTC) - timedelta(hours=ttl_hours)
                    if checked_dt > ttl_threshold:
                        should_check_liveness = False
                except (ValueError, TypeError, AttributeError):
                    should_check_liveness = True

            if should_check_liveness:
                # Use prefetched liveness if available (issue #1038)
                liveness = None
                if prefetch_dict:
                    with prefetch_lock:
                        liveness = prefetch_dict.pop(dedup_key, None)

                if liveness is not None:
                    logger.debug("Using prefetched liveness for '%s': %s", dedup_key, liveness)
                else:
                    liveness = check_job_liveness(job, config, conn=conn)

                # Record the Signal-0 outcome for auto-derivation of
                # opaque-redirect candidates. Runs on every liveness attempt
                # (not just INCONCLUSIVE) so the blocked ratio is accurate.
                first_url = first_source_url(job)
                attempted = getattr(liveness, "attempted", True)
                blocked = getattr(liveness, "blocked", False)
                record_signal0_outcome(conn, first_url, attempted, blocked, config)

                now_iso = datetime.now(UTC).isoformat()
                persist_job_expiry_state(conn, dedup_key, liveness, now_iso)
                if liveness == _EXPIRED:
                    logger.info(
                        "run_scoring worker: expired liveness for '%s' @ '%s'",
                        job.get("title"),
                        job.get("company"),
                    )
                    # PORT-SEAM: private's auto-archive update_pipeline_status(
                    # ..., "archived", ...) call is dropped here (see module
                    # docstring) -- persist_job_expiry_state above already
                    # wrote expiry_status='expired', which is the shared-
                    # corpus signal downstream freshness/ranking consumers
                    # (jobcannon.host.structural_axes.freshness) already key
                    # on to suppress dead postings.
                    _increment_summary(summary, "skipped_dead", summary_lock)
                    return

            result = score_and_persist_job(job, conn, config, run_id=run_id, timeout=timeout)

            if result is None:
                _increment_summary(summary, "skipped_no_jd", summary_lock)
                return

            status = getattr(result, "status", None)
            if status == "skipped":
                _increment_summary(summary, "skipped_no_jd", summary_lock)
                return
            if status == "error":
                _increment_summary(summary, "errors", summary_lock)
                return

            _increment_summary(summary, "scored", summary_lock)

            # Re-read classification for the per-class counter.
            # PORT-SEAM: `jobs` -> `postings`, `?` -> `%s`.
            cls_row = raw.execute(
                "SELECT classification FROM postings WHERE dedup_key = %s",
                (dedup_key,),
            ).fetchone()
            if cls_row and cls_row["classification"]:
                key = f"classified_{cls_row['classification']}"
                if key in summary:
                    _increment_summary(summary, key, summary_lock)

        except TypeError as e:
            # A TypeError here is much more likely to be a scorer_fn contract
            # mismatch than a transient per-job failure -- log it loudly with
            # a traceback so a signature mismatch can never again be
            # indistinguishable from a routine skip (issue #1214 follow-up).
            logger.error(
                "run_scoring worker TypeError for job '%s' (possible scorer_fn "
                "signature mismatch): %s",
                dedup_key,
                e,
                exc_info=True,
            )
            _increment_summary(summary, "errors", summary_lock)
        except Exception as e:
            logger.warning(
                "run_scoring worker error for job '%s': %s",
                dedup_key,
                e,
            )
            _increment_summary(summary, "errors", summary_lock)


def run_scoring(
    new_job_keys: list[str],
    config: dict,
    *,
    run_id: str | None = None,
    timeout: float | None = None,
) -> dict:
    """Unified v3.0 scoring runner.

    For each dedup_key in ``new_job_keys``:

    1. Fetch the postings row (skip silently if missing).
    2. Unverifiable-aggregator deferral gate -- defer jobs whose provenance
       is entirely opaque-redirect-aggregator sources with no corroborated
       direct_url.
    3. Pre-score liveness gate (CONTEXT D-11). Dead jobs are counted as
       skipped and never hit the scorer.
    4. Delegate scoring + persistence to ``score_and_persist_job``.

    Parallel workers (issue #1036): when JC_SCORE_WORKERS > 1 (design note
    Q-B), scoring runs in a thread pool with N workers. The per-item
    pipeline (exclusion -> deferral -> liveness -> scoring) is unchanged;
    workers > pool slots is safe (the pool queues checkouts).

    Returns a summary dict with counters for scored / skipped / error cases
    and per-classification breakdown.

    ``run_id``: optional run-envelope correlation id, forwarded to
    ``score_and_persist_job`` for call-signature compatibility; currently
    unused downstream (see jobcannon.host.scoring_orchestrator's own
    docstring -- the private per-job run_events.mark(...) emission this
    threaded into has no public target, design note Q-D).

    ``timeout``: optional provider-call timeout override (seconds),
    forwarded to every ``_process_one_job`` call and on to
    ``score_and_persist_job`` -> the scorer. Defaults to None (provider
    default).

    # PORT-SEAM: `db_path: str` parameter dropped from the signature -- host
    # has no single sqlite path (see _prefetch_liveness/_process_one_job).
    """
    summary = {
        "scored": 0,
        "classified_apply": 0,
        "classified_consider": 0,
        "classified_skip": 0,
        "classified_reject": 0,
        "skipped_dead": 0,
        "skipped_no_jd": 0,
        "deferred": 0,
        "errors": 0,
    }

    if not new_job_keys:
        return summary

    # PORT-SEAM: `derive_worker_count(config)` (VRAM/nvidia-smi probing, from
    # vram_query.py, rated DIES) -> `_worker_count()` (single host
    # concurrency env var, design note Q-B). Keeps the in-process thread
    # pool for the port (FU-3 files the procrastinate-native redesign).
    num_workers = _worker_count()

    # Serial path (default behavior)
    if num_workers == 1:
        logger.debug("run_scoring: using serial execution (1 worker)")

        prefetch_dict: OrderedDict[str, str] = OrderedDict()
        prefetch_lock = threading.Lock()
        consumed_index = [0]
        stop_event = threading.Event()
        prefetch_thread = None
        if len(new_job_keys) > _LIVENESS_PREFETCH_BOUND:
            prefetch_thread = threading.Thread(
                target=_prefetch_liveness,
                args=(
                    new_job_keys,
                    config,
                    prefetch_dict,
                    stop_event,
                    prefetch_lock,
                    consumed_index,
                ),
                daemon=True,
            )
            prefetch_thread.start()

        try:
            for i, dedup_key in enumerate(new_job_keys):
                _process_one_job(
                    dedup_key,
                    config,
                    run_id,
                    None,
                    summary,
                    prefetch_dict,
                    prefetch_lock,
                    timeout=timeout,
                )
                with prefetch_lock:
                    consumed_index[0] = i
        finally:
            stop_event.set()
            if prefetch_thread:
                prefetch_thread.join(timeout=5.0)

        logger.info(
            "run_scoring: %d scored, %d dead, %d no-jd, %d deferred, %d errors",
            summary["scored"],
            summary["skipped_dead"],
            summary["skipped_no_jd"],
            summary["deferred"],
            summary["errors"],
        )
        return summary

    # Parallel path (issue #1036): worker pool with queue.Queue
    logger.info("run_scoring: using %d parallel workers", num_workers)

    work_queue: queue.Queue[str] = queue.Queue()
    for key in new_job_keys:
        work_queue.put(key)

    summary_lock = threading.Lock()

    prefetch_dict = OrderedDict()
    prefetch_lock = threading.Lock()
    consumed_index = [0]
    stop_event = threading.Event()
    prefetch_thread = None
    if len(new_job_keys) > _LIVENESS_PREFETCH_BOUND:
        prefetch_thread = threading.Thread(
            target=_prefetch_liveness,
            args=(
                new_job_keys,
                config,
                prefetch_dict,
                stop_event,
                prefetch_lock,
                consumed_index,
            ),
            daemon=True,
        )
        prefetch_thread.start()

    def worker_thread() -> None:
        """Worker thread: pull keys from queue and score each."""
        while True:
            try:
                dedup_key = work_queue.get_nowait()
            except queue.Empty:
                break
            try:
                _process_one_job(
                    dedup_key,
                    config,
                    run_id,
                    summary_lock,
                    summary,
                    prefetch_dict,
                    prefetch_lock,
                    timeout=timeout,
                )
            except Exception as e:
                logger.warning(
                    "run_scoring worker thread error for job '%s': %s",
                    dedup_key,
                    e,
                )
                with summary_lock:
                    summary["errors"] += 1
            finally:
                with prefetch_lock:
                    consumed_index[0] += 1
                work_queue.task_done()

    workers = []
    for _ in range(num_workers):
        t = threading.Thread(target=worker_thread, daemon=True)
        t.start()
        workers.append(t)

    work_queue.join()
    for t in workers:
        t.join()

    stop_event.set()
    if prefetch_thread:
        prefetch_thread.join(timeout=5.0)

    logger.info(
        "run_scoring: %d scored, %d dead, %d no-jd, %d deferred, %d errors",
        summary["scored"],
        summary["skipped_dead"],
        summary["skipped_no_jd"],
        summary["deferred"],
        summary["errors"],
    )
    return summary

"""PORTED from job_finder/web/scoring_runner.py @ 5c6e4603623dd796b9074d2c694ba45dc85250a9
(private job-cannon). Ledger L-0263.

# PORT-SEAM: residence is host, not engine -- this module opens pooled
# connections and drives an in-process thread pool, and calls
# jobcannon.host.scoring_orchestrator.score_and_persist_job (a sibling host
# module, already landed via L-0259/#361). Lands as
# jobcannon/host/scoring_runner.py, scheduled by a new `score` procrastinate
# task in jobcannon/host/tasks.py (mirrors the existing `scan` task). Every
# other seam edit in this file (dropped legs, SQL-dialect rewrites, the
# db_path -> connection_factory swap, the worker-count seam) is marked
# inline at its exact site below, not just here.

Scoring runner -- unified v3.0 scoring orchestration (Phase 34 Plan 4).

Single entry point: ``run_scoring`` calls ``score_and_persist_job`` per
dedup_key, preserving the pre-score liveness gate per CONTEXT D-11.
The legacy two-tier (Haiku + Sonnet) entry points were removed in
Plan 4 Commit E.

Parallel workers (issue #1036): when configured and VRAM permits, scoring
runs in a thread pool with N workers, each with its own DB connection.
"""

from __future__ import annotations

import logging
import os  # PORT-SEAM: for _worker_count()'s env var read below.
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

# PORT-SEAM: private's `from job_finder.web.legitimacy_scanner import
# scan_legitimacy` has no target -- the legitimacy-scan leg is dropped
# (see _process_one_job's body).
# PORT-SEAM: private's `from job_finder.web.location_policy import
# is_unresolved_location_policy` has no target -- the location-policy
# counter leg is dropped (see _process_one_job's body).

from jobcannon.engine.opaque_redirect_candidates import record_signal0_outcome

# PORT-SEAM: private's local `from job_finder.web.scoring_orchestrator
# import score_and_persist_job` becomes `from jobcannon.host.
# scoring_orchestrator import score_and_persist_job` below (host, not
# engine -- L-0259/#361 landed it there).

from jobcannon.engine.source_registry import first_source_url, is_unverifiable_candidate
from jobcannon.host.scoring_orchestrator import score_and_persist_job

# PORT-SEAM: private's `try: from job_finder.web.data_enricher import
# enrich_job / except ImportError: enrich_job = None` is dropped -- dead
# import in private too (grep-confirmed: enrich_job has no call site in
# this file's body; only named in a docstring cross-reference to a
# different caller, the onboarding wizard's eager first-score loop).

logger = logging.getLogger(__name__)

# Liveness prefetch bound: how many jobs ahead to prefetch (issue #1038)
_LIVENESS_PREFETCH_BOUND = 4


# PORT-SEAM: single host concurrency setting (design note Q-B), replacing
# private's VRAM-gated `derive_worker_count(config)` import from
# job_finder.web.vram_query (rated DIES -- no local GPU to probe on a
# hosted worker). Unparseable/non-positive values fall back to 1 (serial)
# rather than raising -- a config typo must degrade the scoring path, never
# crash it. Keeps the in-process thread pool for this port; decomposing
# into per-job procrastinate tasks is filed as FU-3, not applied here.
def _worker_count() -> int:
    raw = os.environ.get("JC_SCORE_WORKERS", "1")
    try:
        n = int(raw)
    except ValueError:
        return 1
    return n if n > 0 else 1


def _prefetch_liveness(
    dedup_keys: list[str],
    # PORT-SEAM: `db_path: str` param dropped -- host has no single sqlite
    # path; `standalone_connection(db_path)` -> `connection_factory()`
    # (pooled, zero positional args by ScanServices contract).
    config: dict,
    prefetch_dict: OrderedDict[str, str],
    stop_event: threading.Event,
    prefetch_lock: threading.Lock,
    consumed_index: list[int],
) -> None:
    """Background thread that prefetches liveness checks ahead of the scoring loop.

    Walks k+1...k+4 ahead of the current scoring position, running check_job_liveness
    for each job and storing the result in a bounded OrderedDict. The main loop
    consumes these verdicts in order and performs persist_job_expiry_state on its
    own connection (per-item commits and archive-before-score semantics unchanged).

    Implements backpressure via a shared consumed_index counter: for indices past
    the initial window, the producer *waits* (polling stop_event) until the
    consumer has advanced enough that producing index i would not exceed
    _LIVENESS_PREFETCH_BOUND jobs of lead over the consumer. Every index is
    therefore either prefetched or the thread was stopped -- no index is ever
    silently skipped/abandoned (issue #1038).

    Args:
        dedup_keys: List of job dedup_keys to score (in order).
        # PORT-SEAM: `db_path` arg dropped from Args -- see signature above.
        config: Application config dict.
        prefetch_dict: Bounded OrderedDict to store prefetch results {dedup_key: liveness}.
        stop_event: Threading event to signal the prefetch thread to stop.
        prefetch_lock: Lock protecting prefetch_dict and consumed_index.
        consumed_index: Shared [int] counter tracking consumer progress.
    """
    try:
        # PORT-SEAM: `standalone_connection(db_path)` -> `connection_factory()`
        # (pooled); `raw` gives the bare psycopg connection for host-authored
        # `%s`/`postings` SQL (engine-authored qmark SQL would instead route
        # through `conn.execute()`'s jobcannon.db.compat translation).
        with connection_factory() as conn:
            raw = conn.raw if hasattr(conn, "raw") else conn
            for i, dedup_key in enumerate(dedup_keys):
                if stop_event.is_set():
                    break

                # Only prefetch ahead: skip the first few jobs that the main loop
                # is already processing or about to process
                if i < _LIVENESS_PREFETCH_BOUND:
                    continue

                # Backpressure: wait (do not skip/abandon) until the consumer has
                # advanced enough that producing index i keeps our lead within
                # _LIVENESS_PREFETCH_BOUND. Bail out only if asked to stop.
                with prefetch_lock:
                    lead = i - consumed_index[0]
                while lead >= _LIVENESS_PREFETCH_BOUND:
                    if stop_event.is_set():
                        return
                    time.sleep(0.05)
                    with prefetch_lock:
                        lead = i - consumed_index[0]

                # Check if we've already prefetched this key (under lock)
                with prefetch_lock:
                    if dedup_key in prefetch_dict:
                        continue

                # Fetch the job row for liveness check
                # PORT-SEAM: `jobs` -> `postings`, `SELECT {JOBS_ALL_COLUMNS}`
                # -> `SELECT *` (carries every column, incl.
                # jd_adjudicated_version -- see run_scoring's REFUTER-FLAG
                # note below), `conn.execute` -> `raw.execute`, `?` -> `%s`.
                row = raw.execute(
                    "SELECT * FROM postings WHERE dedup_key = %s",
                    (dedup_key,),
                ).fetchone()
                if row is None:
                    continue

                job = dict(row)

                # Run liveness check (shadow opaque-redirect candidates skip
                # PORT-SEAM: comment now says "conn"; private said
                # "db_path/conn" -- db_path is gone (see signature above).
                # Signal-0 direct GET when conn is available).
                liveness = check_job_liveness(job, config, conn=conn)

                # Store result under lock
                with prefetch_lock:
                    prefetch_dict[dedup_key] = liveness
                    # Safety net only: the wait-based backpressure above already
                    # keeps prefetch_dict within _LIVENESS_PREFETCH_BOUND entries,
                    # so this eviction should never actually trigger.
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
    # PORT-SEAM: `db_path: str` param dropped -- see _prefetch_liveness.
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
    # PORT-SEAM: "legitimacy" dropped from pipeline description (leg
    # dropped further down this function's body).
    (exclusion → deferral → liveness → scoring) and
    updates the shared summary dict. It creates its own
    # PORT-SEAM: "standalone_connection" -> "connection_factory()" (pooled).
    connection_factory() connection for thread safety.

    Used by both serial and parallel scoring paths to ensure
    consistent behavior (issue #1036).

    Args:
        dedup_key: Job dedup_key to score.
        # PORT-SEAM: `db_path` arg dropped from Args -- see signature above.
        config: Application config dict.
        run_id: Run envelope correlation id (or None).
        summary_lock: Lock protecting the shared summary dict (None for serial path).
        summary: Shared summary dict (counters updated under lock if provided).
        prefetch_dict: OrderedDict of prefetched liveness results (issue #1038).
        prefetch_lock: Lock protecting prefetch_dict (all callers pass a real lock).
        timeout: Optional provider-call timeout override (seconds), forwarded
            to score_and_persist_job -> score_job -> call_model. Defaults to
            None (provider default). Budgeted callers (the onboarding wizard's
            eager first-score loop) pass the remaining wall-clock budget.
    """
    # PORT-SEAM: `standalone_connection(db_path)` -> `connection_factory()`
    # (pooled); `raw` gives the bare psycopg connection for host-authored
    # `%s`/`postings` SQL below.
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

            # Exclusion-filter auto-dismiss
            profile = config.get("profile") or {}
            excluded, rule_tag, detailed_text = should_exclude(
                job,
                profile.get("exclusions") or {},
                min_salary=profile.get("min_salary"),
                config=config,
            )
            if excluded:
                # PORT-SEAM: private's auto-dismiss update_pipeline_status(...)
                # call (using rule_tag/detailed_text as the evidence string)
                # is dropped here -- update_pipeline_status has no public
                # target. jobcannon.db._persistence excludes it by name;
                # jobcannon.db._user_actions is the sole writer of
                # pipeline_status (per-user, closed {dismissed, applied}
                # vocabulary) -- structurally incompatible with private's
                # global, open-vocabulary usage here. This is the design
                # note's own authorized alternative ("...or gate that leg",
                # L-0263 seam #4): excluded jobs are simply skipped, not
                # persisted as 'dismissed'.
                _increment_summary(summary, "skipped_no_jd", summary_lock)
                return

            # Unverifiable-aggregator deferral gate
            if is_unverifiable_candidate(job, config):
                _increment_summary(summary, "deferred", summary_lock)
                return

            # Scoring precheck (completeness gates)
            # PORT-SEAM: gates now include D5 jd-adjudication (jobcannon.
            # engine.job_scorer.scoring_precheck) -- the SELECT * above
            # carries jd_adjudicated_version so a stamped posting is never
            # spuriously gated (see REFUTER FLAG note above).
            precheck_reason = scoring_precheck(job)
            if precheck_reason is not None:
                _increment_summary(summary, "skipped_no_jd", summary_lock)
                return

            # Liveness gate (D-11)
            ttl_hours = (config.get("scoring") or {}).get("liveness_recheck_hours", 24)
            expiry_checked_at = job.get("expiry_checked_at")
            should_check_liveness = True
            if expiry_checked_at:
                try:
                    # PORT-SEAM: private's sqlite3 `expiry_checked_at` is a
                    # naive-UTC TEXT column, always a str here. Postgres
                    # stores `timestamptz` (m0016) and psycopg auto-decodes
                    # it to a timezone-aware `datetime`, so this branch
                    # accepts both shapes -- a str is parsed exactly as
                    # private did; a datetime is used as-is (the common
                    # host-path case). `except` widened with AttributeError
                    # for the same reason (a bare int/None would otherwise
                    # raise unguarded on `.replace`/`.tzinfo`).
                    checked_dt = expiry_checked_at
                    if isinstance(checked_dt, str):
                        checked_dt = datetime.fromisoformat(checked_dt.replace("Z", "+00:00"))
                    if checked_dt.tzinfo is None:
                        checked_dt = checked_dt.replace(tzinfo=UTC)
                    ttl_threshold = datetime.now(UTC) - timedelta(hours=ttl_hours)
                    if checked_dt > ttl_threshold:
                        should_check_liveness = False
                # PORT-SEAM: AttributeError added -- timestamptz -> datetime shape.
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

                # Record the Signal-0 outcome for auto-derivation of opaque-redirect
                # candidates. It runs on every liveness attempt (not just INCONCLUSIVE)
                # so the blocked ratio is accurate. Shadow-flagged hosts are skipped in
                # check_job_liveness, so their attempted flag is False and nothing is tallied.
                first_url = first_source_url(job)
                attempted = getattr(liveness, "attempted", True)
                blocked = getattr(liveness, "blocked", False)
                record_signal0_outcome(conn, first_url, attempted, blocked, config)

                # PORT-SEAM: `utc_now_iso()` (job_finder.json_utils) has no
                # public target -- inlined as `datetime.now(UTC).isoformat()`.
                now_iso = datetime.now(UTC).isoformat()
                persist_job_expiry_state(conn, dedup_key, liveness, now_iso)
                if liveness == _EXPIRED:
                    logger.info(
                        "run_scoring worker: archiving expired '%s' @ '%s'",
                        job.get("title"),
                        job.get("company"),
                    )
                    # PORT-SEAM: private's auto-archive update_pipeline_status(
                    # ..., "archived", ...) call is dropped here -- same
                    # no-public-target rationale as the auto-dismiss drop
                    # above. persist_job_expiry_state (just above) already
                    # wrote expiry_status='expired', which is the shared-
                    # corpus signal downstream freshness/ranking consumers
                    # key on to suppress dead postings.
                    _increment_summary(summary, "skipped_dead", summary_lock)
                    return

            # PORT-SEAM: legitimacy scan dropped -- `postings` has no
            # `legitimacy_note` column (grep-confirmed through m0015;
            # jobcannon.db._assessment_writer's own docstring names this
            # exact omission). No column exists to write `scan_legitimacy`'s
            # result into, so the whole leg (jd_text check, scan_legitimacy
            # call, UPDATE jobs SET legitimacy_note ...) is a delete-hunk,
            # same class as the already-accepted `set_postings`/
            # `run_events.mark` drops in the sibling L-0259 PR (#361).

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

            # PORT-SEAM: "Count unresolved location policies per run" (#1576)
            # re-SELECT is dropped -- jobcannon.db._assessment_writer's own
            # docstring states the location-policy verdict JSON it consumes
            # "is not itself persisted as a column" on this host, and
            # score_and_persist_job does not return the LocationPolicy it
            # computed internally. No public seam exists to source this
            # counter from without re-implementing location-policy
            # computation here -- delete-hunk, flagged in the PR body.

            # Re-read classification for the per-class counter
            # PORT-SEAM: `jobs` -> `postings`, `?` -> `%s`, `conn.execute` ->
            # `raw.execute`. `cls_row[0]` is unchanged -- jobcannon.db.rows.
            # hybrid_row supports positional access same as private's
            # sqlite3.Row.
            cls_row = raw.execute(
                "SELECT classification FROM postings WHERE dedup_key = %s",
                (dedup_key,),
            ).fetchone()
            if cls_row and cls_row[0]:
                key = f"classified_{cls_row[0]}"
                if key in summary:
                    _increment_summary(summary, key, summary_lock)

        except TypeError as e:
            # A TypeError here is much more likely to be a scorer_fn contract
            # mismatch (e.g. a caller-supplied scorer/test-double missing a
            # required parameter such as location_policy) than a transient
            # per-job failure. Logging it at the same WARNING level as every
            # other per-job error let exactly this kind of regression hide
            # behind an innocuous scored=0 (issue #1214 follow-up) -- log it
            # loudly with a traceback so a signature mismatch can never again
            # be indistinguishable from a routine skip.
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
    # PORT-SEAM: `db_path: str` param dropped from the signature -- host has
    # no single sqlite path (see _prefetch_liveness/_process_one_job).
    *,
    run_id: str | None = None,
    timeout: float | None = None,
) -> dict:
    """Unified v3.0 scoring runner -- replaced run_haiku_scoring +
    run_sonnet_evaluation (Plan 4 Commit E removed the legacy two-tier path).

    For each dedup_key in ``new_job_keys``:

    1. Fetch the jobs row (skip silently if missing).
    2. Unverifiable-aggregator deferral gate (Section 5) — defer jobs whose
       provenance is entirely opaque-redirect-aggregator sources with no
       corroborated direct_url. These jobs are not scored, not archived, and
       not written to the DB until verification resolves direct_url or
       archival policy removes them.
    3. Pre-score liveness gate (CONTEXT D-11) — matches the position used by
       the legacy ``run_sonnet_evaluation``. Dead jobs are counted as skipped
       and never hit the scorer.
    4. Delegate scoring + persistence to ``score_and_persist_job``, which
       performs the atomic dual-write of new columns AND legacy shim
       (CONTEXT D-16).

    Parallel workers (issue #1036): when configured and VRAM permits, scoring
    runs in a thread pool with N workers, each with its own DB connection.
    # PORT-SEAM: "legitimacy" dropped from pipeline description (leg
    # dropped in _process_one_job's body).
    The per-item pipeline (exclusion → deferral → liveness → scoring)
    is unchanged; workers > server slots is safe (server queues).

    Returns a summary dict with counters for scored / skipped / error cases
    and per-classification breakdown. Counter keys match the new pipeline
    summary shape introduced in Plan 2 commit A (Plan 3 Commit E collapses
    the legacy haiku_scored / sonnet_queued / sonnet_evaluated keys).

    ``run_id`` (issue #215): the run-envelope correlation id from the
    scheduler / harness wrapper. Threaded into ``score_and_persist_job`` so
    each per-job ``score`` event on the ``run_events`` stream carries the
    same id as the run's ``run_start`` / ``run_end`` envelope. Callers that
    don't have a run envelope (e.g. tests, ad-hoc scripts) leave it ``None``
    and the orchestrator emits the ``"scoring:adhoc"`` sentinel.

    ``timeout``: optional provider-call timeout override (seconds), forwarded
    to every ``_process_one_job`` call (both the serial and parallel-worker
    paths) and on to ``score_and_persist_job`` -> the scorer. Defaults to
    None (provider default, e.g. ollama_provider._DEFAULT_TIMEOUT of 300s).
    Budgeted callers (the onboarding wizard's eager first-score loop, issue
    #1413's scoring-leg gap) pass the remaining wall-clock budget so a single
    scoring call cannot alone exceed it -- mirrors how the same wizard loop
    already threads a timeout into ``enrich_job`` for the enrichment leg.
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
        # PORT-SEAM: `location_policy_unresolved` key dropped -- the counter
        # that fed it is a delete-hunk in _process_one_job (see above).
    }

    if not new_job_keys:
        return summary

    # PORT-SEAM: `derive_worker_count(config)` (VRAM/nvidia-smi probing, from
    # vram_query.py, rated DIES) -> `_worker_count()` (single host
    # concurrency env var, design note Q-B).
    num_workers = _worker_count()

    # Serial path (default behavior, or when VRAM gating fails)
    if num_workers == 1:
        logger.debug("run_scoring: using serial execution (1 worker)")

        # Liveness prefetch thread (issue #1038) - only spawn if enough jobs to benefit
        prefetch_dict: OrderedDict[str, str] = OrderedDict()
        prefetch_lock = threading.Lock()  # Protect shared OrderedDict from race
        consumed_index = [0]  # Shared [int] for backpressure (list for mutability)
        stop_event = threading.Event()
        prefetch_thread = None
        if len(new_job_keys) > _LIVENESS_PREFETCH_BOUND:
            prefetch_thread = threading.Thread(
                target=_prefetch_liveness,
                args=(
                    new_job_keys,
                    # PORT-SEAM: `db_path,` arg dropped -- see
                    # _prefetch_liveness's signature.
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
                    # PORT-SEAM: `db_path,` arg dropped -- see
                    # _process_one_job's signature.
                    config,
                    run_id,
                    None,
                    summary,
                    prefetch_dict,
                    prefetch_lock,
                    timeout=timeout,
                )
                # Update consumed index for backpressure
                with prefetch_lock:
                    consumed_index[0] = i
        finally:
            stop_event.set()
            if prefetch_thread:
                prefetch_thread.join(timeout=5.0)

        # PORT-SEAM: unresolved/share location-policy computation dropped --
        # the counter it read no longer exists (see summary dict above).
        logger.info(
            # PORT-SEAM: trailing "%d unresolved location policies (%.1f%%)"
            # clause dropped; `scored,` -> `summary["scored"]` (local var
            # removed above).
            "run_scoring: %d scored, %d dead, %d no-jd, %d deferred, %d errors",
            summary["scored"],
            summary["skipped_dead"],
            summary["skipped_no_jd"],
            summary["deferred"],
            summary["errors"],
            # PORT-SEAM: trailing `unresolved,` / `share,` args dropped along
            # with the format-string clause above.
        )
        return summary

    # Parallel path (issue #1036): worker pool with queue.Queue
    logger.info("run_scoring: using %d parallel workers", num_workers)

    # Create work queue and populate with dedup_keys
    work_queue: queue.Queue[str] = queue.Queue()
    for key in new_job_keys:
        work_queue.put(key)

    # Lock for protecting shared summary dict
    summary_lock = threading.Lock()

    # Liveness prefetch thread (issue #1038) - shared across workers, only spawn if enough jobs
    # Use same OrderedDict pattern as serial path for consistency.
    # Workers consume out of order, so consumed_index here tracks total completed
    # count (not a strictly-ordered position) -- that is sufficient for the lead
    # bound semantics: it only needs to be a monotonically non-decreasing lower
    # bound on how many jobs have been consumed.
    prefetch_dict: OrderedDict[str, str] = OrderedDict()
    prefetch_lock = threading.Lock()
    consumed_index = [0]  # Shared [int] for backpressure (list for mutability)
    stop_event = threading.Event()
    prefetch_thread = None
    if len(new_job_keys) > _LIVENESS_PREFETCH_BOUND:
        prefetch_thread = threading.Thread(
            target=_prefetch_liveness,
            args=(
                new_job_keys,
                # PORT-SEAM: `db_path,` arg dropped -- see
                # _prefetch_liveness's signature.
                config,
                prefetch_dict,
                stop_event,
                prefetch_lock,
                consumed_index,
            ),
            daemon=True,
        )
        prefetch_thread.start()

    # Worker threads
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
                    # PORT-SEAM: `db_path,` arg dropped -- see
                    # _process_one_job's signature.
                    config,
                    run_id,
                    summary_lock,
                    summary,
                    prefetch_dict,
                    prefetch_lock,
                    timeout=timeout,
                )
            except Exception as e:
                # Catch connection setup exceptions and count them in summary errors
                logger.warning(
                    "run_scoring worker thread error for job '%s': %s",
                    dedup_key,
                    e,
                )
                with summary_lock:
                    summary["errors"] += 1
            finally:
                # Update consumed index for prefetch backpressure (total completed
                # count -- workers consume out of order, so this is a count, not
                # a strictly-ordered position).
                with prefetch_lock:
                    consumed_index[0] += 1
                work_queue.task_done()

    # Spawn workers
    workers = []
    for _ in range(num_workers):
        t = threading.Thread(target=worker_thread, daemon=True)
        t.start()
        workers.append(t)

    # Wait for all work to complete
    work_queue.join()
    for t in workers:
        t.join()

    # Stop prefetch thread
    stop_event.set()
    if prefetch_thread:
        prefetch_thread.join(timeout=5.0)

    # PORT-SEAM: unresolved/share location-policy computation dropped -- see
    # the serial path above (same summary-dict key removal).
    logger.info(
        # PORT-SEAM: trailing "%d unresolved location policies (%.1f%%)"
        # clause dropped; `scored,` -> `summary["scored"]` (local var
        # removed above).
        "run_scoring: %d scored, %d dead, %d no-jd, %d deferred, %d errors",
        summary["scored"],
        summary["skipped_dead"],
        summary["skipped_no_jd"],
        summary["deferred"],
        summary["errors"],
        # PORT-SEAM: trailing `unresolved,` / `share,` args dropped along
        # with the format-string clause above.
    )
    return summary

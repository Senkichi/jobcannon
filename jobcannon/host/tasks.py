"""Procrastinate task-shape declarations for the hosted job taxonomy, plus the
periodic enqueue tick, storage-check tick, orphaned-job reclaim tick,
anon-user reap tick, events-retention reap tick, deletion-reconciliation
sweep tick, revoked-subjects reap tick, jd-adjudication tick, and
nightly-monitor sampler tick that fire on a schedule.

Task shapes (`scan`, `expiry_check`, `stale_detect`) and the nine periodics
(`enqueue_due_scans`, `db_storage_check`, `reclaim_orphaned_jobs`,
`reap_anon_users`, `reap_old_events`, `reconcile_deleted_users`,
`reap_revoked_subjects`, `jd_adjudication`, `nightly_sampler`, each declared with
`@app.periodic` + `@app.task` below) all live here — this ONE
`procrastinate.App` instance (constructed below) is the sole periodic-task
scheduling mechanism in this codebase; a new periodic task is another peer
registered on it, never a second scheduler. An eighth periodic,
`enqueue_imap_ingest` (L-0188), lives in the sibling
`jobcannon.host.ingestion_tasks` module rather than here — same `app`
instance, imported separately by `jobcannon/worker/__main__.py` purely for
its `@app.task`/`@app.periodic` decorators to register (that module's own
docstring explains why it is a separate file: `ingestion_tasks.py` pulls in
`jobcannon.engine.parsed_job`/`ats_detection`, a different dependency
surface than this module's). 'enrich' is intentionally not defined: no enrich hook exists
to run. Defining a periodic task's SHAPE is not the same as RUNNING it: this
module still never runs a worker or applies procrastinate's schema at import
time — `jobcannon.worker.__main__` owns both (it applies procrastinate's
schema via `_ensure_procrastinate_schema`, then calls `App.run_worker()`),
and `App.run_worker()` is what actually fires every periodic tick and
deferred task on schedule.

Connector note: PsycopgConnector (procrastinate 3.9.0, the pinned version —
see pyproject.toml) is the async psycopg3 connector, and it is REQUIRED here
because `jobcannon.worker.__main__` calls App.run_worker(), which opens the
connector async (asyncio.run(... self.open_async() ...)). SyncPsycopgConnector
does not subclass BaseAsyncConnector and inherits open_async/execute_query_*_
async stubs that raise SyncConnectorConfigurationError, so a
SyncPsycopgConnector-backed App crashes at run_worker() before fetching any
job — sync connectors are defer-only. Every sync call site (web routes, the
periodic tick's own enqueue loop, scripts' `.defer()` calls) keeps working
unchanged: PsycopgConnector implements get_sync_connector(), which lazily
builds an internal SyncPsycopgConnector for pre-open sync use, so ONE async
connector serves both run_worker() and every sync defer. Construction is
still lazy (verified empirically against 3.9.0: constructing PsycopgConnector,
and wrapping it in App(), does not attempt a connection), so an empty/unset
DATABASE_URL stays harmless at import time.

Registry note: `app.tasks` is keyed by each task's fully-qualified dotted name
(``<module>.<function>``, e.g. ``jobcannon.host.tasks.scan``), NOT the bare
function name — verified empirically against 3.9.0. Callers introspecting the
registry (see tests/host/test_scan_tasks.py) must account for this.

`app` itself is constructed in `jobcannon.host.task_app` (issue #135/#136),
not here — this module's own top-level imports (`scan_tasks` -> the ATS-
scanning/fastembed/onnxruntime stack) must never load in the web process,
but the web process still needs to defer tasks declared here by name (see
`jobcannon.host.user_deletion`). Importing the bare `app` object from the
light module keeps that possible; see task_app.py's docstring for the full
mechanism.
"""

from __future__ import annotations

import datetime
import logging
import os

from procrastinate import RetryStrategy
from procrastinate import exceptions as procrastinate_exceptions
from procrastinate import manager as procrastinate_manager

from jobcannon.db._events import delete_expired_events
from jobcannon.db._revoked_subjects import prune_expired_revocations
from jobcannon.db._users import reap_unconverted_anon_users
from jobcannon.host import scan_tasks as _scan_tasks
from jobcannon.host.nightly.config import nightly_monitor_enabled
from jobcannon.host.scan_tasks import (
    run_expiry_check_task,
    run_scan_task,
    run_stale_detect_task,
)
from jobcannon.host.storage_check import check_db_storage
from jobcannon.host.task_app import app

logger = logging.getLogger(__name__)

# Default retention window for non-anon `events` rows (the events-retention
# issue), overridable via the JC_EVENTS_RETENTION_DAYS env var. A named
# module-level constant (rather than an inline literal default, unlike
# JC_ANON_RETENTION_DAYS's "30" above) so the default is one grep away
# instead of buried in a call to os.environ.get.
DEFAULT_EVENTS_RETENTION_DAYS = 365


@app.task(queue="scan")
def scan(company_name: str | None = None) -> dict:
    return run_scan_task([company_name] if company_name else None)


@app.task(queue="maintenance")
def expiry_check() -> None:
    run_expiry_check_task()


@app.task(queue="maintenance")
def stale_detect() -> None:
    run_stale_detect_task()


def _tick_connection():
    """Seam for tests: the tick's DB context. Production: the pooled factory."""
    from jobcannon.db import connection_factory

    return connection_factory()


@app.periodic(
    cron=os.environ.get("JC_SCAN_CRON", "0 */8 * * *"),
    periodic_id="enqueue_due_scans",
)
@app.task(queue="scan", queueing_lock="enqueue_due_scans")
def enqueue_due_scans(timestamp: int) -> dict:
    """Periodic enqueue tick (spec §4): one `scan` job per due company, deduped
    by a per-company queueing lock. Over-enqueueing is safe (engine gates);
    a lock already held (job still todo) is counted, not an error. The tick
    itself carries a queueing lock so a slow tick can't stack behind itself."""
    interval_hours = int(os.environ.get("JC_SCAN_INTERVAL_HOURS", "8"))
    with _tick_connection() as conn:
        due = _scan_tasks._due_company_names(conn, interval_hours=interval_hours)
    enqueued = already = 0
    for name in due:
        try:
            scan.configure(queueing_lock=f"scan:{name}").defer(company_name=name)
            enqueued += 1
        except procrastinate_exceptions.AlreadyEnqueued:
            already += 1
    return {"enqueued": enqueued, "already_enqueued": already}


@app.periodic(
    cron=os.environ.get("JC_STORAGE_CHECK_CRON", "17 6 * * *"),
    periodic_id="db_storage_check",
)
@app.task(queue="maintenance", queueing_lock="db_storage_check")
def db_storage_check(timestamp: int) -> dict:
    """Periodic: report the DB storage percentage through the sanctioned
    scan_health_log recorder so a nearing-tier-limit database shows up
    alongside every other health signal, not just in Render's own email."""
    from jobcannon.db import connection_factory
    from jobcannon.host.health_recorder import record_scan_health

    limit_mb = int(os.environ.get("JC_DB_STORAGE_LIMIT_MB", "5120"))
    with connection_factory() as conn:
        status = check_db_storage(conn, limit_mb=limit_mb)
    record_scan_health(source="db_storage_check", **status)
    return status


@app.periodic(
    cron=os.environ.get("JC_RECLAIM_CRON", "*/15 * * * *"),
    periodic_id="reclaim_orphaned_jobs",
)
@app.task(queue="maintenance", queueing_lock="reclaim_orphaned_jobs")
def reclaim_orphaned_jobs(timestamp: int) -> dict:
    """Reclaim `doing` jobs orphaned by a hard-killed worker (deploy-runbook.md
    "Orphaned `doing` jobs"): if the worker process dies mid-job (e.g. a
    Render redeploy's grace period expiring before an in-flight scan
    finishes), procrastinate's own stalled-worker pruning only deletes the
    dead `procrastinate_workers` row — the job's `worker_id` goes NULL via
    `ON DELETE SET NULL`, but nothing in procrastinate itself resets the
    job's `status` back off `doing`.

    Selection is exactly `status = 'doing' AND worker_id IS NULL` — the
    signature that pruning leaves behind — never a generic age/heartbeat
    "stalled" query: `JobManager.get_stalled_jobs` is heartbeat-based and
    would also match a healthy long-running job whose worker is still
    reporting in. `JobManager.list_jobs` cannot express this predicate either
    — verified against 3.9.0's `list_jobs` SQL, `worker_id=None` means
    "don't filter on worker_id" (`(%(worker_id)s::bigint IS NULL OR
    worker_id = %(worker_id)s)`), not "worker_id IS NULL" — so selection is a
    plain parameterized query and only the mutation goes through the
    JobManager API.

    Disposition is retry, not delete: `JobManager.retry_job_by_id` performs
    the transition (its `procrastinate_retry_job_v2` DB function only
    accepts a job whose status is 'doing' or 'failed', which is exactly the
    set this query selects), and every task on this queue (scan,
    expiry_check, stale_detect) recomputes from source rather than applying
    incremental side effects, so re-running an interrupted one is safe.

    Retry is per-row isolated (issue #110): `retry_job_v2` flips the job back
    to 'todo' but never touches `queueing_lock`, so an orphan can still be
    holding the same `queueing_lock` as an unrelated job a *live* worker
    already re-enqueued (e.g. `enqueue_due_scans` deferred a fresh
    `scan:CollideCo` while the old one was stuck 'doing'). Retrying that
    orphan then collides with `procrastinate_jobs_queueing_lock_idx_v1`
    (`WHERE status = 'todo'`) and raises `procrastinate.exceptions.
    UniqueViolation` — verified empirically against 3.9.0 via a direct
    `procrastinate_retry_job_v2` probe. Left uncaught, one poisoned row would
    abort the whole tick and starve every other orphan behind it, so each
    retry gets its own try/except: a queueing_lock collision is logged and
    skipped (the surviving 'todo' row already covers that work), everything
    else re-raises. The other partial unique index on this table,
    `procrastinate_jobs_lock_idx_v1` (`WHERE status = 'doing'`), can't fire
    from this call site — `retry_job_v2`'s UPDATE only ever writes
    status='todo' or 'failed' — so the constraint-name check is provably
    exhaustive for this call, not a guess.
    """
    from jobcannon.db import connection_factory

    with connection_factory() as conn:
        raw = conn.raw if hasattr(conn, "raw") else conn
        rows = raw.execute(
            "SELECT id FROM procrastinate_jobs WHERE status = 'doing' AND worker_id IS NULL"
        ).fetchall()
    candidate_ids = [row["id"] for row in rows]
    retry_at = datetime.datetime.now(tz=datetime.timezone.utc)
    reclaimed_ids = []
    skipped = 0
    for job_id in candidate_ids:
        try:
            app.job_manager.retry_job_by_id(job_id, retry_at=retry_at)
        except procrastinate_exceptions.UniqueViolation as exc:
            if exc.constraint_name != procrastinate_manager.QUEUEING_LOCK_CONSTRAINT:
                raise
            skipped += 1
            logger.warning(
                "reclaim_orphaned_jobs: skipped job %s, queueing_lock %r already "
                "held by a live 'todo' job",
                job_id,
                exc.queueing_lock,
            )
            continue
        reclaimed_ids.append(job_id)
    return {
        "reclaimed": len(reclaimed_ids),
        "skipped": skipped,
        "disposition": "retry",
        "job_ids": reclaimed_ids,
    }


@app.periodic(
    cron=os.environ.get("JC_ANON_REAP_CRON", "43 6 * * *"),
    periodic_id="reap_anon_users",
)
@app.task(queue="maintenance", queueing_lock="reap_anon_users")
def reap_anon_users(timestamp: int) -> dict:
    """Periodic reaper (#48): deletes anon-namespaced `users` rows (see
    jobcannon.db._users module docstring for the predicate and why it never
    reaps a converted visitor) once they are older than the retention
    window. Returns a count, not the reaped ids — procrastinate persists
    task results, and a user id is PII-adjacent."""
    from jobcannon.db import connection_factory

    retention_days = int(os.environ.get("JC_ANON_RETENTION_DAYS", "30"))
    with connection_factory() as conn:
        reaped_ids = reap_unconverted_anon_users(conn, retention_days=retention_days)
    return {"reaped": len(reaped_ids), "retention_days": retention_days}


@app.periodic(
    cron=os.environ.get("JC_EVENTS_REAP_CRON", "51 6 * * *"),
    periodic_id="reap_old_events",
)
@app.task(queue="maintenance", queueing_lock="reap_old_events")
def reap_old_events(timestamp: int) -> dict:
    """Periodic reaper (the events-retention issue): deletes `events` rows
    for non-anon user ids once they are older than the retention window,
    excluding every type in events_schema.DURABLE_EVENT_TYPES —
    `consent_recorded` (the audit trail) and `user_signed_up` (durable
    signup attribution) — see jobcannon.db._events.delete_expired_events for
    the full predicate. Returns a count, not the reaped ids — procrastinate
    persists task results into the same database being reaped."""
    from jobcannon.db import connection_factory

    retention_days = int(
        os.environ.get("JC_EVENTS_RETENTION_DAYS", str(DEFAULT_EVENTS_RETENTION_DAYS))
    )
    with connection_factory() as conn:
        reaped_ids = delete_expired_events(conn, retention_days=retention_days)
    return {"reaped": len(reaped_ids), "retention_days": retention_days}


@app.periodic(
    cron=os.environ.get("JC_DELETION_RECONCILE_CRON", "24 7 * * *"),
    periodic_id="reconcile_deleted_users",
)
@app.task(queue="maintenance", queueing_lock="reconcile_deleted_users")
def reconcile_deleted_users(timestamp: int) -> dict:
    """Periodic reconciliation sweep (#136): catches a `user.deleted`
    webhook Clerk never delivered (or delivered while this app was down,
    outside Svix's retry window) by directly asking Clerk's Backend API
    about each old, still-present local `users` row. See
    jobcannon.host.user_deletion.run_reconciliation_sweep for the
    lookup/deletion contract (fail-closed on anything but a definitive
    404); this tick only wires the Clerk client and the env-tunable knobs.

    Requires CLERK_SECRET_KEY, now declared on BOTH services
    (jobcannon.host.config's HostConfig.clerk_secret_key expanded from
    web-only, issue #136) specifically so this worker-side task can reuse
    jobcannon.web.auth.build_clerk_client — the repo's one Clerk SDK client
    construction site — rather than building a second one. If the key is
    unset (e.g. a local dev worker with no Clerk configured), this tick
    logs and returns a `clerk_unreachable` status rather than raising or
    crash-looping the worker; this sweep is a catch-net for a rare delivery
    failure, not a hard dependency of every worker tick. Two more non-"ok"
    statuses can come back from `run_reconciliation_sweep` itself (VERIFIED-4
    / F6 / HIGH-3): `degraded` when every checked row's Clerk lookup errored
    (a bare `status: "ok"` would otherwise mask a mid-run Clerk outage even
    though a nonzero `errors` count is buried in the dict), and
    `clerk_misconfigured` when the sweep's own circuit breaker refused to
    delete anything because most/all checked rows came back a definitive 404
    (HIGH-3: a valid key pointed at the wrong Clerk instance reads exactly
    that way too, indistinguishable from a genuine mass deletion without the
    breaker). `checked` is logged unconditionally (even when every count is
    0) so a healthy "nothing to do" sweep is distinguishable in the logs
    from a sweep that silently stopped running at all — this repo has no
    nightly_monitor/deadman infra; the log line is the health signal. Every
    non-"ok" status logs at WARNING or ERROR (see below) so it never reads
    as a healthy tick.
    """
    from jobcannon.db import connection_factory
    from jobcannon.host.config import load_host_config
    from jobcannon.host.user_deletion import run_reconciliation_sweep

    host_config = load_host_config()
    if not host_config.clerk_secret_key:
        logger.error(
            "reconcile_deleted_users: CLERK_SECRET_KEY unset -- cannot "
            "reach Clerk, sweep skipped entirely this tick"
        )
        return {"status": "clerk_unreachable", "checked": 0}

    from jobcannon.web.auth import build_clerk_client

    clerk_client = build_clerk_client(host_config)
    settle_days = int(os.environ.get("JC_DELETION_RECONCILE_SETTLE_DAYS", "3"))
    row_cap = int(os.environ.get("JC_DELETION_RECONCILE_ROW_CAP", "50"))
    result = run_reconciliation_sweep(
        connection_factory,
        clerk_client.users,
        settle_days=settle_days,
        row_cap=row_cap,
    )
    if result.get("status") == "ok":
        logger.info("reconcile_deleted_users: %s", result)
    else:
        # degraded / clerk_misconfigured -- run_reconciliation_sweep already
        # logged the specific reason at WARNING/ERROR; this line is the
        # summary a log-scanning monitor keys on.
        logger.warning("reconcile_deleted_users: non-ok sweep result: %s", result)
    return {"status": "ok", **result}


# MEDIUM-4 (review-1/2/devin): a transient PostHog 5xx/network error must
# not permanently lose a purge -- the local users row is already gone by
# the time this task runs, so nothing ever re-enqueues it once the job
# fails. Bounded (not indefinite): 5 attempts, linear backoff, matches
# purge_person's ~10s per-call timeout budget (posthog_admin._REQUEST_
# TIMEOUT_S) without risking an unbounded retry storm against a real outage.
_PURGE_POSTHOG_PERSON_RETRY = RetryStrategy(max_attempts=5, linear_wait=30)


@app.task(queue="maintenance", retry=_PURGE_POSTHOG_PERSON_RETRY)
def purge_posthog_person(distinct_id: str) -> dict:
    """#135: deletes the PostHog person for `distinct_id` -- already the
    PSEUDONYMOUS id (jobcannon.host.user_deletion.cascade_delete_user is
    this task's only enqueuer, and passes posthog_client.pseudonymize()'s
    output, never a raw Clerk user id). Runs async on the worker's
    `maintenance` queue, never inline in the webhook request thread, so a
    PostHog outage cannot block or delay account deletion itself -- the
    local deletion has already committed by the time this task even runs.
    Fails soft (returns a "skipped" status, no exception) when the PostHog
    admin API credentials aren't configured; see
    jobcannon.host.posthog_admin.purge_person for the full contract.
    Retries a bounded number of times (see _PURGE_POSTHOG_PERSON_RETRY
    above) on a genuine HTTP failure -- purge_person raises on those, never
    on "unconfigured" (which returns a status dict instead, so it is never
    retried -- retrying a permanently-unconfigured purge forever would be
    pure noise)."""
    from jobcannon.host import posthog_admin

    return posthog_admin.purge_person(distinct_id)


@app.periodic(
    cron=os.environ.get("JC_REVOKED_REAP_CRON", "*/15 * * * *"),
    periodic_id="reap_revoked_subjects",
)
@app.task(queue="maintenance", queueing_lock="reap_revoked_subjects")
def reap_revoked_subjects(timestamp: int) -> dict:
    """Periodic reaper (issue #159): hard-deletes `revoked_subjects` rows
    past their own `expires_at`. Unlike `reap_anon_users`/`reap_old_events`
    above, there is no retention-window env var here — the window
    (`jobcannon.db._revoked_subjects.REVOCATION_TTL_MINUTES`, 15 minutes) is
    a code-level security constant sized against Clerk's own ~60s JWT
    lifetime, not a privacy-policy-driven retention period, so it is not a
    render.yaml-declared value the way JC_EVENTS_RETENTION_DAYS/
    JC_ANON_RETENTION_DAYS are.

    Cadence (issue #159 follow-up, privacy-disclosure gap): a Clerk user id
    is a GDPR online identifier, and privacy.md §8 promises deletion is a
    genuine hard delete, not a soft flag or archival copy. The row itself
    (not just the 15-minute blocking window) must therefore track that
    promise. This runs every 15 minutes (same cadence as the existing
    `reclaim_orphaned_jobs` sibling in the scheduler, see
    docs/deploy-runbook.md §8) so worst-case total retention of one id is
    the 15-minute TTL plus up to one more tick before the next sweep finds
    it — ~30 minutes, not 15. State that real bound, not the TTL alone, in
    any disclosure text. An unpruned-but-not-yet-reaped expired row is
    harmless to the auth check itself (`is_subject_revoked`'s
    `expires_at > now()` predicate already excludes it from denying
    access) — this tick's job is retention, not correctness. Returns a
    count, not the reaped ids — procrastinate persists task results into
    the same database being reaped, and a Clerk user id is PII-adjacent."""
    from jobcannon.db import connection_factory

    with connection_factory() as conn:
        reaped_ids = prune_expired_revocations(conn)
    return {"reaped": len(reaped_ids)}


@app.periodic(
    cron=os.environ.get("JC_JD_ADJUDICATION_CRON", "0 12 * * *"),
    periodic_id="jd_adjudication",
)
@app.task(queue="maintenance", queueing_lock="jd_adjudication")
def jd_adjudication(timestamp: int) -> dict:
    """Periodic tick (L-0189, issue #183): adjudicates a bounded batch of
    AMBIGUOUS jd_full rows via jobcannon.host.jd_adjudication_backfill, so
    postings.jd_adjudicated_version gets stamped non-NULL for rows the
    deterministic jd-content contract can't resolve on its own (CLEAN/REJECT).

    This is the writer tests/test_scoring_precheck_wiring_guard.py (#183)
    requires exist under jobcannon/db/ before any host/db/worker module may
    wire scoring (score_and_persist_job / job_scorer.score_job /
    scoring_precheck). Landing this task does NOT itself wire scoring --
    see jobcannon/host/jd_adjudication_backfill.py's module docstring."""
    from jobcannon.db import connection_factory
    from jobcannon.engine import runtime_config
    from jobcannon.host import model_provider as _model_provider
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    config = runtime_config.get_runtime_config()
    with connection_factory() as conn:
        return run_jd_adjudication_backfill(conn, config, call_model=_model_provider.call_model)


@app.periodic(
    cron=os.environ.get("JC_NIGHTLY_SAMPLER_CRON", "*/4 * * * *"),
    periodic_id="nightly_sampler",
)
@app.task(queue="maintenance", queueing_lock="nightly_sampler")
def nightly_sampler(timestamp: int) -> dict:
    """Periodic (Ledger L-0471): watermark-driven checkpoint sampler over
    scan_health_log / procrastinate_jobs, gated OFF by default behind
    JC_NIGHTLY_MONITOR_ENABLED -- the checker below MUST run before any
    other import or DB connection in this function, so a disabled monitor
    costs nothing beyond this one os.environ read (same shape as every
    other gated-but-always-registered periodic in this module: registering
    the shape is not the same as running it, and enabling here is a pure
    env change with no redeploy). See jobcannon/host/nightly/sampler.py's
    module docstring for what this tick does and does not do while dark:
    no LLM spend (call_model stays unwired), no issue filing, only a
    fire-once scan_health_log ERROR row on a forced FAIL verdict."""
    if not nightly_monitor_enabled():
        return {"skipped": "disabled"}
    from jobcannon.host.nightly.sampler import run_sampler_tick

    return run_sampler_tick() or {"skipped": "tick_failed"}

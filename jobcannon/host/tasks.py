"""Procrastinate task-shape declarations for the hosted job taxonomy, plus the
periodic enqueue tick, storage-check tick, orphaned-job reclaim tick,
anon-user reap tick, and events-retention reap tick that fire on a schedule.

Task shapes (`scan`, `expiry_check`, `stale_detect`) and the five periodics
(`enqueue_due_scans`, `db_storage_check`, `reclaim_orphaned_jobs`,
`reap_anon_users`, `reap_old_events`, each declared with `@app.periodic` +
`@app.task` below) all live here. 'enrich' is intentionally not defined: no enrich hook exists
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

from procrastinate import exceptions as procrastinate_exceptions
from procrastinate import manager as procrastinate_manager

from jobcannon.db._events import delete_expired_events
from jobcannon.db._users import reap_unconverted_anon_users
from jobcannon.host import scan_tasks as _scan_tasks
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

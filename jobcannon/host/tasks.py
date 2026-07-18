"""Procrastinate task-shape declarations for the hosted job taxonomy, plus the
periodic enqueue tick and OD-18 storage-check tick that fire on a schedule.

Task shapes (`scan`, `expiry_check`, `stale_detect`) and the two periodics
(`enqueue_due_scans` and `db_storage_check`, each declared with `@app.periodic`
+ `@app.task` below) all live here. 'enrich' is intentionally not defined: no
enrich hook exists to run. Defining a periodic task's SHAPE is not the same as
RUNNING it: this module still never runs a worker or applies procrastinate's
schema at import time — `jobcannon.worker.__main__` owns both (it applies
procrastinate's schema via `_ensure_procrastinate_schema`, then calls
`App.run_worker()`), and `App.run_worker()` is what actually fires every
periodic tick and deferred task on schedule.

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
"""

from __future__ import annotations

import os

import procrastinate

from procrastinate import exceptions as procrastinate_exceptions

from jobcannon.host import scan_tasks as _scan_tasks
from jobcannon.host.scan_tasks import (
    run_expiry_check_task,
    run_scan_task,
    run_stale_detect_task,
)
from jobcannon.host.storage_check import check_db_storage

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=os.environ.get("DATABASE_URL", ""))
)


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
    """OD-18 periodic: report the DB storage percentage through the sanctioned
    scan_health_log recorder so a nearing-tier-limit database shows up
    alongside every other health signal, not just in Render's own email."""
    from jobcannon.db import connection_factory
    from jobcannon.host.health_recorder import record_scan_health

    limit_mb = int(os.environ.get("JC_DB_STORAGE_LIMIT_MB", "5120"))
    with connection_factory() as conn:
        status = check_db_storage(conn, limit_mb=limit_mb)
    record_scan_health(source="db_storage_check", **status)
    return status

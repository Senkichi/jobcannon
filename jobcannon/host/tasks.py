"""Procrastinate task-shape declarations for the hosted job taxonomy.

Wave 2 defines the task callables so PR-10's worker standup is purely additive.
It does NOT run a worker, register a periodic cron, or apply procrastinate's
schema. 'enrich' is intentionally not defined: no enrich hook exists to run.

Connector note: SyncPsycopgConnector (procrastinate 3.9.0, the pinned version
— see pyproject.toml) is the psycopg3 sync connector. It is NOT opened at
construction time (verified empirically against 3.9.0: constructing it, and
wrapping it in App(), does not attempt a connection), so an empty/unset
DATABASE_URL is harmless here — this module only declares task shapes.

Registry note: `app.tasks` is keyed by each task's fully-qualified dotted name
(``<module>.<function>``, e.g. ``jobcannon.host.tasks.scan``), NOT the bare
function name — verified empirically against 3.9.0. Callers introspecting the
registry (see tests/host/test_scan_tasks.py) must account for this.
"""

from __future__ import annotations

import os

import procrastinate

from jobcannon.host.scan_tasks import (
    run_expiry_check_task,
    run_scan_task,
    run_stale_detect_task,
)

app = procrastinate.App(
    connector=procrastinate.SyncPsycopgConnector(conninfo=os.environ.get("DATABASE_URL", ""))
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

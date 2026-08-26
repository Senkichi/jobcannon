"""Issue #19: dead-worker simulation for `reclaim_orphaned_jobs`, on a real
Postgres with the real procrastinate schema — the predicate under test
(`status = 'doing' AND worker_id IS NULL`) is a property of procrastinate's
own `ON DELETE SET NULL` foreign key, not something a mock can stand in for.
"""

import procrastinate
import psycopg
from psycopg.rows import dict_row

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def test_reclaim_orphaned_jobs_retries_the_orphan_and_skips_the_live_job(monkeypatch):
    import jobcannon.worker.__main__ as worker_main  # sets the win32 loop policy on import
    from jobcannon.host import tasks
    from jobcannon.host.config import HostConfig
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    dsn, db_name = create_throwaway_db("jobcannon_reclaim")
    monkeypatch.setenv("DATABASE_URL", dsn)
    try:
        with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=dsn)):
            worker_main._ensure_procrastinate_schema()
        init_engine_seams(HostConfig(database_url=dsn, runtime={}))
        try:
            conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
            try:
                # Dead worker: registered, then its heartbeat row is gone (the
                # same DELETE procrastinate's own stalled-worker pruning does),
                # which sets its job's worker_id to NULL via ON DELETE SET NULL
                # while the job itself is still 'doing' — the orphan signature.
                dead_worker_id = conn.execute(
                    "INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id"
                ).fetchone()["id"]
                orphaned_id = conn.execute(
                    "INSERT INTO procrastinate_jobs (queue_name, task_name, status, worker_id) "
                    "VALUES ('scan', 'jobcannon.host.tasks.scan', 'doing', %s) RETURNING id",
                    (dead_worker_id,),
                ).fetchone()["id"]
                conn.execute("DELETE FROM procrastinate_workers WHERE id = %s", (dead_worker_id,))
                assert (
                    conn.execute(
                        "SELECT worker_id FROM procrastinate_jobs WHERE id = %s", (orphaned_id,)
                    ).fetchone()["worker_id"]
                    is None
                )

                # Live worker: still registered (heartbeat row intact) and its
                # job is also 'doing' — a healthy in-flight job. Must survive
                # untouched: reclaiming it would let a second worker pick up
                # work another worker is still actively running.
                live_worker_id = conn.execute(
                    "INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id"
                ).fetchone()["id"]
                inflight_id = conn.execute(
                    "INSERT INTO procrastinate_jobs (queue_name, task_name, status, worker_id) "
                    "VALUES ('scan', 'jobcannon.host.tasks.scan', 'doing', %s) RETURNING id",
                    (live_worker_id,),
                ).fetchone()["id"]

                with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=dsn)):
                    with tasks.app.open():
                        result = tasks.reclaim_orphaned_jobs(0)

                assert result["reclaimed"] == 1
                assert result["skipped"] == 0
                assert result["disposition"] == "retry"
                assert result["job_ids"] == [orphaned_id]

                orphaned_row = conn.execute(
                    "SELECT status, worker_id FROM procrastinate_jobs WHERE id = %s",
                    (orphaned_id,),
                ).fetchone()
                assert orphaned_row["status"] == "todo"
                assert orphaned_row["worker_id"] is None

                inflight_row = conn.execute(
                    "SELECT status, worker_id FROM procrastinate_jobs WHERE id = %s",
                    (inflight_id,),
                ).fetchone()
                assert inflight_row["status"] == "doing"
                assert inflight_row["worker_id"] == live_worker_id
            finally:
                conn.close()
        finally:
            teardown_engine_seams()
    finally:
        drop_throwaway_db(db_name)


def test_reclaim_orphaned_jobs_skips_a_queueing_lock_collision_and_keeps_going(monkeypatch):
    """Issue #110: retry_job_v2 never touches queueing_lock, so an orphan can
    collide with a live 'todo' job already holding the same lock (e.g. a
    dead worker's stuck scan:CollideCo vs. a fresh enqueue_due_scans re-defer
    of the same company). One poisoned row must not abort the tick — the
    clean orphan alongside it still gets reclaimed, and the collision is
    counted, not silently dropped or fatal."""
    import jobcannon.worker.__main__ as worker_main
    from jobcannon.host import tasks
    from jobcannon.host.config import HostConfig
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    dsn, db_name = create_throwaway_db("jobcannon_reclaim_collide")
    monkeypatch.setenv("DATABASE_URL", dsn)
    try:
        with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=dsn)):
            worker_main._ensure_procrastinate_schema()
        init_engine_seams(HostConfig(database_url=dsn, runtime={}))
        try:
            conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
            try:
                # Colliding orphan: dead worker, still 'doing', holding a
                # queueing_lock that a live 'todo' job (inserted below) also
                # holds. Retrying this one must trip
                # procrastinate_jobs_queueing_lock_idx_v1 and be skipped.
                dead_worker_id = conn.execute(
                    "INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id"
                ).fetchone()["id"]
                colliding_id = conn.execute(
                    "INSERT INTO procrastinate_jobs "
                    "(queue_name, task_name, status, worker_id, queueing_lock) "
                    "VALUES ('scan', 'jobcannon.host.tasks.scan', 'doing', %s, "
                    "'scan:CollideCo') RETURNING id",
                    (dead_worker_id,),
                ).fetchone()["id"]
                conn.execute("DELETE FROM procrastinate_workers WHERE id = %s", (dead_worker_id,))

                # The blocker: a live 'todo' job already holding that same
                # queueing_lock (e.g. a fresh re-enqueue while the old one
                # was stuck). Must survive untouched.
                blocker_id = conn.execute(
                    "INSERT INTO procrastinate_jobs (queue_name, task_name, status, queueing_lock) "
                    "VALUES ('scan', 'jobcannon.host.tasks.scan', 'todo', 'scan:CollideCo') "
                    "RETURNING id"
                ).fetchone()["id"]

                # A clean orphan with a distinct queueing_lock: no collision,
                # must be reclaimed despite the poisoned row above it.
                clean_dead_worker_id = conn.execute(
                    "INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id"
                ).fetchone()["id"]
                clean_id = conn.execute(
                    "INSERT INTO procrastinate_jobs "
                    "(queue_name, task_name, status, worker_id, queueing_lock) "
                    "VALUES ('scan', 'jobcannon.host.tasks.scan', 'doing', %s, "
                    "'scan:CleanCo') RETURNING id",
                    (clean_dead_worker_id,),
                ).fetchone()["id"]
                conn.execute(
                    "DELETE FROM procrastinate_workers WHERE id = %s", (clean_dead_worker_id,)
                )

                with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=dsn)):
                    with tasks.app.open():
                        result = tasks.reclaim_orphaned_jobs(0)

                assert result["reclaimed"] == 1
                assert result["skipped"] == 1
                assert result["disposition"] == "retry"
                assert result["job_ids"] == [clean_id]

                colliding_row = conn.execute(
                    "SELECT status, worker_id FROM procrastinate_jobs WHERE id = %s",
                    (colliding_id,),
                ).fetchone()
                assert colliding_row["status"] == "doing"
                assert colliding_row["worker_id"] is None

                blocker_row = conn.execute(
                    "SELECT status FROM procrastinate_jobs WHERE id = %s", (blocker_id,)
                ).fetchone()
                assert blocker_row["status"] == "todo"

                clean_row = conn.execute(
                    "SELECT status, worker_id FROM procrastinate_jobs WHERE id = %s",
                    (clean_id,),
                ).fetchone()
                assert clean_row["status"] == "todo"
                assert clean_row["worker_id"] is None
            finally:
                conn.close()
        finally:
            teardown_engine_seams()
    finally:
        drop_throwaway_db(db_name)


def test_reclaim_orphaned_jobs_is_a_noop_with_nothing_orphaned(monkeypatch):
    """No candidates -> zero-touch, not an error — the common case on every
    tick (deploy-runbook.md: "a healthy fleet keeps it empty between ticks")."""
    import jobcannon.worker.__main__ as worker_main
    from jobcannon.host import tasks
    from jobcannon.host.config import HostConfig
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    dsn, db_name = create_throwaway_db("jobcannon_reclaim_noop")
    monkeypatch.setenv("DATABASE_URL", dsn)
    try:
        with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=dsn)):
            worker_main._ensure_procrastinate_schema()
        init_engine_seams(HostConfig(database_url=dsn, runtime={}))
        try:
            with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=dsn)):
                with tasks.app.open():
                    result = tasks.reclaim_orphaned_jobs(0)
            assert result == {
                "reclaimed": 0,
                "skipped": 0,
                "disposition": "retry",
                "job_ids": [],
            }
        finally:
            teardown_engine_seams()
    finally:
        drop_throwaway_db(db_name)


def test_reclaim_orphaned_jobs_registered_with_periodic_id_and_queueing_lock():
    """Wiring check, no DB: the periodic/task decorator shape mirrors
    db_storage_check exactly (periodic_id + queue="maintenance" +
    queueing_lock, both keyed to the task's own name)."""
    from jobcannon.host import tasks

    task = tasks.app.tasks["jobcannon.host.tasks.reclaim_orphaned_jobs"]
    assert task.queue == "maintenance"
    assert task.queueing_lock == "reclaim_orphaned_jobs"

    periodic_tasks = tasks.app.periodic_registry.periodic_tasks
    key = ("jobcannon.host.tasks.reclaim_orphaned_jobs", "reclaim_orphaned_jobs")
    assert periodic_tasks[key].periodic_id == "reclaim_orphaned_jobs"

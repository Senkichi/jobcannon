"""THE integration-path test for PR 10: a job deferred through the REAL
procrastinate schema on a REAL Postgres is picked up by run_worker and
executes run_scan_task end-to-end (scan + structural + embed tails). Leaf
tests passing while the dispatch path is broken has bitten this program
three times — this test exists so the worker path cannot ship unproven."""

import procrastinate

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def test_deferred_scan_job_runs_to_success_on_real_postgres(monkeypatch):
    import jobcannon.worker.__main__ as worker_main  # sets the win32 loop policy on import
    from jobcannon.db.migrate import run_migrations
    from jobcannon.engine import services
    from jobcannon.host import tasks
    from jobcannon.host.config import HostConfig
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    dsn, db_name = create_throwaway_db("jobcannon_worker_int")
    monkeypatch.setenv("DATABASE_URL", dsn)
    try:
        run_migrations(dsn)
        init_engine_seams(HostConfig(database_url=dsn, runtime={}))
        try:
            with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=dsn)):
                worker_main._ensure_procrastinate_schema()
                svc = services.get_services()
                with svc.connection_factory() as conn:
                    conn.execute(
                        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, "
                        "ats_probe_status, scan_enabled) VALUES (?, ?, ?, ?, ?, ?)",
                        ("TestCo", "TestCo", "jobvite", "testco", "hit", True),
                    )
                    conn.commit()
                with tasks.app.open():
                    tasks.scan.configure(queueing_lock="scan:TestCo").defer(company_name="TestCo")
                tasks.app.run_worker(
                    queues=["scan"],
                    wait=False,
                    listen_notify=False,
                    install_signal_handlers=False,
                    concurrency=1,
                )
                with svc.connection_factory() as conn:
                    row = conn.execute(
                        "SELECT last_scanned_at FROM companies WHERE name = ?", ("TestCo",)
                    ).fetchone()
                with tasks.app.open():
                    jobs = tasks.app.job_manager.list_jobs()
                statuses = [j.status for j in jobs if j.task_name == "jobcannon.host.tasks.scan"]
                assert statuses == ["succeeded"]
                assert row["last_scanned_at"] is not None
        finally:
            teardown_engine_seams()
    finally:
        drop_throwaway_db(db_name)


def test_ensure_procrastinate_schema_is_noop_on_second_boot(monkeypatch):
    """Models every Render redeploy: the worker image restarts against the
    same already-provisioned DB. The to_regclass probe must find the schema
    already applied and skip apply_schema entirely on the second boot — not
    merely avoid raising (adversarial review finding)."""
    import procrastinate.schema as schema_module

    import jobcannon.worker.__main__ as worker_main
    from jobcannon.host import tasks

    dsn, db_name = create_throwaway_db("jobcannon_worker_second_boot")
    monkeypatch.setenv("DATABASE_URL", dsn)
    try:
        with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=dsn)):
            worker_main._ensure_procrastinate_schema()  # first boot: applies the schema

            calls = []
            monkeypatch.setattr(
                schema_module.SchemaManager, "apply_schema", lambda self: calls.append(1)
            )
            worker_main._ensure_procrastinate_schema()  # second boot: must be a no-op
            assert calls == []
    finally:
        drop_throwaway_db(db_name)

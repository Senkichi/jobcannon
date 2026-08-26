"""Worker entrypoint: ordering and idempotence of the startup sequence,
without a real worker loop (run_worker is monkeypatched out)."""


def test_worker_main_wires_in_order(monkeypatch):
    import jobcannon.worker.__main__ as worker_main

    calls = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored/ignored")
    monkeypatch.setattr(worker_main, "run_migrations", lambda dsn: calls.append(("migrate", dsn)))
    monkeypatch.setattr(
        worker_main, "init_engine_seams", lambda cfg: calls.append(("seams", cfg.database_url))
    )
    monkeypatch.setattr(
        worker_main, "_ensure_procrastinate_schema", lambda: calls.append(("pschema",))
    )

    class _FakeApp:
        def run_worker(self, **kwargs):
            calls.append(("run_worker", kwargs))

    monkeypatch.setattr(worker_main.tasks, "app", _FakeApp())
    worker_main.main()
    assert [c[0] for c in calls] == ["migrate", "seams", "pschema", "run_worker"]
    kwargs = calls[-1][1]
    assert kwargs["queues"] == ["scan", "maintenance"]
    assert kwargs["concurrency"] == 2  # JC_WORKER_CONCURRENCY default

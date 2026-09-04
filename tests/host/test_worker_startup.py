"""Worker entrypoint: ordering and idempotence of the startup sequence,
without a real worker loop (run_worker is monkeypatched out)."""


def test_worker_main_wires_in_order(monkeypatch):
    import jobcannon.worker.__main__ as worker_main

    calls = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored/ignored")
    monkeypatch.delenv("JC_MIGRATE_ALLOW_NEWER_DB", raising=False)
    monkeypatch.setattr(
        worker_main,
        "run_migrations",
        lambda dsn, **kwargs: calls.append(("migrate", dsn, kwargs)),
    )
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
    # allow_newer_db defaults False with the env var unset (fail-closed) --
    # asserted here against the real allow_newer_db_from_env() helper, not a
    # mock of it, so this proves the wiring actually calls through.
    assert calls[0][2] == {"allow_newer_db": False}
    kwargs = calls[-1][1]
    assert kwargs["queues"] == ["scan", "maintenance", "ingest"]
    assert kwargs["concurrency"] == 2  # JC_WORKER_CONCURRENCY default


def test_worker_main_passes_allow_newer_db_override_through(monkeypatch):
    """The worker boot path must honor JC_MIGRATE_ALLOW_NEWER_DB via
    the SAME helper the pre-deploy CLI uses (jobcannon.db.migrate's
    allow_newer_db_from_env), not a re-parsed copy -- issue #196 H1. Only
    run_migrations is mocked (as in test_worker_main_wires_in_order above);
    the env var and the real helper are exercised end to end so this proves
    the actual call, not a stand-in that always returns the same value."""
    import jobcannon.worker.__main__ as worker_main

    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored/ignored")
    monkeypatch.setenv("JC_MIGRATE_ALLOW_NEWER_DB", "1")

    calls = []
    monkeypatch.setattr(
        worker_main, "run_migrations", lambda dsn, **kwargs: calls.append((dsn, kwargs))
    )
    monkeypatch.setattr(worker_main, "init_engine_seams", lambda cfg: None)
    monkeypatch.setattr(worker_main, "_ensure_procrastinate_schema", lambda: None)

    class _FakeApp:
        def run_worker(self, **kwargs):
            pass

    monkeypatch.setattr(worker_main.tasks, "app", _FakeApp())
    worker_main.main()
    assert calls == [("postgresql://ignored/ignored", {"allow_newer_db": True})]

"""Tests for jobcannon/host/nightly/sampler.py and its periodic wrapper in
jobcannon/host/tasks.py (ledger L-0471).

No-DB tests cover the JC_NIGHTLY_MONITOR_ENABLED gate (the periodic's own
docstring claims the checker runs before any import or DB connection --
these tests prove it by making a DB touch an assertion failure) and the
run_sampler_tick() fail-safe wrapper. @requires_postgres tests cover
_tick()'s core loop against real procrastinate_jobs/procrastinate_events
rows: terminal-job detection, watermark-advance-only-on-a-drained-tick, and
the FAIL-escalation fire-once dedup through jobcannon.host.nightly.state's
notified list.
"""

from __future__ import annotations

import contextlib

import procrastinate
import pytest

from tests.host.conftest import requires_postgres


@pytest.fixture(scope="session")
def _nightly_procrastinate_schema(postgres_test_dsn):
    """Apply procrastinate's own queue schema to the shared throwaway test DB.

    tests/host/conftest.py's postgres_test_dsn only runs jobcannon's own
    migrations (run_migrations) -- procrastinate's tables are a second,
    separate schema authority (see jobcannon/worker/__main__.py's module
    docstring, "Two-Schema-Authorities ruling") applied via schema_manager.
    Mirrors tests/host/test_reclaim_orphaned_jobs.py's replace_connector +
    apply_schema precedent, session-scoped here since apply_schema is plain
    CREATEs, not idempotent, and only needs to run once per throwaway DB.

    ``postgres_test_dsn`` is session-scoped and shared with every other test
    file in the same pytest session/xdist worker; tests/host/
    test_nightly_morning.py declares the identical fixture (same name,
    different module -- pytest caches each independently), so whichever of
    the two runs SECOND hits ``DuplicateObject`` on these non-idempotent
    CREATEs. Swallow it here too rather than making fixture-apply ordering
    a silent cross-file dependency.
    """
    from procrastinate.exceptions import ConnectorException

    from jobcannon.host import tasks

    with tasks.app.replace_connector(procrastinate.PsycopgConnector(conninfo=postgres_test_dsn)):
        with tasks.app.open():
            try:
                tasks.app.schema_manager.apply_schema()
            except ConnectorException:
                pass  # already applied by another nightly test file's fixture this session


def _insert_terminal_job(conn, *, task_name: str, status: str, duration_s: float = 5.0) -> int:
    """Insert one terminal procrastinate_jobs row plus its started/terminal
    procrastinate_events pair, ``duration_s`` apart. Returns the job id."""
    row = conn.execute(
        "INSERT INTO procrastinate_jobs (queue_name, task_name, status) "
        "VALUES ('maintenance', %s, %s::procrastinate_job_status) RETURNING id",
        (task_name, status),
    ).fetchone()
    job_id = row["id"]
    event_type = "succeeded" if status == "succeeded" else "failed"
    conn.execute(
        "INSERT INTO procrastinate_events (job_id, type, at) "
        "VALUES (%s, 'started'::procrastinate_job_event_type, now() - make_interval(secs => %s))",
        (job_id, duration_s),
    )
    conn.execute(
        f"INSERT INTO procrastinate_events (job_id, type, at) "
        f"VALUES (%s, '{event_type}'::procrastinate_job_event_type, now())",
        (job_id,),
    )
    return job_id


# --- No-DB: gating and fail-safe wrapping --------------------------------


def test_nightly_sampler_task_skips_when_disabled_and_touches_no_db(monkeypatch):
    monkeypatch.delenv("JC_NIGHTLY_MONITOR_ENABLED", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("connection_factory must not be called when disabled")

    monkeypatch.setattr("jobcannon.db.connection_factory", _boom)
    from jobcannon.host import tasks

    assert tasks.nightly_sampler(0) == {"skipped": "disabled"}


def test_nightly_sampler_task_wraps_none_tick_result_as_tick_failed(monkeypatch):
    monkeypatch.setenv("JC_NIGHTLY_MONITOR_ENABLED", "1")
    monkeypatch.setattr("jobcannon.host.nightly.sampler.run_sampler_tick", lambda: None)
    from jobcannon.host import tasks

    assert tasks.nightly_sampler(0) == {"skipped": "tick_failed"}


def test_nightly_sampler_task_returns_tick_result_when_enabled(monkeypatch):
    monkeypatch.setenv("JC_NIGHTLY_MONITOR_ENABLED", "1")
    monkeypatch.setattr(
        "jobcannon.host.nightly.sampler.run_sampler_tick",
        lambda: {"checkpoints": ["PASS"]},
    )
    from jobcannon.host import tasks

    assert tasks.nightly_sampler(0) == {"checkpoints": ["PASS"]}


def test_run_sampler_tick_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("JC_NIGHTLY_MONITOR_ENABLED", raising=False)
    from jobcannon.host.nightly.sampler import run_sampler_tick

    assert run_sampler_tick() is None


def test_run_sampler_tick_swallows_connection_failure(monkeypatch):
    monkeypatch.setenv("JC_NIGHTLY_MONITOR_ENABLED", "1")

    @contextlib.contextmanager
    def _raising_factory():
        raise RuntimeError("no pool")
        yield  # pragma: no cover -- generator body unreachable, factory raises first

    monkeypatch.setattr("jobcannon.db.connection_factory", _raising_factory)
    from jobcannon.host.nightly.sampler import run_sampler_tick

    assert run_sampler_tick() is None


# --- Real Postgres: _tick()'s core loop -----------------------------------


@requires_postgres
def test_tick_processes_succeeded_job_and_advances_watermark(
    db_conn, _nightly_procrastinate_schema
):
    from jobcannon.host.nightly import sampler, state as nightly_state

    job_id = _insert_terminal_job(db_conn, task_name="nightly_tick_ok", status="succeeded")
    result = sampler._tick(db_conn)

    assert result["new_terminal_jobs"] == 1
    # call_model stays unwired (None) on this dark unit -- every non-forced
    # verdict fails safe to VERDICT_UNAVAILABLE, not PASS/ANOMALY, until a
    # later unit wires a live tenant-scoped call_model.
    assert result["checkpoints"] == ["VERDICT_UNAVAILABLE"]
    assert result["capped"] is False

    state = nightly_state.load_state(db_conn)
    assert state["procrastinate_watermark_id"] == job_id


@requires_postgres
def test_tick_fail_escalation_fires_scan_health_once_per_incident(
    db_conn, monkeypatch, _nightly_procrastinate_schema
):
    from jobcannon.host.nightly import sampler, state as nightly_state

    calls = []
    monkeypatch.setattr(sampler, "record_scan_health", lambda **kw: calls.append(kw))

    job_id = _insert_terminal_job(db_conn, task_name="nightly_tick_fail", status="failed")
    result = sampler._tick(db_conn)

    assert result["checkpoints"] == ["FAIL"]
    assert len(calls) == 1
    assert calls[0]["source"] == "nightly_sampler"
    assert calls[0]["job"] == "nightly_tick_fail"
    assert calls[0]["run_id"] == str(job_id)
    assert calls[0]["verdict"] == "FAIL"
    assert calls[0]["forced"] is True

    # Simulate a retry/replay of the same incident (e.g. a crash between this
    # tick's checkpoint and its watermark write): reset the watermark so the
    # same job is seen again. The fire-once dedup in state.notified must
    # suppress a second scan_health_log write for the same (job, run_id).
    state = nightly_state.load_state(db_conn)
    nightly_state.save_state(db_conn, {**state, "procrastinate_watermark_id": 0}, base=None)

    result2 = sampler._tick(db_conn)
    assert result2["checkpoints"] == ["FAIL"]
    assert len(calls) == 1  # no new record_scan_health call on the replay


@requires_postgres
def test_tick_watermark_advances_only_through_drained_jobs(
    db_conn, monkeypatch, _nightly_procrastinate_schema
):
    from jobcannon.host.nightly import sampler, state as nightly_state

    monkeypatch.setenv("JC_NIGHTLY_MAX_EVENTS_PER_TICK", "1")
    job1 = _insert_terminal_job(db_conn, task_name="nightly_tick_cap_a", status="succeeded")
    job2 = _insert_terminal_job(db_conn, task_name="nightly_tick_cap_b", status="succeeded")

    result = sampler._tick(db_conn)
    assert result["new_terminal_jobs"] == 2
    assert result["capped"] is True
    assert len(result["checkpoints"]) == 1

    state = nightly_state.load_state(db_conn)
    assert state["procrastinate_watermark_id"] == job1

    # Next tick resumes from job1's watermark, not from scratch, and picks
    # up the job the capped tick left behind.
    monkeypatch.delenv("JC_NIGHTLY_MAX_EVENTS_PER_TICK", raising=False)
    result2 = sampler._tick(db_conn)
    assert result2["new_terminal_jobs"] == 1
    assert result2["checkpoints"] == ["VERDICT_UNAVAILABLE"]

    state2 = nightly_state.load_state(db_conn)
    assert state2["procrastinate_watermark_id"] == job2

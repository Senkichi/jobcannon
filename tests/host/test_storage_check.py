"""Storage check: threshold logic (real Postgres) plus the periodic
task wiring (no DB — connection_factory and record_scan_health are mocked)."""

from __future__ import annotations

import contextlib

from tests.host.conftest import requires_postgres


@requires_postgres
def test_storage_check_alerts_at_80pct_and_not_below(db_conn):
    from jobcannon.host.storage_check import check_db_storage

    # A fresh throwaway DB is ~8MB; a 10MB limit puts it over 80%, a 10GB limit under.
    over = check_db_storage(db_conn, limit_mb=10)
    under = check_db_storage(db_conn, limit_mb=10_240)
    assert over["alert"] is True and over["used_pct"] > 0.8
    assert under["alert"] is False and under["used_pct"] < 0.8
    assert over["used_bytes"] == under["used_bytes"] > 0
    assert over["limit_mb"] == 10


def test_db_storage_check_task_reports_through_the_recorder(monkeypatch):
    """No Postgres needed: check_db_storage, connection_factory, and
    record_scan_health are all seams — this proves the periodic task's
    wiring order (open conn -> check -> record) and the recorder call
    contract, not the SQL itself (covered above)."""
    from jobcannon.host import tasks

    calls = []

    monkeypatch.setattr(
        tasks,
        "check_db_storage",
        lambda conn, *, limit_mb: {
            "used_bytes": 1,
            "limit_mb": limit_mb,
            "used_pct": 0.01,
            "alert": False,
        },
    )

    @contextlib.contextmanager
    def _fake_connection_factory():
        yield object()

    def _fake_record_scan_health(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("jobcannon.db.connection_factory", _fake_connection_factory)
    monkeypatch.setattr(
        "jobcannon.host.health_recorder.record_scan_health", _fake_record_scan_health
    )

    result = tasks.db_storage_check(0)

    assert result == {"used_bytes": 1, "limit_mb": 5120, "used_pct": 0.01, "alert": False}
    assert len(calls) == 1
    assert calls[0]["source"] == "db_storage_check"
    assert calls[0]["used_bytes"] == 1
    assert calls[0]["alert"] is False

"""Storage check: threshold logic (real Postgres) plus the periodic
task wiring (no DB — connection_factory and record_scan_health are mocked)."""

from __future__ import annotations

import contextlib

from tests.host.conftest import requires_postgres


@requires_postgres
def test_storage_check_alerts_at_80pct_and_not_below(db_conn):
    from jobcannon.host.storage_check import check_db_storage

    # A fresh throwaway DB is ~8MB; a 10MB limit puts it over 80%, a 10GB limit under.
    # Ground-truth anchors are taken immediately after each check_db_storage
    # call (see the anchor assertions below for why these exist).
    over = check_db_storage(db_conn, limit_mb=10)
    direct_over = db_conn.execute(
        "SELECT pg_database_size(current_database()) AS used_bytes"
    ).fetchone()["used_bytes"]
    under = check_db_storage(db_conn, limit_mb=10_240)
    direct_under = db_conn.execute(
        "SELECT pg_database_size(current_database()) AS used_bytes"
    ).fetchone()["used_bytes"]
    assert over["alert"] is True and over["used_pct"] > 0.8
    assert under["alert"] is False and under["used_pct"] < 0.8
    assert over["limit_mb"] == 10

    # `used_bytes` comes from independent pg_database_size() reads taken a few
    # milliseconds apart, not a single snapshot. It is NOT guaranteed to be
    # byte-identical: autovacuum's autoanalyze can fire between calls and
    # unlink+recreate the catalog relcache-init file (an arbitrary-size blob,
    # not page-aligned) — empirically confirmed on this box: the file is
    # exactly 160944 bytes, and 40 back-to-back reads taken in complete
    # isolation (no other suite activity) produced only deltas of {0, 160944}
    # (#201: the flake was NOT a full-suite-load artifact; a bare
    # byte-equality assertion between two live reads was just brittle). What
    # the alarm actually depends on is that used_bytes reflects the real,
    # current database size regardless of which limit_mb was passed in — not
    # that reads are pixel-identical — so bound reads to within a generous
    # multiple of the server's own block size (never hardcoded) rather than
    # requiring exact equality.
    block_size = db_conn.execute(
        "SELECT current_setting('block_size')::int AS block_size"
    ).fetchone()["block_size"]
    tolerance = (
        block_size * 128
    )  # ~1 MiB: many autoanalyze cycles of headroom, still << the ~9MB throwaway DB
    assert over["used_bytes"] > 0 and under["used_bytes"] > 0
    assert abs(over["used_bytes"] - under["used_bytes"]) <= tolerance

    # Ground-truth anchor: the assertion above only proves the two
    # check_db_storage reads AGREE with each other — a check that reads the
    # wrong database, or a single relation instead of the whole database,
    # would satisfy it too, as long as both calls hit the same wrong source.
    # Anchor each read against an independent, direct pg_database_size()
    # query so the assertion proves the check reports what Postgres itself
    # reports, not merely that it's internally self-consistent.
    assert abs(over["used_bytes"] - direct_over) <= tolerance
    assert abs(under["used_bytes"] - direct_under) <= tolerance


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

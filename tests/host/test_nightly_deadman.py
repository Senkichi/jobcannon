"""Tests for jobcannon/host/nightly/deadman.py (ledger L-0387).

No-DB tests cover the JC_NIGHTLY_MONITOR_ENABLED gate and the
run_deadman_check() fail-safe wrapper, mirroring
tests/host/test_nightly_sampler.py's split. @requires_postgres tests cover
_check()'s core logic against a real nightly_monitor_state row: deadline
math (UTC morning_hour/morning_minute + fixed grace), report-present
short-circuit, and the fire-once notified dedup.

Ported from private's tests/test_nightly_monitor_deadman.py
@ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8 -- DROPPED: the four
TestReportFileBeltAndSuspenders cases (D12 local report.md existence
check). That check has no host equivalent (see deadman.py's module
docstring: state.save_state is a single atomic Postgres UPSERT, so there is
no second artifact for a stale state row to diverge from) and is not
ported.
"""

from __future__ import annotations

import contextlib
from datetime import datetime

from tests.host.conftest import requires_postgres

# --- No-DB: gating and fail-safe wrapping --------------------------------


def test_run_deadman_check_disabled_touches_no_db(monkeypatch):
    monkeypatch.delenv("JC_NIGHTLY_MONITOR_ENABLED", raising=False)

    def _boom(*args, **kwargs):
        raise AssertionError("connection_factory must not be called when disabled")

    monkeypatch.setattr("jobcannon.db.connection_factory", _boom)
    from jobcannon.host.nightly.deadman import run_deadman_check

    result = run_deadman_check()
    assert result == {
        "enabled": False,
        "alerted": False,
        "notified": False,
        "reason": "nightly_monitor disabled",
    }


def test_run_deadman_check_swallows_connection_failure(monkeypatch):
    monkeypatch.setenv("JC_NIGHTLY_MONITOR_ENABLED", "1")

    @contextlib.contextmanager
    def _raising_factory():
        raise RuntimeError("no pool")
        yield  # pragma: no cover -- generator body unreachable, factory raises first

    monkeypatch.setattr("jobcannon.db.connection_factory", _raising_factory)
    from jobcannon.host.nightly.deadman import run_deadman_check

    result = run_deadman_check()
    assert result["enabled"] is True
    assert result["alerted"] is False
    assert result["reason"] == "deadman check failed"


# --- Real Postgres: _check()'s core logic ---------------------------------


@requires_postgres
def test_before_deadline_is_noop(db_conn, monkeypatch):
    from jobcannon.host.nightly import deadman

    calls = []
    monkeypatch.setattr(deadman, "record_scan_health", lambda **kw: calls.append(kw))

    now = datetime(2026, 7, 19, 6, 0, 0)  # before default 05:30 + 90min = 07:00 UTC
    result = deadman._check(db_conn, _now=now)

    assert result["alerted"] is False
    assert "deadline not reached" in result["reason"]
    assert calls == []


@requires_postgres
def test_report_present_today_is_noop(db_conn, monkeypatch):
    from jobcannon.host.nightly import deadman, state as nightly_state

    calls = []
    monkeypatch.setattr(deadman, "record_scan_health", lambda **kw: calls.append(kw))

    state = nightly_state.load_state(db_conn)
    nightly_state.save_state(db_conn, {**state, "last_report_date": "2026-07-19"}, base=state)

    now = datetime(2026, 7, 19, 12, 0, 0)
    result = deadman._check(db_conn, _now=now)

    assert result["alerted"] is False
    assert result["reason"] == "report already present for 2026-07-19"
    assert calls == []


@requires_postgres
def test_missing_report_notifies_once(db_conn, monkeypatch):
    from jobcannon.host.nightly import deadman, state as nightly_state

    calls = []
    monkeypatch.setattr(deadman, "record_scan_health", lambda **kw: calls.append(kw))

    now = datetime(2026, 7, 19, 12, 0, 0)
    result = deadman._check(db_conn, _now=now)

    assert result["enabled"] is True
    assert result["alerted"] is True
    assert result["notified"] is True
    assert "No report for 2026-07-19" in result["reason"]
    assert len(calls) == 1
    assert calls[0]["source"] == "nightly_monitor"
    assert calls[0]["kind"] == "deadman_report_missing"
    assert calls[0]["level"] == "ERROR"
    assert calls[0]["date"] == "2026-07-19"

    state = nightly_state.load_state(db_conn)
    assert "2026-07-19:deadman" in state["notified"]

    # Fire-once: second check on the same day is silent.
    result2 = deadman._check(db_conn, _now=now)
    assert result2["alerted"] is False
    assert "already notified" in result2["reason"]
    assert len(calls) == 1


@requires_postgres
def test_deadline_respects_config_morning_time(db_conn, monkeypatch):
    from jobcannon.host.nightly import deadman

    calls = []
    monkeypatch.setattr(deadman, "record_scan_health", lambda **kw: calls.append(kw))
    monkeypatch.setenv("JC_NIGHTLY_MORNING_HOUR", "6")
    monkeypatch.setenv("JC_NIGHTLY_MORNING_MINUTE", "0")

    # 06:00 + 90 min = 07:30 UTC deadline
    before = datetime(2026, 7, 19, 7, 15, 0)
    after = datetime(2026, 7, 19, 7, 31, 0)

    result_before = deadman._check(db_conn, _now=before)
    assert result_before["alerted"] is False
    assert "deadline not reached" in result_before["reason"]
    assert calls == []

    result_after = deadman._check(db_conn, _now=after)
    assert result_after["alerted"] is True
    assert len(calls) == 1


@requires_postgres
def test_notified_persists_and_suppresses(db_conn, monkeypatch):
    from jobcannon.host.nightly import deadman, state as nightly_state

    calls = []
    monkeypatch.setattr(deadman, "record_scan_health", lambda **kw: calls.append(kw))

    state = nightly_state.load_state(db_conn)
    nightly_state.save_state(db_conn, {**state, "notified": ["2026-07-19:deadman"]}, base=state)

    now = datetime(2026, 7, 19, 12, 0, 0)
    result = deadman._check(db_conn, _now=now)

    assert result["alerted"] is False
    assert "already notified" in result["reason"]
    assert calls == []

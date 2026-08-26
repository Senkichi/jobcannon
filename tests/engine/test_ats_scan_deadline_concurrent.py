"""Concurrent-path deadline regression tests (issue #39).

Pre-fix, the concurrent branch of ``_run_ats_api_scan`` deadlocked instead of
truncating whenever the scan deadline tripped with submitted futures still
queued: ``executor.shutdown(wait=False, cancel_futures=True)`` leaves drained
futures in state ``CANCELLED``, and ``as_completed()`` only counts a
cancelled future as done after the ``CANCELLED_AND_NOTIFIED`` transition —
performed exclusively by a worker thread picking the item up, which never
happens for a drained item. The fix replaces the three mid-loop shutdowns
with a per-scan ``abort_scan = threading.Event()`` that every worker checks
once at entry (returning a ``deadline_skipped`` no-op result when set), so
every submitted future always completes normally and ``as_completed()``
terminates.

Determinism: no elapsed-time assertions, no sleep-based ordering. The tests
synchronize on events/semaphores only:
- worker entry/exit is gated by ``threading.Event``s / ``threading.Semaphore``s
  inside the fake worker;
- the moment the scan flips its internal ``abort_scan`` event is observed by
  monkeypatching ``_run``'s module-level ``threading`` attribute with a
  ``SimpleNamespace(Event=...)`` whose ``Event`` subclass signals the test
  from inside ``set()``.

Each scan runs on a daemon thread with a 30s join timeout, so a regression
(deadlock) fails the assertion instead of wedging the suite.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from jobcannon.engine import services
from jobcannon.engine.ats_scanner import _run as _run_mod
from jobcannon.engine.ats_scanner._run import (
    _CompanyScanResult,
    _run_ats_api_scan,
    _scan_one_company_worker,
)
from tests.engine.helpers.ats_scan_services import create_scan_schema, make_scan_services


class _Ticker:
    """Deterministic ``time.monotonic()`` stand-in: yields the scripted
    values in order, then repeats the final value forever. See
    test_ats_scan_deadline.py's identical helper for the full rationale."""

    def __init__(self, values: list[float]) -> None:
        assert values, "_Ticker needs at least one value"
        self._values = list(values)
        self.calls: list[float] = []

    def __call__(self) -> float:
        idx = min(len(self.calls), len(self._values) - 1)
        value = self._values[idx]
        self.calls.append(value)
        return value


def _base_summary() -> dict:
    return {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}


def _insert_hit_companies(conn: sqlite3.Connection, count: int) -> None:
    platforms = ["greenhouse", "lever", "ashby"]
    for i in range(count):
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'hit', 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
            (f"co{i}", f"Co{i}", platforms[i % 3], f"co{i}"),
        )
    conn.commit()


def _ok_result(company) -> _CompanyScanResult:
    return _CompanyScanResult(
        company_name=company["name_raw"],
        jobs_discovered=1,
        jobs_new=[],
        skipped_title_filter=0,
    )


def _skipped_result(company) -> _CompanyScanResult:
    return _CompanyScanResult(
        company_name=company["name_raw"],
        jobs_discovered=0,
        jobs_new=[],
        skipped_title_filter=0,
        deadline_skipped=True,
    )


def _abort_observer(abort_set_observed: threading.Event) -> SimpleNamespace:
    """A ``threading``-namespace stand-in for ``_run_mod`` whose
    ``Event.set()`` also signals the test the moment the scan flips its
    internal ``abort_scan`` flag."""

    class _SignalingEvent(threading.Event):
        def set(self) -> None:
            super().set()
            abort_set_observed.set()

    return SimpleNamespace(Event=_SignalingEvent)


def _drive_scan(db_path: str, summary: dict, box: dict, *, concurrency: int) -> threading.Thread:
    """Run _run_ats_api_scan on a daemon thread; exceptions land in box."""

    def _target() -> None:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            _run_ats_api_scan(
                conn,
                db_path,
                ["Engineer"],
                [],
                summary,
                [],
                high_score_threshold=999,
                dormancy_threshold=10,
                dormancy_interval_days=3,
                tracker=None,
                company_names=None,
                workday_max_pages=None,
                scan_concurrency=concurrency,
                deadline_monotonic=100.0,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced via the box
            box["exc"] = exc
        finally:
            conn.close()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t


def test_concurrent_deadline_mid_drain_success_branch_truncates_not_deadlocks(
    tmp_path, monkeypatch
):
    """Deadline trips in the DRAIN loop's SUCCESS branch with futures still
    queued: the scan must truncate and return, not deadlock (pre-fix: hangs
    forever on the drained futures stranded in CANCELLED)."""
    db_path = str(tmp_path / "scan.db")
    conn = sqlite3.connect(db_path)
    create_scan_schema(conn)
    _insert_hit_companies(conn, 6)
    conn.close()

    # Six pre-deadline reads cover the submit loop's per-company checks, so
    # all six companies are queued; every later read is past the deadline, so
    # the trip can only come from the drain loop's own (success-branch) check.
    monkeypatch.setattr(time, "monotonic", _Ticker([0.0] * 6 + [1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    abort_set_observed = threading.Event()
    monkeypatch.setattr(_run_mod, "threading", _abort_observer(abort_set_observed))

    gate = threading.Event()

    def fake_worker(company, _db, _tt, _te, _wmp, abort, _config=None):
        if abort is not None and abort.is_set():
            return _skipped_result(company)
        if company["name_raw"] == "Co0":
            return _ok_result(company)  # completes immediately -> drain check fires
        gate.wait(timeout=20.0)
        return _ok_result(company)

    summary = _base_summary()
    box: dict = {}
    services.set_services(make_scan_services(db_path))
    try:
        with patch.object(_run_mod, "_scan_one_company_worker", side_effect=fake_worker):
            t = _drive_scan(db_path, summary, box, concurrency=2)
            # Deterministic ordering: Co0's result reaches the drain loop,
            # the drain-loop success-branch deadline check fires, and ONLY
            # once the scan has flipped its abort event do we release the
            # two gated workers.
            assert abort_set_observed.wait(timeout=10.0), "scan never flipped its abort event"
            gate.set()
            t.join(timeout=30.0)
    finally:
        services.clear_services()

    assert not t.is_alive(), (
        "_run_ats_api_scan DEADLOCKED: the deadline tripped mid-drain with "
        "queued futures and as_completed() never terminated"
    )
    assert "exc" not in box, f"scan raised: {box.get('exc')!r}"
    assert summary.get("truncated") is True
    # Co0 plus the two already-running gated workers are always merged
    # (a started worker is always drained). Whether one of the
    # three still-queued companies also gets merged is a genuine race, not a
    # bug: after Co0's fast worker returns, its now-free pool thread races
    # the main thread's abort_scan.set() (a few lines below the drain-loop
    # deadline check) for the pickup of the next queued company. If the pool
    # thread dequeues and the worker's abort-check runs before
    # abort_scan.set() lands, that company scans for real and
    # companies_scanned == 3; if abort_scan.set() wins, the worker no-ops at
    # entry and companies_scanned == 2. Both are correct post-fix behavior —
    # the invariant under test is termination and truncation, not this exact
    # count, so accept either outcome.
    assert summary["companies_scanned"] in (2, 3)


def test_concurrent_deadline_during_submit_truncates_not_deadlocks(tmp_path, monkeypatch):
    """Deadline trips in the SUBMIT loop with a future already queued: the
    other former shutdown site, cancelling before as_completed() snapshots.
    Deterministic == 2 via a semaphore confirming both pool threads are
    gated (so the third company is provably still queued, not merely
    unlucky)."""
    db_path = str(tmp_path / "scan.db")
    conn = sqlite3.connect(db_path)
    create_scan_schema(conn)
    _insert_hit_companies(conn, 6)
    conn.close()

    # Reads 1-3 are pre-deadline (companies 0-2 submitted); read 4 is past,
    # so the submit loop breaks with one future queued behind two running.
    monkeypatch.setattr(time, "monotonic", _Ticker([0.0, 0.0, 0.0, 1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    abort_set_observed = threading.Event()
    monkeypatch.setattr(_run_mod, "threading", _abort_observer(abort_set_observed))

    gate = threading.Event()
    entered = threading.Semaphore(0)

    def fake_worker(company, _db, _tt, _te, _wmp, abort, _config=None):
        if abort is not None and abort.is_set():
            return _skipped_result(company)
        entered.release()
        gate.wait(timeout=20.0)
        return _ok_result(company)

    summary = _base_summary()
    box: dict = {}
    services.set_services(make_scan_services(db_path))
    try:
        with patch.object(_run_mod, "_scan_one_company_worker", side_effect=fake_worker):
            t = _drive_scan(db_path, summary, box, concurrency=2)
            # Both pool threads are inside gated workers (companies 0, 1) —
            # confirmed deterministically via the semaphore, not a race — so
            # company 2 stays queued and cannot be picked up early; wait for
            # the submit-loop break to flip the abort event before release.
            assert entered.acquire(timeout=10.0), "first worker never started"
            assert entered.acquire(timeout=10.0), "second worker never started"
            assert abort_set_observed.wait(timeout=10.0), "scan never flipped its abort event"
            gate.set()
            t.join(timeout=30.0)
    finally:
        services.clear_services()

    assert not t.is_alive(), (
        "_run_ats_api_scan DEADLOCKED at the submit-loop truncation site with a queued future"
    )
    assert "exc" not in box, f"scan raised: {box.get('exc')!r}"
    assert summary.get("truncated") is True
    # Companies 0 and 1 were running and are merged; company 2 was queued
    # behind the deadline and must be skipped; companies 3-5 never submitted.
    assert summary["companies_scanned"] == 2


def test_concurrent_deadline_mid_drain_except_branch_truncates_not_deadlocks(tmp_path, monkeypatch):
    """Deadline trips inside the drain loop's EXCEPT branch (the third
    former shutdown(wait=False, cancel_futures=True) site): a worker raises
    an ordinary exception, and the deadline check that runs while handling
    that exception is the one that must trip the abort — not the success
    branch's check. Pre-fix, this site's cancel_futures leaves the same
    CANCELLED-future deadlock as the other two sites."""
    db_path = str(tmp_path / "scan.db")
    conn = sqlite3.connect(db_path)
    create_scan_schema(conn)
    _insert_hit_companies(conn, 6)
    conn.close()

    # Six pre-deadline reads cover the submit loop's per-company checks, so
    # all six companies are queued; the seventh read is inside the EXCEPT
    # branch's own deadline check (Co0's worker raises and is the first
    # future drained), so the trip can only come from that branch.
    monkeypatch.setattr(time, "monotonic", _Ticker([0.0] * 6 + [1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    abort_set_observed = threading.Event()
    monkeypatch.setattr(_run_mod, "threading", _abort_observer(abort_set_observed))

    gate = threading.Event()

    def fake_worker(company, _db, _tt, _te, _wmp, abort, _config=None):
        if abort is not None and abort.is_set():
            return _skipped_result(company)
        if company["name_raw"] == "Co0":
            raise RuntimeError("simulated worker failure")  # completes immediately
        gate.wait(timeout=20.0)
        return _ok_result(company)

    summary = _base_summary()
    box: dict = {}
    services.set_services(make_scan_services(db_path))
    try:
        with patch.object(_run_mod, "_scan_one_company_worker", side_effect=fake_worker):
            t = _drive_scan(db_path, summary, box, concurrency=2)
            # Deterministic ordering: Co0's exception reaches the drain
            # loop's except branch first, that branch's own deadline check
            # fires, and ONLY once the scan has flipped its abort event do we
            # release the gated workers. gate.wait() on an already-set Event
            # returns immediately, so no ordering of the remaining pickups
            # can deadlock.
            assert abort_set_observed.wait(timeout=10.0), "scan never flipped its abort event"
            gate.set()
            t.join(timeout=30.0)
    finally:
        services.clear_services()

    assert not t.is_alive(), (
        "_run_ats_api_scan DEADLOCKED: the deadline tripped in the drain loop's "
        "except branch with queued futures and as_completed() never terminated"
    )
    assert "exc" not in box, f"scan raised: {box.get('exc')!r}"
    assert summary.get("truncated") is True
    assert any("simulated worker failure" in err for err in summary["errors"]), (
        f"expected the except-branch failure recorded in summary['errors'], got {summary['errors']!r}"
    )


def test_worker_abort_set_skips_before_any_work(tmp_path):
    """The real worker's entry check: with abort already set it returns a
    deadline_skipped no-op without opening a connection or touching the
    scan surface (services.get_services / connection_factory / platform
    scanner). Patches _run's public seams with fail-if-called mocks rather
    than relying solely on the entry short-circuit."""
    db_path = str(tmp_path / "scan.db")
    conn = sqlite3.connect(db_path)
    create_scan_schema(conn)
    _insert_hit_companies(conn, 1)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM companies WHERE name_raw = 'Co0'").fetchone()
    conn.close()

    abort = threading.Event()
    abort.set()

    def _fail_if_called(*_a, **_k):
        raise AssertionError("worker touched the DB/scan path despite abort being set")

    with (
        patch.object(_run_mod, "get_services", side_effect=_fail_if_called),
        patch.object(_run_mod, "run_platform_scan", side_effect=_fail_if_called),
    ):
        result = _scan_one_company_worker(row, db_path, ["Engineer"], [], None, abort)

    assert result.deadline_skipped is True
    assert result.error is None
    assert result.jobs_discovered == 0
    assert result.jobs_new == []

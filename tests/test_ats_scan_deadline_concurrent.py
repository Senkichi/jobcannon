"""Concurrent-path deadline regression tests (issue #1658).

Pre-fix, the concurrent branch of ``_run_ats_api_scan`` deadlocked instead of
truncating whenever the #1368 scan deadline tripped with submitted futures
still queued: ``executor.shutdown(wait=False, cancel_futures=True)`` leaves
drained futures in state ``CANCELLED``, and ``as_completed()`` only counts a
cancelled future as done after the ``CANCELLED_AND_NOTIFIED`` transition —
performed exclusively by a worker thread picking the item up, which never
happens for a drained item. The fix replaces the mid-loop shutdowns with an
abort ``threading.Event`` checked at worker entry, so every future completes.

Determinism: no elapsed-time assertions and no sleep-based ordering. The
tests synchronize on threading primitives only:
- worker entry/exit is gated by ``threading.Event``s inside the fake worker
  (test 2 also uses a ``threading.Semaphore`` whose release/acquire pair is
  the happens-before edge that establishes the "both pool threads are busy"
  premise — not a wall-clock timing assumption);
- the moment the scan flips its internal abort event is observed by patching
  the module's ``threading`` namespace with an Event subclass whose ``set()``
  also signals the test.

The scan runs on a daemon thread with a hang-detector join ceiling
(``THREAD_SYNC_HANG_TIMEOUT_S`` from ``tests.helpers.timeouts``) so a
regression fails the assertion instead of wedging the suite. The same
generous ceiling backs every ``Event.wait`` / ``Semaphore.acquire`` /
``Thread.join`` in this file — they are hang detectors, not speed
assertions (issue #1877: the old 10 s bound on worker-start acquire
flaked on the self-hosted runner under concurrent CI load).

Issue #1984 refined this further for the one test whose deadline trip is
decided in the *submit* loop (mocked ``time.monotonic`` call count only,
zero dependency on the pool's worker threads actually being scheduled):
racing that trip against a wall-clock ``entered.acquire(timeout=...)`` in
the test body let the deadline fire before either worker got CPU time under
load. That test's mocked clock now blocks its own tripping read on the
same worker-started semaphore, so the deadline literally cannot advance
until the workers it expects to be mid-scan have demonstrably started —
starvation makes the test slower, never flaky. The other two deadline-trip
tests trip inside the *drain* loop, which only runs once a worker's future
has actually completed, so they were never subject to this race.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import jobcannon.engine.ats_scanner._run as _run_mod
from jobcannon.engine.ats_scanner._run import (
    _CompanyScanResult,
    _run_ats_api_scan,
    _scan_one_company_worker,
)
from tests.helpers.timeouts import THREAD_SYNC_HANG_TIMEOUT_S
from tests.test_ats_scan_concurrency import _base_summary, _insert_test_companies


def _scan_state_diag(
    scan_thread: threading.Thread,
    box: dict,
    *,
    abort_set: threading.Event | None = None,
    gate: threading.Event | None = None,
) -> str:
    """Format the scan thread's live state for an assertion-failure message.

    When a thread-synchronization wait times out, the cause is almost never
    visible from the wait site alone — it is either a wedged scan thread, an
    exception that escaped the scan target, or an abort/gate event in an
    unexpected state. Dumping all of them at once turns a flake into a
    diagnostic instead of a re-run (issue #1877).
    """
    parts = [f"scan_thread_alive={scan_thread.is_alive()}"]
    if "exc" in box:
        parts.append(f"scan_exc={box['exc']!r}")
    if abort_set is not None:
        parts.append(f"abort_set={abort_set.is_set()}")
    if gate is not None:
        parts.append(f"gate_set={gate.is_set()}")
    return f"({_scan_state_diag.__name__}: {', '.join(parts)})"


class _Ticker:
    """Deterministic time.monotonic stand-in: yields the scripted values in
    order, then repeats the final value forever."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._i = 0

    def __call__(self) -> float:
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


class _GatedDeadlineTicker:
    """``time.monotonic`` stand-in whose deadline-tripping read blocks until
    the workers it expects to already be mid-scan have demonstrably started.

    Issue #1984: the submit loop's deadline check
    (``job_finder/web/ats_scanner/_run.py``) runs on the main thread against
    this mocked clock with no dependency on whether the pool's worker
    threads have actually been scheduled onto a CPU yet. Tripping on a
    fixed call count let the deadline fire arbitrarily fast in wall-clock
    terms, racing the OS thread scheduler that starts the pool workers —
    under runner load, ``abort_scan.set()`` (which fires the instant this
    read trips) could land before either worker got CPU time, so both saw
    abort already set and no-opped instead of signaling ``started_gate``.

    Fix: gate the read itself on that same signal. Once the scripted
    pre-deadline values are exhausted, the first "past deadline" read
    blocks — using the real wall clock via ``Semaphore.acquire(timeout=)``,
    unaffected by this module's own ``time.monotonic`` mock — until
    ``needed_starts`` workers have released ``started_gate``, i.e. until
    they've passed their own abort check and are genuinely running. The
    deadline clock now waits on worker starvation instead of racing it, so
    the ceiling only ever makes a starved run slower, never flaky.
    """

    def __init__(
        self,
        pre_values: list[float],
        tripped_value: float,
        started_gate: threading.Semaphore,
        *,
        needed_starts: int,
    ) -> None:
        self._pre = list(pre_values)
        self._tripped = tripped_value
        self._gate = started_gate
        self._needed = needed_starts
        self._i = 0
        self._armed = False

    def __call__(self) -> float:
        if self._i < len(self._pre):
            v = self._pre[self._i]
            self._i += 1
            return v
        if not self._armed:
            for n in range(self._needed):
                if not self._gate.acquire(timeout=THREAD_SYNC_HANG_TIMEOUT_S):
                    raise AssertionError(
                        f"deadline clock held past worker start #{n + 1}/{self._needed} "
                        f"but it never arrived (entered.release() never called) — "
                        f"worker starvation exceeded the {THREAD_SYNC_HANG_TIMEOUT_S}s "
                        "hang ceiling"
                    )
            self._armed = True
        return self._tripped


def _ok_result(company) -> _CompanyScanResult:
    return _CompanyScanResult(
        company_name=company["name_raw"],
        jobs_discovered=1,
        # Plant the company name as a distinguishable job key so the caller
        # can verify per-company that a completed worker's result was actually
        # merged into all_new_job_keys (issue #1130 identity check).
        jobs_new=[company["name_raw"]],
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


def _drive_scan(
    db_path: str,
    summary: dict,
    box: dict,
    *,
    concurrency: int,
    all_new_job_keys: list,
) -> threading.Thread:
    """Run _run_ats_api_scan on a daemon thread; exceptions land in box.

    ``all_new_job_keys`` is the shared list the scan extends with each merged
    worker's job keys; the caller inspects it after the join to verify the
    #1130 per-company identity invariant.
    """

    def _target() -> None:
        try:
            from jobcannon.engine.db_helpers import standalone_connection

            with standalone_connection(db_path) as conn:
                _run_ats_api_scan(
                    conn,
                    db_path,
                    ["Engineer"],
                    [],
                    summary,
                    all_new_job_keys,
                    high_score_threshold=999,
                    dormancy_threshold=10,
                    dormancy_interval_days=3,
                    tracker=None,
                    company_names=None,
                    workday_max_pages=None,
                    scan_concurrency=concurrency,
                    deadline_monotonic=100.0,
                )
        except BaseException as exc:
            box["exc"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t


def _abort_observer(abort_set_observed: threading.Event) -> SimpleNamespace:
    """A threading-namespace stand-in for _run_mod whose Event.set() also
    signals the test the moment the scan flips its internal abort flag."""

    class _SignalingEvent(threading.Event):
        def set(self) -> None:
            super().set()
            abort_set_observed.set()

    return SimpleNamespace(Event=_SignalingEvent)


def test_concurrent_deadline_mid_drain_truncates_not_deadlocks(migrated_db_path, monkeypatch):
    """Deadline trips in the DRAIN loop with futures still queued: the scan
    must truncate and return, not deadlock (pre-fix: hangs forever)."""
    _insert_test_companies(migrated_db_path, count=6)

    # Six pre-deadline reads cover the submit loop's per-company checks, so
    # all six companies are queued; every later read is past the deadline, so
    # the trip can only come from the drain loop's own check.
    monkeypatch.setattr(time, "monotonic", _Ticker([0.0] * 6 + [1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    abort_set_observed = threading.Event()
    monkeypatch.setattr(_run_mod, "threading", _abort_observer(abort_set_observed))

    gate = threading.Event()
    # Derive the "fast" company from the scan's actual iteration order rather
    # than hardcoding "Company0": the query's ORDER BY leaves all test
    # companies tied on NULL last_scanned_at, so SQLite's row-order among
    # them is an implementation detail, not a guarantee (issue #1668 nit 3).
    fast: list[str | None] = [None]
    fast_lock = threading.Lock()
    completed: set[str] = set()
    completed_lock = threading.Lock()

    def fake_worker(
        company, _db, _tt, _te, _wmp, abort, _config=None, _run_id=None, *_extra, **_kw
    ):
        if abort is not None and abort.is_set():
            return _skipped_result(company)
        with fast_lock:
            if fast[0] is None:
                fast[0] = company["name_raw"]
                is_fast = True
            else:
                is_fast = False
        if is_fast:
            result = _ok_result(company)  # completes immediately -> drain check fires
            with completed_lock:
                completed.add(company["name_raw"])
            return result
        gate.wait(timeout=THREAD_SYNC_HANG_TIMEOUT_S)
        result = _ok_result(company)
        with completed_lock:
            completed.add(company["name_raw"])
        return result

    summary = _base_summary()
    all_new_job_keys: list[str] = []
    box: dict = {}
    with patch.object(_run_mod, "_scan_one_company_worker", side_effect=fake_worker):
        t = _drive_scan(
            migrated_db_path, summary, box, concurrency=2, all_new_job_keys=all_new_job_keys
        )
        # Deterministic ordering: the fast company's result reaches the
        # drain loop, the drain-loop deadline check fires, and ONLY once the
        # scan has flipped its abort event do we release the two gated
        # workers. The three remaining queued companies are then picked up
        # with abort already set and must no-op.
        assert abort_set_observed.wait(timeout=THREAD_SYNC_HANG_TIMEOUT_S), (
            f"scan never flipped its abort event {_scan_state_diag(t, box, abort_set=abort_set_observed, gate=gate)}"
        )
        gate.set()
        t.join(timeout=THREAD_SYNC_HANG_TIMEOUT_S)

    assert not t.is_alive(), (
        "_run_ats_api_scan DEADLOCKED: the deadline tripped mid-drain with "
        "queued futures and as_completed() never terminated"
    )
    assert "exc" not in box, f"scan raised: {box.get('exc')!r}"
    assert summary.get("truncated") is True
    # The fast company plus the two already-running gated workers are always
    # merged (issue #1130: a started worker is always drained). But whether
    # one of the three still-queued companies also gets merged is a genuine
    # race between two threads, not a bug: after the fast company's worker
    # returns, its now-free pool thread races the main thread's
    # `abort_scan.set()` (a few lines below the drain-loop deadline check)
    # for the pickup of the next queued company. If the pool thread dequeues
    # and the worker's abort-check runs before `abort_scan.set()` lands, that
    # company scans for real and companies_scanned == 3; if
    # `abort_scan.set()` wins, the worker no-ops at entry and
    # companies_scanned == 2. Both are correct post-fix behavior — the
    # invariant under test is termination and truncation, not this exact
    # count, so accept either outcome.
    assert summary["companies_scanned"] in (2, 3)
    # Issue #1130 identity check (issue #1668 nit 2): the count relaxation
    # above is necessary but not sufficient — it catches a dropped started
    # worker only probabilistically. This per-company-name assertion catches
    # it deterministically: every company whose worker ran to completion
    # (tracked in ``completed``) must have its job key (the company name
    # planted by ``_ok_result``) present in ``all_new_job_keys``, and
    # ``companies_scanned`` must equal the number of completed workers.
    assert summary["companies_scanned"] == len(completed), (
        f"companies_scanned={summary['companies_scanned']} but "
        f"{len(completed)} workers ran to completion ({completed!r})"
    )
    for name in completed:
        assert name in all_new_job_keys, (
            f"company {name!r} ran to completion but its result was not merged "
            f"(all_new_job_keys={all_new_job_keys!r})"
        )


def test_concurrent_deadline_during_submit_truncates_not_deadlocks(migrated_db_path, monkeypatch):
    """Deadline trips in the SUBMIT loop with futures already queued: the
    other pre-fix shutdown site, cancelling before as_completed() snapshots."""
    _insert_test_companies(migrated_db_path, count=6)

    # Both the pool size and the gated-ticker's needed_starts below must
    # agree on how many workers are expected running when the deadline
    # trips; a single named constant keeps an under-count (which would
    # silently weaken the gate) or an over-count (which hangs to the
    # ceiling) from drifting apart at the two call sites.
    CONCURRENCY = 2

    gate = threading.Event()
    # started_gate for _GatedDeadlineTicker below: released by a worker only
    # after it has passed its own abort check, i.e. once it is genuinely
    # running (not a no-op). Consumed entirely inside the ticker now — the
    # test body no longer performs its own acquire()s racing the deadline
    # clock (issue #1984; see _GatedDeadlineTicker's docstring).
    entered = threading.Semaphore(0)
    # The property this test exists to prove is "deadline trips with a
    # future already queued behind running workers" (the pre-fix deadlock
    # site: a queued future's cancellation leaves it CANCELLED, and
    # as_completed() never counts that as done). Recording every no-op
    # (abort-branch) return lets the assertions below pin that premise
    # instead of merely inferring it from companies_scanned.
    skipped: list[str] = []

    def fake_worker(
        company, _db, _tt, _te, _wmp, abort, _config=None, _run_id=None, *_extra, **_kw
    ):
        if abort is not None and abort.is_set():
            skipped.append(company["name_raw"])
            return _skipped_result(company)
        entered.release()
        gate.wait(timeout=THREAD_SYNC_HANG_TIMEOUT_S)
        return _ok_result(company)

    # Reads 1-3 are pre-deadline (companies 0-2 submitted); read 4 is the
    # submit loop's trip check ahead of company 3 and must not resolve to
    # "past deadline" until companies 0 and 1's workers have demonstrably
    # started — see _GatedDeadlineTicker's docstring for why a fixed call
    # count raced the OS thread scheduler under load (issue #1984). Once it
    # trips, the submit loop breaks with one future (company 2) queued
    # behind two already-running workers (companies 0 and 1).
    monkeypatch.setattr(
        time,
        "monotonic",
        _GatedDeadlineTicker([0.0, 0.0, 0.0], 1_000_000.0, entered, needed_starts=CONCURRENCY),
    )
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    abort_set_observed = threading.Event()
    monkeypatch.setattr(_run_mod, "threading", _abort_observer(abort_set_observed))

    summary = _base_summary()
    all_new_job_keys: list[str] = []
    box: dict = {}
    with patch.object(_run_mod, "_scan_one_company_worker", side_effect=fake_worker):
        t = _drive_scan(
            migrated_db_path,
            summary,
            box,
            concurrency=CONCURRENCY,
            all_new_job_keys=all_new_job_keys,
        )
        # By the time the submit loop can observe "past deadline" at all,
        # the gated ticker above has already confirmed both pool workers
        # started, so this wait resolves promptly in the healthy case and
        # remains a hang detector (not a speed assertion) in the broken one.
        assert abort_set_observed.wait(timeout=THREAD_SYNC_HANG_TIMEOUT_S), (
            f"scan never flipped its abort event {_scan_state_diag(t, box, abort_set=abort_set_observed, gate=gate)}"
        )
        gate.set()
        t.join(timeout=THREAD_SYNC_HANG_TIMEOUT_S)

    assert not t.is_alive(), (
        "_run_ats_api_scan DEADLOCKED at the submit-loop truncation site with a queued future"
    )
    assert "exc" not in box, f"scan raised: {box.get('exc')!r}"
    assert summary.get("truncated") is True
    # Companies 0 and 1 were running and are merged; company 2 was queued
    # behind the deadline and must be skipped; companies 3-5 never submitted.
    assert summary["companies_scanned"] == 2
    # Pin the premise the test exists to cover (docstring: "with futures
    # already queued"): exactly one worker (company 2) must have taken the
    # abort no-op branch. A degenerate run where the deadline trips one read
    # early -- two futures submitted, none queued -- would still satisfy
    # every assertion above while silently dropping the pre-fix deadlock
    # scenario (a queued future's CANCELLED state that as_completed() never
    # resolves) from coverage.
    assert len(skipped) == 1, f"expected exactly one queued-behind-deadline no-op, got {skipped!r}"


def test_concurrent_deadline_mid_drain_except_branch_truncates_not_deadlocks(
    migrated_db_path, monkeypatch
):
    """Deadline trips inside the drain loop's EXCEPT branch (the third
    former shutdown(wait=False, cancel_futures=True) site): a worker raises
    an ordinary exception, and the deadline check that runs while handling
    that exception is the one that must trip the abort — not the success
    branch's check. Pre-fix, reinstating the mid-loop shutdown here leaves
    the same CANCELLED-future deadlock as the other two sites."""
    _insert_test_companies(migrated_db_path, count=6)

    # Six pre-deadline reads cover the submit loop's per-company checks, so
    # all six companies are queued; the seventh read is inside the EXCEPT
    # branch's own deadline check (the fast company's worker raises and is
    # the first future drained), so the trip can only come from that branch.
    monkeypatch.setattr(time, "monotonic", _Ticker([0.0] * 6 + [1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    abort_set_observed = threading.Event()
    monkeypatch.setattr(_run_mod, "threading", _abort_observer(abort_set_observed))

    gate = threading.Event()
    # Derive the "fast" company from the scan's actual iteration order rather
    # than hardcoding "Company0" (issue #1668 nit 3): the query's ORDER BY
    # leaves all test companies tied on NULL last_scanned_at, so SQLite's
    # row-order among them is an implementation detail, not a guarantee.
    fast: list[str | None] = [None]
    fast_lock = threading.Lock()

    def fake_worker(
        company, _db, _tt, _te, _wmp, abort, _config=None, _run_id=None, *_extra, **_kw
    ):
        if abort is not None and abort.is_set():
            return _skipped_result(company)
        with fast_lock:
            if fast[0] is None:
                fast[0] = company["name_raw"]
                is_fast = True
            else:
                is_fast = False
        if is_fast:
            raise RuntimeError("simulated worker failure")  # completes immediately
        gate.wait(timeout=THREAD_SYNC_HANG_TIMEOUT_S)
        return _ok_result(company)

    summary = _base_summary()
    all_new_job_keys: list[str] = []
    box: dict = {}
    with patch.object(_run_mod, "_scan_one_company_worker", side_effect=fake_worker):
        t = _drive_scan(
            migrated_db_path, summary, box, concurrency=2, all_new_job_keys=all_new_job_keys
        )
        # Deterministic ordering: the fast company's exception reaches the
        # drain loop's except branch first, that branch's own deadline check
        # fires, and ONLY once the scan has flipped its abort event do we
        # release the gated workers. Whichever queued companies get picked
        # up before/after abort_scan.set() either no-op or scan for real —
        # gate.wait() on an already-set Event returns immediately, so
        # neither ordering can deadlock.
        assert abort_set_observed.wait(timeout=THREAD_SYNC_HANG_TIMEOUT_S), (
            f"scan never flipped its abort event {_scan_state_diag(t, box, abort_set=abort_set_observed, gate=gate)}"
        )
        gate.set()
        t.join(timeout=THREAD_SYNC_HANG_TIMEOUT_S)

    assert not t.is_alive(), (
        "_run_ats_api_scan DEADLOCKED: the deadline tripped in the drain loop's "
        "except branch with queued futures and as_completed() never terminated"
    )
    assert "exc" not in box, f"scan raised: {box.get('exc')!r}"
    assert summary.get("truncated") is True
    # The except branch records the failure into summary["errors"] (see
    # _run.py's drain loop: `summary["errors"].append(f"{company['name_raw']}: {exc}")`)
    # rather than raising it out of _run_ats_api_scan. The fast company's
    # name is derived from the scan's actual iteration order (nit 3), so
    # build the expected error string from ``fast[0]`` instead of hardcoding
    # "Company0".
    assert fast[0] is not None, "no worker ever ran"
    assert f"{fast[0]}: simulated worker failure" in summary["errors"]


def test_worker_abort_set_skips_before_any_work(migrated_db_path):
    """The real worker's entry check: with abort already set it returns a
    deadline_skipped no-op without opening a connection or scanning."""
    companies = _insert_test_companies(migrated_db_path, count=1)

    from jobcannon.engine.db_helpers import standalone_connection

    with standalone_connection(migrated_db_path) as conn:
        row = conn.execute(
            "SELECT * FROM companies WHERE id = ?", (companies[0]["id"],)
        ).fetchone()

    abort = threading.Event()
    abort.set()

    def _fail_if_called(*_a, **_k):
        raise AssertionError("worker touched the DB/scan path despite abort being set")

    with (
        patch.object(_run_mod, "standalone_connection", side_effect=_fail_if_called),
        patch.object(_run_mod, "run_platform_scan", side_effect=_fail_if_called),
    ):
        result = _scan_one_company_worker(row, migrated_db_path, ["Engineer"], [], None, abort)

    assert result.deadline_skipped is True
    assert result.error is None
    assert result.jobs_discovered == 0
    assert result.jobs_new == []

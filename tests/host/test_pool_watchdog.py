"""Pool watchdog: bounded probe + wedged-pool recycle (2026-08-26 incident).

The incident's terminal state was a WEDGED psycopg_pool: one established
connection went dark while its TCP flow kept ACKing, the pool's untimed
reset/check round-trips on it wedged all three background workers, and the
wedged pool never attempted another connect — even though a fresh connect
succeeded on every instance restart. These tests pin the self-healing
contract: one wall-clock-bounded probe (probe_pool, shared with /healthz)
and a supervisor loop that swaps in a freshly built pool after K
consecutive probe failures, rate-limited against recycle churn.

Construction-level only — fakes, tiny intervals, no live Postgres.
"""

import contextlib
import threading
import time

from psycopg_pool import ConnectionPool

from jobcannon.db import pool as pool_mod

_DSN = "postgresql://u:p@192.0.2.9/db"  # IP literal: no resolution path


class _FakeConn:
    def execute(self, sql):
        assert sql == "SELECT 1"


class _FakePool:
    def __init__(self, fail: bool = False, hang_s: float = 0.0):
        self.fail = fail
        self.hang_s = hang_s
        self.closed_with: list = []

    @contextlib.contextmanager
    def connection(self, timeout=None):
        if self.hang_s:
            # Silent-blackhole stand-in: hangs, no exception.
            time.sleep(self.hang_s)
        if self.fail:
            raise RuntimeError("acquire failed after bounded wait")
        yield _FakeConn()

    def get_stats(self):
        return {"pool_size": 1, "connections_num": 1}

    def close(self, timeout=None):
        self.closed_with.append(timeout)


def _make_built_cls(*, fail: bool = False):
    """Fresh ConnectionPool stand-in per test — no shared `built` list."""

    class Built(_FakePool):
        check_connection = ConnectionPool.check_connection
        built: list = []

        def __init__(self, conninfo: str, **kwargs):
            super().__init__(fail=fail)
            self.conninfo = conninfo
            self.kwargs = kwargs
            self.opened = False
            Built.built.append(self)

        def open(self):
            self.opened = True

    return Built


def test_probe_pool_ok(monkeypatch):
    monkeypatch.setattr(pool_mod, "_pool", _FakePool())
    assert pool_mod.probe_pool() is None


def test_probe_pool_failure_carries_exception_detail(monkeypatch):
    monkeypatch.setattr(pool_mod, "_pool", _FakePool(fail=True))
    detail = pool_mod.probe_pool()
    assert detail is not None
    assert detail.startswith("RuntimeError:")


def test_probe_pool_hang_is_wall_clock_bounded(monkeypatch):
    monkeypatch.setattr(pool_mod, "_pool", _FakePool(hang_s=5.0))
    start = time.monotonic()
    detail = pool_mod.probe_pool(acquire_timeout=0.05, wall_timeout=0.1)
    elapsed = time.monotonic() - start
    assert detail is not None
    assert "did not complete" in detail
    assert elapsed < 2.0  # generous CI slack; the hang itself is 5 s


def test_watchdog_interval_env_knob(monkeypatch):
    monkeypatch.delenv("JC_POOL_WATCHDOG_S", raising=False)
    assert pool_mod._watchdog_interval_s() == 15.0
    monkeypatch.setenv("JC_POOL_WATCHDOG_S", "0.25")
    assert pool_mod._watchdog_interval_s() == 0.25
    monkeypatch.setenv("JC_POOL_WATCHDOG_S", "garbage")
    assert pool_mod._watchdog_interval_s() == 15.0


def test_recycle_swaps_in_fresh_pool_and_closes_old(monkeypatch):
    built_cls = _make_built_cls()
    old = _FakePool(fail=True)
    monkeypatch.setattr(pool_mod, "ConnectionPool", built_cls)
    monkeypatch.setattr(pool_mod, "_pool", old)
    monkeypatch.setattr(pool_mod, "_pool_args", (_DSN, 1, 10))
    pool_mod._recycle_pool("RuntimeError: acquire failed")
    assert len(built_cls.built) == 1
    new = built_cls.built[0]
    assert pool_mod._pool is new
    assert new.opened is True
    # Old pool closed with a bounded worker wait, AFTER the swap — a wedged
    # close must never block the fresh pool from serving.
    assert old.closed_with == [5.0]


def test_recycle_rebuild_failure_keeps_existing_pool(monkeypatch):
    class Exploding:
        check_connection = ConnectionPool.check_connection

        def __init__(self, conninfo: str, **kwargs):
            raise RuntimeError("resolver down")

    old = _FakePool(fail=True)
    monkeypatch.setattr(pool_mod, "ConnectionPool", Exploding)
    monkeypatch.setattr(pool_mod, "_pool", old)
    monkeypatch.setattr(pool_mod, "_pool_args", (_DSN, 1, 10))
    pool_mod._recycle_pool("detail")
    assert pool_mod._pool is old  # wedged beats gone
    assert old.closed_with == []


def test_recycle_noops_without_pool_or_args(monkeypatch):
    built_cls = _make_built_cls()
    monkeypatch.setattr(pool_mod, "ConnectionPool", built_cls)
    monkeypatch.setattr(pool_mod, "_pool", None)
    monkeypatch.setattr(pool_mod, "_pool_args", (_DSN, 1, 10))
    pool_mod._recycle_pool("detail")
    assert built_cls.built == []

    monkeypatch.setattr(pool_mod, "_pool", _FakePool())
    monkeypatch.setattr(pool_mod, "_pool_args", None)
    pool_mod._recycle_pool("detail")
    assert built_cls.built == []


def test_watchdog_recycles_after_consecutive_failures_then_rate_limits(monkeypatch):
    # Replacement pools fail too, so failures keep accruing after the
    # recycle — the min-recycle interval must hold the line at one rebuild.
    built_cls = _make_built_cls(fail=True)
    wedged = _FakePool(fail=True)
    monkeypatch.setenv("JC_POOL_WATCHDOG_S", "0.02")
    monkeypatch.setattr(pool_mod, "ConnectionPool", built_cls)
    monkeypatch.setattr(pool_mod, "_pool", wedged)
    monkeypatch.setattr(pool_mod, "_pool_args", (_DSN, 1, 10))
    loop = threading.Thread(target=pool_mod._watchdog_loop, daemon=True)
    loop.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not built_cls.built:
            time.sleep(0.01)
        assert len(built_cls.built) == 1  # K failures -> exactly one recycle
        assert wedged.closed_with == [5.0]
        time.sleep(0.2)  # many more failing ticks
        assert len(built_cls.built) == 1  # rate limit held
    finally:
        pool_mod._pool = None  # loop exit signal
        loop.join(2.0)
    assert not loop.is_alive()


def test_watchdog_loop_exits_when_pool_closed(monkeypatch):
    monkeypatch.setenv("JC_POOL_WATCHDOG_S", "0.02")
    monkeypatch.setattr(pool_mod, "_pool", None)
    loop = threading.Thread(target=pool_mod._watchdog_loop, daemon=True)
    loop.start()
    loop.join(2.0)
    assert not loop.is_alive()


def test_watchdog_disabled_by_zero_interval(monkeypatch):
    monkeypatch.setenv("JC_POOL_WATCHDOG_S", "0")
    monkeypatch.setattr(pool_mod, "_watchdog_thread", None)
    pool_mod._ensure_watchdog()
    assert pool_mod._watchdog_thread is None


def test_open_pool_stores_rebuild_args_and_starts_watchdog(monkeypatch):
    built_cls = _make_built_cls()
    monkeypatch.setenv("JC_POOL_WATCHDOG_S", "0.05")
    monkeypatch.setattr(pool_mod, "ConnectionPool", built_cls)
    monkeypatch.setattr(pool_mod, "_pool", None)
    monkeypatch.setattr(pool_mod, "_pool_args", None)
    monkeypatch.setattr(pool_mod, "_watchdog_thread", None)
    pool_mod.open_pool(_DSN, min_size=2, max_size=4)
    try:
        assert pool_mod._pool_args == (_DSN, 2, 4)
        assert pool_mod._watchdog_thread is not None
        assert pool_mod._watchdog_thread.is_alive()
    finally:
        # Healthy fake keeps the loop's probes green while alive; nulling
        # the pool makes the loop exit at its next 0.05 s tick.
        pool_mod._pool = None

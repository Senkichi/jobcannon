"""Fork safety: a forked child must abandon the inherited pool and build its
own (2026-08-26 root cause: the web app is built before gunicorn forks, so
workers inherit a pool whose background threads don't exist in the child and
whose connection socket is shared across processes).

_reinit_after_fork is exercised directly — os.register_at_fork doesn't exist
on Windows and actually forking is a POSIX-only concern; the hook body is
plain code either way. Construction-level only: fakes, no live Postgres.
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


class _InheritedPool:
    """Stand-in for the parent's pool as seen by a forked child."""

    def __init__(self):
        self.closed_with: list = []

    @contextlib.contextmanager
    def connection(self, timeout=None):
        yield _FakeConn()

    def close(self, timeout=None):
        self.closed_with.append(timeout)


def _make_built_cls():
    class Built(_InheritedPool):
        check_connection = ConnectionPool.check_connection
        built: list = []

        def __init__(self, conninfo: str, **kwargs):
            super().__init__()
            self.conninfo = conninfo
            self.kwargs = kwargs
            self.opened = False
            Built.built.append(self)

        def open(self):
            self.opened = True

    return Built


def _isolate(monkeypatch, *, pool, args):
    monkeypatch.setattr(pool_mod, "_pool_lock", threading.Lock())
    monkeypatch.setattr(pool_mod, "_pool", pool)
    monkeypatch.setattr(pool_mod, "_pool_args", args)
    monkeypatch.setattr(pool_mod, "_watchdog_thread", None)
    monkeypatch.setattr(pool_mod, "_orphaned_prefork_pools", [])


def test_reinit_abandons_inherited_pool_and_rebuilds(monkeypatch):
    built_cls = _make_built_cls()
    inherited = _InheritedPool()
    monkeypatch.setenv("JC_POOL_WATCHDOG_S", "0.05")
    monkeypatch.setattr(pool_mod, "ConnectionPool", built_cls)
    _isolate(monkeypatch, pool=inherited, args=(_DSN, 2, 8))
    old_lock = pool_mod._pool_lock

    pool_mod._reinit_after_fork()
    try:
        # Fresh lock: the parent may have held the old one at the fork instant.
        assert pool_mod._pool_lock is not old_lock
        # Fresh pool built from the stored args, with this process's threads.
        assert len(built_cls.built) == 1
        assert pool_mod._pool is built_cls.built[0]
        assert built_cls.built[0].opened is True
        assert pool_mod._pool_args == (_DSN, 2, 8)
        # The inherited pool is NEVER closed (close() writes a Terminate
        # message into the socket the parent still uses) and never becomes
        # garbage (Connection.__del__ writes into the socket too).
        assert inherited.closed_with == []
        assert inherited in pool_mod._orphaned_prefork_pools
        # The child gets its own watchdog.
        assert pool_mod._watchdog_thread is not None
        assert pool_mod._watchdog_thread.is_alive()
    finally:
        wd = pool_mod._watchdog_thread
        pool_mod._pool = None  # loop exit signal
        if wd is not None:
            wd.join(2.0)


def test_reinit_noops_without_inherited_pool(monkeypatch):
    built_cls = _make_built_cls()
    monkeypatch.setattr(pool_mod, "ConnectionPool", built_cls)
    _isolate(monkeypatch, pool=None, args=(_DSN, 1, 10))

    pool_mod._reinit_after_fork()

    assert built_cls.built == []
    assert pool_mod._pool is None
    assert pool_mod._orphaned_prefork_pools == []


def test_reinit_rebuild_failure_leaves_child_poolless_not_dark(monkeypatch):
    class Exploding:
        check_connection = ConnectionPool.check_connection

        def __init__(self, conninfo: str, **kwargs):
            raise RuntimeError("resolver down in child")

    inherited = _InheritedPool()
    monkeypatch.setattr(pool_mod, "ConnectionPool", Exploding)
    _isolate(monkeypatch, pool=inherited, args=(_DSN, 1, 10))

    pool_mod._reinit_after_fork()  # must not raise: it runs inside fork machinery

    # Fail visible: no pool means get_pool() raises and /healthz 503s,
    # instead of the child silently serving through a broken inherited pool.
    assert pool_mod._pool is None
    assert inherited.closed_with == []
    assert inherited in pool_mod._orphaned_prefork_pools


def test_reinit_then_watchdog_recycle_uses_child_machinery(monkeypatch):
    # End-to-end sanity for the incident shape: child rebuilds after fork,
    # the rebuilt pool wedges, and the CHILD's own watchdog recycles it —
    # the machinery the pre-fix children never had.
    class WedgedBuilt(_InheritedPool):
        check_connection = ConnectionPool.check_connection
        built: list = []

        def __init__(self, conninfo: str, **kwargs):
            super().__init__()
            WedgedBuilt.built.append(self)

        def open(self):
            pass

        @contextlib.contextmanager
        def connection(self, timeout=None):
            raise RuntimeError("acquire failed after bounded wait")
            yield  # pragma: no cover

        def get_stats(self):
            return {"connections_num": 1}

    inherited = _InheritedPool()
    monkeypatch.setenv("JC_POOL_WATCHDOG_S", "0.02")
    monkeypatch.setattr(pool_mod, "ConnectionPool", WedgedBuilt)
    _isolate(monkeypatch, pool=inherited, args=(_DSN, 1, 10))

    pool_mod._reinit_after_fork()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(WedgedBuilt.built) < 2:
            time.sleep(0.01)
        # Build 1 = the post-fork rebuild; build 2 = the child watchdog's
        # recycle after K consecutive probe failures.
        assert len(WedgedBuilt.built) >= 2
        assert WedgedBuilt.built[0].closed_with == [5.0]
        assert inherited.closed_with == []
    finally:
        wd = pool_mod._watchdog_thread
        pool_mod._pool = None
        if wd is not None:
            wd.join(2.0)

"""DB-aware /healthz contract (2026-08-26 incident).

A static /healthz kept a web instance whose DB path had died post-boot in
the platform's rotation indefinitely. The route now runs a bounded pooled
probe when (and only when) a pool is open: unreachable DB -> 503 so the
platform's health checks replace the instance. No pool -> the static OK the
DB-free dev/test contract has always promised.
"""

import contextlib
import time

import pytest

from jobcannon.db import pool as pool_mod
from jobcannon.web import create_app


@pytest.fixture
def client():
    app = create_app({"TESTING": True})
    return app.test_client()


class _FakeConn:
    def execute(self, sql):
        assert sql == "SELECT 1"


class _FakePool:
    def __init__(self, fail: bool = False, hang_s: float = 0.0):
        self.fail = fail
        self.hang_s = hang_s
        self.probe_timeouts: list[float | None] = []

    @contextlib.contextmanager
    def connection(self, timeout=None):
        self.probe_timeouts.append(timeout)
        if self.hang_s:
            # Silent-blackhole stand-in: the checkout liveness probe /
            # query round-trip on a dead-but-established socket hangs with
            # no exception — the mode psycopg_pool's own timeout= (which
            # bounds only acquisition) cannot bound.
            time.sleep(self.hang_s)
        if self.fail:
            raise RuntimeError("no connection available after bounded wait")
        yield _FakeConn()

    def get_stats(self):
        return {"pool_size": 0, "connections_errors": 3}


def test_healthz_without_pool_is_static_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "db": "not-configured"}


def test_healthz_probes_open_pool_and_reports_ok(client, monkeypatch):
    fake = _FakePool()
    monkeypatch.setattr(pool_mod, "_pool", fake)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "db": "ok"}
    # Acquisition deadline forwarded (2.0, inside the route's 2.5 s
    # wall-clock thread bound — which the hang test below exercises for
    # real; this assertion covers only the argument wiring).
    assert fake.probe_timeouts == [2.0]


def test_healthz_unreachable_db_is_503(client, monkeypatch):
    monkeypatch.setattr(pool_mod, "_pool", _FakePool(fail=True))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.get_json() == {"status": "unhealthy", "db": "unreachable"}


def test_healthz_hung_probe_is_wall_clock_bounded(client, monkeypatch):
    # The 2026-08-26 blackhole mode: the probe neither fails nor returns.
    # psycopg_pool's timeout= cannot bound this (it covers acquisition
    # only); the route's daemon-thread join must. A hang far longer than
    # the bound has to come back as a 503 in ~2.5 s, not ride the hang.
    monkeypatch.setattr(pool_mod, "_pool", _FakePool(hang_s=20.0))
    start = time.monotonic()
    resp = client.get("/healthz")
    elapsed = time.monotonic() - start
    assert resp.status_code == 503
    assert resp.get_json() == {"status": "unhealthy", "db": "unreachable"}
    assert elapsed < 10.0  # generous CI slack; the hang itself is 20 s

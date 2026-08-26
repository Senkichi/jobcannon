"""DB-aware /healthz contract (2026-08-26 incident).

A static /healthz kept a web instance whose DB path had died post-boot in
the platform's rotation indefinitely. The route now runs a bounded pooled
probe when (and only when) a pool is open: unreachable DB -> 503 so the
platform's health checks replace the instance. No pool -> the static OK the
DB-free dev/test contract has always promised.
"""

import contextlib

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
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.probe_timeouts: list[float | None] = []

    @contextlib.contextmanager
    def connection(self, timeout=None):
        self.probe_timeouts.append(timeout)
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
    # The probe must be bounded: an untimed checkout would ride the pool's
    # full 30 s acquire deadline and time out the health check itself.
    assert fake.probe_timeouts == [2.5]


def test_healthz_unreachable_db_is_503(client, monkeypatch):
    monkeypatch.setattr(pool_mod, "_pool", _FakePool(fail=True))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.get_json() == {"status": "unhealthy", "db": "unreachable"}

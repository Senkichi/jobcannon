"""Pool construction resilience: bounded connect phase + checkout liveness.

Pure construction-level tests — no live Postgres required. The behavioral
counterpart (a real pooled connection surviving checkout/return cycles)
lives in tests/host/test_connection_factory.py behind requires_postgres.
"""

from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

from jobcannon.db import pool as pool_mod
from jobcannon.db.pool import _conninfo_with_defaults


def test_connect_timeout_defaulted_and_dsn_fields_preserved():
    out = conninfo_to_dict(
        _conninfo_with_defaults("postgresql://alice:s3cret@db.example.internal:5432/jobcannon")
    )
    assert out["connect_timeout"] == "10"
    assert out["host"] == "db.example.internal"
    assert out["port"] == "5432"
    assert out["user"] == "alice"
    assert out["password"] == "s3cret"
    assert out["dbname"] == "jobcannon"


def test_explicit_connect_timeout_in_dsn_wins():
    out = conninfo_to_dict(_conninfo_with_defaults("postgresql://u:p@h/db?connect_timeout=3"))
    assert out["connect_timeout"] == "3"


def test_open_pool_wires_bounded_connect_and_liveness_check(monkeypatch):
    captured: dict = {}

    class FakePool:
        # open_pool references ConnectionPool.check_connection on the class it
        # constructs; mirror the real attribute so that lookup stays honest.
        check_connection = ConnectionPool.check_connection

        def __init__(self, conninfo: str, **kwargs):
            captured["conninfo"] = conninfo
            captured.update(kwargs)

        def open(self) -> None:
            captured["opened"] = True

    monkeypatch.setattr(pool_mod, "ConnectionPool", FakePool)
    monkeypatch.setattr(pool_mod, "_pool", None)
    pool_mod.open_pool("postgresql://u:p@h/db")
    try:
        assert conninfo_to_dict(captured["conninfo"])["connect_timeout"] == "10"
        assert captured["check"] is ConnectionPool.check_connection
        assert captured["opened"] is True
    finally:
        pool_mod._pool = None

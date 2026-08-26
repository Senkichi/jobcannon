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


def _ai(family, ip):
    # getaddrinfo tuple shape: (family, type, proto, canonname, sockaddr)
    import socket as _s

    addr = (ip, 0) if family == _s.AF_INET else (ip, 0, 0, 0)
    return (family, _s.SOCK_STREAM, _s.IPPROTO_TCP, "", addr)


def test_hostaddr_pinned_from_boot_resolution(monkeypatch):
    import socket as _s

    monkeypatch.setattr(
        pool_mod.socket, "getaddrinfo", lambda *a, **k: [_ai(_s.AF_INET, "203.0.113.7")]
    )
    out = conninfo_to_dict(_conninfo_with_defaults("postgresql://u:p@db.internal/job"))
    assert out["hostaddr"] == "203.0.113.7"
    assert out["host"] == "db.internal"  # kept for TLS/auth


def test_hostaddr_prefers_ipv4_over_ipv6(monkeypatch):
    import socket as _s

    monkeypatch.setattr(
        pool_mod.socket,
        "getaddrinfo",
        lambda *a, **k: [_ai(_s.AF_INET6, "2001:db8::7"), _ai(_s.AF_INET, "203.0.113.9")],
    )
    out = conninfo_to_dict(_conninfo_with_defaults("postgresql://u:p@db.internal/job"))
    assert out["hostaddr"] == "203.0.113.9"


def test_hostaddr_not_pinned_for_ip_literal_host(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("getaddrinfo must not be called for an IP literal")

    monkeypatch.setattr(pool_mod.socket, "getaddrinfo", boom)
    out = conninfo_to_dict(_conninfo_with_defaults("postgresql://u:p@192.0.2.5/job"))
    assert "hostaddr" not in out


def test_hostaddr_respects_explicit_hostaddr_and_multihost(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("getaddrinfo must not be called")

    monkeypatch.setattr(pool_mod.socket, "getaddrinfo", boom)
    explicit = conninfo_to_dict(
        _conninfo_with_defaults("host=db.internal hostaddr=198.51.100.2 user=u dbname=job")
    )
    assert explicit["hostaddr"] == "198.51.100.2"
    multi = conninfo_to_dict(
        _conninfo_with_defaults("host=a.internal,b.internal user=u dbname=job")
    )
    assert "hostaddr" not in multi


def test_hostaddr_resolution_failure_falls_back_to_name(monkeypatch):
    import socket as _s

    def fail(*a, **k):
        raise _s.gaierror("resolver down at boot")

    monkeypatch.setattr(pool_mod.socket, "getaddrinfo", fail)
    out = conninfo_to_dict(_conninfo_with_defaults("postgresql://u:p@db.internal/job"))
    assert "hostaddr" not in out
    assert out["host"] == "db.internal"

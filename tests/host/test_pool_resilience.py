"""Pool construction resilience: bounded connect phase + checkout liveness.

Pure construction-level tests — no live Postgres required. The behavioral
counterpart (a real pooled connection surviving checkout/return cycles)
lives in tests/host/test_connection_factory.py behind requires_postgres.
"""

from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import ConnectionPool

from jobcannon.db import pool as pool_mod
from jobcannon.db.pool import _conninfo_with_defaults


def test_connect_timeout_defaulted_and_dsn_fields_preserved(monkeypatch):
    monkeypatch.setattr(
        pool_mod.socket,
        "getaddrinfo",
        lambda *a, **k: [_ai(pool_mod.socket.AF_INET, "203.0.113.1")],
    )
    out = conninfo_to_dict(
        _conninfo_with_defaults("postgresql://alice:s3cret@db.example.internal:5432/jobcannon")
    )
    assert out["connect_timeout"] == "10"
    assert out["host"] == "db.example.internal"
    assert out["port"] == "5432"
    assert out["user"] == "alice"
    assert out["password"] == "s3cret"
    assert out["dbname"] == "jobcannon"


def test_application_name_defaulted_from_service_env(monkeypatch):
    # IP-literal hosts skip resolution, isolating the attribution default.
    monkeypatch.setenv("RENDER_SERVICE_NAME", "jobcannon-web")
    out = conninfo_to_dict(_conninfo_with_defaults("postgresql://u:p@192.0.2.5/db"))
    assert out["application_name"] == "jobcannon-web"

    explicit = conninfo_to_dict(
        _conninfo_with_defaults("postgresql://u:p@192.0.2.5/db?application_name=custom")
    )
    assert explicit["application_name"] == "custom"


def test_dead_socket_tcp_bounds_defaulted():
    # connect_timeout bounds establishment only; these bound I/O on an
    # ESTABLISHED socket whose peer silently vanished (2026-08-26 mode).
    out = conninfo_to_dict(_conninfo_with_defaults("postgresql://u:p@192.0.2.5/db"))
    assert out["tcp_user_timeout"] == "30000"
    assert out["keepalives"] == "1"
    assert out["keepalives_idle"] == "30"
    assert out["keepalives_interval"] == "10"
    assert out["keepalives_count"] == "3"

    explicit = conninfo_to_dict(
        _conninfo_with_defaults("postgresql://u:p@192.0.2.5/db?tcp_user_timeout=5000")
    )
    assert explicit["tcp_user_timeout"] == "5000"


def test_explicit_connect_timeout_in_dsn_wins():
    # IP-literal host: exercises the timeout default without touching resolution.
    out = conninfo_to_dict(
        _conninfo_with_defaults("postgresql://u:p@192.0.2.5/db?connect_timeout=3")
    )
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
    pool_mod.open_pool("postgresql://u:p@192.0.2.9/db")
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


def test_resolution_failure_at_boot_is_fatal(monkeypatch):
    # The original fallback-to-name behavior silently re-admitted the untimed
    # getaddrinfo hang the pin exists to prevent (2026-08-26 production
    # regression) — a failed boot resolution must refuse to open the pool.
    import socket as _s

    import pytest

    calls = {"n": 0}

    def fail(*a, **k):
        calls["n"] += 1
        raise _s.gaierror("resolver down at boot")

    monkeypatch.setattr(pool_mod.socket, "getaddrinfo", fail)
    monkeypatch.setattr(pool_mod, "_RESOLVE_BACKOFF_S", 0)
    with pytest.raises(RuntimeError, match="could not resolve database host"):
        _conninfo_with_defaults("postgresql://u:p@db.internal/job")
    assert calls["n"] == pool_mod._RESOLVE_ATTEMPTS  # it retried before giving up


def test_hung_resolver_is_bounded_and_fatal(monkeypatch):
    # A resolver that never returns (the incident's actual shape — no
    # exception, just a hang) must be converted to a bounded failure, not
    # inherited as an unbounded boot hang.
    import time as _time

    import pytest

    def hang(*a, **k):
        _time.sleep(2)
        return []

    monkeypatch.setattr(pool_mod.socket, "getaddrinfo", hang)
    monkeypatch.setattr(pool_mod, "_RESOLVE_ATTEMPTS", 1)
    monkeypatch.setattr(pool_mod, "_RESOLVE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(pool_mod, "_RESOLVE_BACKOFF_S", 0)
    start = _time.monotonic()
    with pytest.raises(RuntimeError, match="could not resolve database host"):
        _conninfo_with_defaults("postgresql://u:p@db.internal/job")
    assert _time.monotonic() - start < 1.0  # bounded, nowhere near the 2 s hang

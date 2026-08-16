"""Spec §8 seam-wiring test: all three host-wiring surfaces are exercised,
pinning the corrected three-seam architecture so 'silently running on
hardcoded defaults' cannot ship."""

import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


@pytest.fixture()
def wired(monkeypatch):
    """Own throwaway database, NOT the shared session-scoped postgres_test_dsn.

    test_seam3 below does a real, durable commit (extraction_health.record ->
    scan_health_log) through the health-recorder seam — proving that seam
    writes are actually visible on live Postgres, which is the point of the
    test. Sharing postgres_test_dsn would leak that row into the same session
    database other test modules' unqualified queries assume is empty (see
    test_scan_services_contract.py's wired_services fixture docstring for the
    empirically-verified failure mode). An isolated throwaway database removes
    the shared state entirely.
    """
    from jobcannon.db.migrate import run_migrations
    from jobcannon.host.config import load_host_config
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    monkeypatch.setenv("JC_SCAN_MEMO_TTL_S", "1234")
    dsn, db_name = create_throwaway_db("jobcannon_wiring")
    try:
        run_migrations(dsn)
        monkeypatch.setenv("DATABASE_URL", dsn)
        init_engine_seams(load_host_config())
        try:
            yield dsn
        finally:
            try:
                teardown_engine_seams()
            except Exception:
                pass  # never let teardown mask the original test outcome
    finally:
        drop_throwaway_db(db_name)


def test_seam1_scan_services_registered(wired):
    from jobcannon.engine import services

    svc = services.get_services()
    assert svc.jd_storage_max_chars == 50_000
    assert svc.prober_extensions is None  # 1B ruling: fail-closed, deliberate


def test_seam2_runtime_config_reaches_engine_readers(wired):
    # A non-default scan_memo_ttl_s must actually reach the engine's reader —
    # THE regression this test exists for (spec §8).
    from jobcannon.engine.runtime_config import get_runtime_config

    assert get_runtime_config()["ats"]["scan_memo_ttl_s"] == 1234


def test_seam3_health_recorder_writes_rows(wired, monkeypatch):
    import psycopg

    from jobcannon.engine import extraction_health
    from jobcannon.host import health_recorder

    # Spy on commit_unless_nested so the second-connection read below isn't
    # the only thing proving durability — psycopg_pool commits on a clean
    # pool.connection() exit regardless, which would mask a missing explicit
    # commit in record_scan_health. Pin that the recorder's explicit commit
    # path actually runs, exactly once, against its own raw connection.
    calls = []
    real_commit_unless_nested = health_recorder.commit_unless_nested

    def _spy(raw):
        calls.append(raw)
        return real_commit_unless_nested(raw)

    monkeypatch.setattr(health_recorder, "commit_unless_nested", _spy)

    # conn=object(): a non-JSON-serializable sentinel standing in for the
    # engine's own live connection (see health_recorder.py's module
    # docstring) — must be popped before the payload is built, never stored.
    extraction_health.record(check="wiring-test", chars=42, conn=object())

    assert len(calls) == 1
    assert isinstance(calls[0], psycopg.Connection)

    with psycopg.connect(wired) as conn:
        row = conn.execute(
            "SELECT payload FROM scan_health_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    payload = row[0]
    assert payload["check"] == "wiring-test"
    assert payload["chars"] == 42
    assert "conn" not in payload


def test_double_init_is_noop(wired):
    # init_engine_seams' open_pool no-ops on an already-open pool (pool.py's
    # documented re-call behavior); the seam setters (set_services /
    # set_config_provider / set_recorder) are plain reassignments either way.
    # Re-entry with the SAME config/DSN must not raise and seams must still
    # work. (The changed-DSN double-init case is unreachable in practice — web
    # and worker are separate processes — and is deliberately not handled.)
    from jobcannon.engine import services
    from jobcannon.engine.runtime_config import get_runtime_config
    from jobcannon.host.config import load_host_config
    from jobcannon.host.wiring import init_engine_seams

    init_engine_seams(load_host_config())

    assert get_runtime_config()["ats"]["scan_memo_ttl_s"] == 1234
    assert services.get_services().jd_storage_max_chars == 50_000


def test_teardown_clears_all_seams(postgres_test_dsn, monkeypatch):
    from jobcannon.db import pool as pool_mod
    from jobcannon.engine import services
    from jobcannon.engine.runtime_config import get_runtime_config
    from jobcannon.host.config import load_host_config
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    monkeypatch.setenv("DATABASE_URL", postgres_test_dsn)
    init_engine_seams(load_host_config())
    teardown_engine_seams()
    assert get_runtime_config() == {}
    with pytest.raises(RuntimeError):
        services.get_services()
    with pytest.raises(RuntimeError):
        pool_mod.get_pool()


def test_seam4_analytics_salt_threaded_from_host_config(postgres_test_dsn, monkeypatch):
    """init_engine_seams must actually thread HostConfig.analytics_pseudonym_salt
    through to posthog_client.pseudonymize — env var set explicitly in this
    test's own body (not inherited from another fixture) so the assertion
    does not depend on fixture execution order."""
    import hashlib
    import hmac

    from jobcannon.host import posthog_client
    from jobcannon.host.config import load_host_config
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    monkeypatch.setenv("DATABASE_URL", postgres_test_dsn)
    monkeypatch.setenv("JC_ANALYTICS_PSEUDONYM_SALT", "wiring-test-salt")
    init_engine_seams(load_host_config())
    try:
        expected = hmac.new(b"wiring-test-salt", b"user_x", hashlib.sha256).hexdigest()
        assert posthog_client.pseudonymize("user_x") == expected
    finally:
        teardown_engine_seams()


def test_seam4_analytics_salt_cleared_on_teardown(postgres_test_dsn, monkeypatch):
    from jobcannon.host import posthog_client
    from jobcannon.host.config import load_host_config
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    monkeypatch.setenv("DATABASE_URL", postgres_test_dsn)
    monkeypatch.setenv("JC_ANALYTICS_PSEUDONYM_SALT", "wiring-test-salt-2")
    init_engine_seams(load_host_config())
    teardown_engine_seams()

    assert posthog_client.pseudonymize("user_x") is None

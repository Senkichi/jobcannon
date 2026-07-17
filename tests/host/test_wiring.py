"""Spec §8 seam-wiring test: all three host-wiring surfaces are exercised,
pinning the corrected three-seam architecture so 'silently running on
hardcoded defaults' cannot ship."""

import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture()
def wired(postgres_test_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_test_dsn)
    monkeypatch.setenv("JC_SCAN_MEMO_TTL_S", "1234")
    from jobcannon.host.config import load_host_config
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    init_engine_seams(load_host_config())
    try:
        yield
    finally:
        teardown_engine_seams()


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


def test_seam3_health_recorder_writes_rows(wired, postgres_test_dsn):
    import psycopg

    from jobcannon.engine import extraction_health

    extraction_health.record(check="wiring-test", chars=42)
    with psycopg.connect(postgres_test_dsn) as conn:
        row = conn.execute(
            "SELECT payload FROM scan_health_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row[0]["check"] == "wiring-test"


def test_teardown_clears_all_seams(postgres_test_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_test_dsn)
    from jobcannon.engine import services
    from jobcannon.engine.runtime_config import get_runtime_config
    from jobcannon.host.config import load_host_config
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    init_engine_seams(load_host_config())
    teardown_engine_seams()
    assert get_runtime_config() == {}
    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        services.get_services()

"""Postgres test fixtures for the host layer.

Strategy: DSN-driven. Integration tests connect through POSTGRES_ADMIN_DSN,
whatever Postgres instance that points at — on CI that's a `services:`
container (`.github/workflows/ci.yml`'s `test` job, `pgvector/pgvector:pg17`,
matching render.yaml's production `postgresMajorVersion`), which the job
wires POSTGRES_ADMIN_DSN to at the job level since the container is that
job's own ephemeral Postgres and there's no persistent runner-level value it
could shadow. Local development points the same variable at any local
Postgres the same way. (Historical note, #212/#160: for a stretch this repo
ran CI on self-hosted Windows runners that owned their own PG18 + pgvector
instance directly, with no service container; that setup is retired.) Each
pytest session (per xdist worker) creates a throwaway database, runs
migrations once, and drops it at exit. Per-test isolation is
transaction-rollback, not per-test databases. With POSTGRES_ADMIN_DSN unset,
every test in tests/host/ SKIPS.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

ADMIN_DSN = os.environ.get("POSTGRES_ADMIN_DSN")

requires_postgres = pytest.mark.skipif(
    not ADMIN_DSN, reason="POSTGRES_ADMIN_DSN not set — no local Postgres available"
)


@pytest.fixture(autouse=True)
def _reset_analytics_pseudonym_salt():
    """jobcannon.host.posthog_client's pseudonymization salt is a module-level
    global — reset it to None (unconfigured, fail-closed) after every test in
    this directory so a salt set by one test can never leak into the next.

    Deliberately reset-ONLY, not defaulted to a fixed value: the fail-closed
    posture (no salt -> no PostHog fan-out) is meant to be the ambient state
    a test gets for free. The handful of tests that assert a PostHog capture
    actually happens opt in explicitly via
    jobcannon.host.posthog_client.set_analytics_salt(...) in their own
    fixture/body, so that opt-in stays visible at the point it matters
    instead of being silently supplied here for the whole directory."""
    from jobcannon.host import posthog_client

    yield
    posthog_client.set_analytics_salt(None)


@pytest.fixture(autouse=True)
def _reset_posthog_admin():
    """jobcannon.host.posthog_admin's personal-API-key/project-id/host/
    logged-once-flag are module-level globals too (issue #135) — same
    leak-across-tests hazard as the salt above, and the same reset-only
    rationale: unconfigured (fail-soft skip) is the free ambient state,
    tests that exercise a configured purge opt in explicitly via
    posthog_admin.configure(...) in their own body. Also resets
    `_logged_unset_once` so the "logs exactly once" assertion in
    tests/host/test_posthog_admin.py never depends on test execution order
    within the file, let alone across files."""
    from jobcannon.host import posthog_admin

    yield
    posthog_admin.configure(personal_api_key=None, project_id=None, host=None)


@pytest.fixture(autouse=True)
def _pool_watchdog_disabled_by_default(monkeypatch):
    """open_pool auto-starts the pool-watchdog daemon thread, which would leak
    a live thread (first probe at t=15s) across every host test that opens a
    pool. Disable it directory-wide; watchdog tests opt back in by setting
    JC_POOL_WATCHDOG_S themselves (their in-test setenv overrides this)."""
    monkeypatch.setenv("JC_POOL_WATCHDOG_S", "0")


def _dsn_for(db_name: str) -> str:
    # Swap ONLY dbname, preserving host/credentials/query-params/keyword-DSN
    # form — naive rpartition("/") string surgery breaks on DSNs that carry
    # query params (?sslmode=...) or use keyword form (host=... dbname=...).
    return make_conninfo(ADMIN_DSN, dbname=db_name)


def create_throwaway_db(prefix: str) -> tuple[str, str]:
    """Create a uniquely-named throwaway database; returns (dsn, db_name)."""
    db_name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{db_name}"')
    return _dsn_for(db_name), db_name


def drop_throwaway_db(db_name: str) -> None:
    """Atomically terminate connections and drop (PG13+ WITH (FORCE)).

    A separate pg_terminate_backend() pass followed by a plain DROP DATABASE
    is a TOCTOU race on a permanent, shared local service: a new connection
    can sneak in between the terminate and the drop. WITH (FORCE) does both
    atomically in one statement.
    """
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def postgres_test_dsn(worker_id: str):
    """Session-scoped throwaway database, xdist-safe via worker_id suffix."""
    if not ADMIN_DSN:
        pytest.skip("POSTGRES_ADMIN_DSN not set")
    dsn, db_name = create_throwaway_db(f"jobcannon_test_{worker_id}")
    try:
        from jobcannon.db.migrate import run_migrations

        run_migrations(dsn)
        yield dsn
    finally:
        drop_throwaway_db(db_name)


@pytest.fixture()
def db_conn(postgres_test_dsn: str):
    """Per-test connection; everything the test writes is rolled back."""
    from psycopg import Rollback

    conn = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    try:
        with conn.transaction() as tx:
            yield conn
            raise Rollback(tx)
    finally:
        conn.close()


@pytest.fixture
def db_conn_pair(postgres_test_dsn):
    """Two INDEPENDENT psycopg connections to the same test DB — real
    cross-session lock contention for SKIP LOCKED tests. Cleanup: rollback
    + close both."""
    a = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    b = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    try:
        yield a, b
    finally:
        for c in (a, b):
            try:
                c.rollback()
                c.close()
            except Exception:
                pass


@pytest.fixture
def seeded_pending_postings(db_conn_pair):
    """One company + 4 postings (distinct dedup_key, non-empty jd_full,
    embedding_model_version NULL) COMMITTED so both connections see them.
    Returns the 4 posting ids. Cleanup DELETEs + commits (the sweeps under
    test commit for real — rollback isolation does not cover this fixture)."""
    a, _ = db_conn_pair
    cid = a.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status) "
        "VALUES ('SweepCo', 'SweepCo', 'jobvite', 'sweepco', 'hit') RETURNING id"
    ).fetchone()["id"]
    ids = [
        a.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company, jd_full) "
            "VALUES (%s, %s, 'Engineer', 'SweepCo', %s) RETURNING id",
            (f"sweep-{i}", cid, f"jd body number {i} with real words"),
        ).fetchone()["id"]
        for i in range(4)
    ]
    a.commit()
    try:
        yield ids
    finally:
        a.rollback()
        a.execute("DELETE FROM postings WHERE company_id = %s", (cid,))
        a.execute("DELETE FROM companies WHERE id = %s", (cid,))
        a.commit()

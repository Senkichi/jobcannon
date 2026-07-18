"""Postgres test fixtures for the host layer.

Strategy (1B spec §8 + Windows self-hosted CI constraint): GitHub `services:`
containers require a Linux runner, so integration tests run against a permanent
local PostgreSQL Windows service, reachable via POSTGRES_ADMIN_DSN. Each pytest
session (per xdist worker) creates a throwaway database, runs migrations once,
and drops it at exit. Per-test isolation is transaction-rollback, not per-test
databases. With POSTGRES_ADMIN_DSN unset, every test in tests/host/ SKIPS.
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

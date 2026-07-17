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
from psycopg.rows import dict_row

ADMIN_DSN = os.environ.get("POSTGRES_ADMIN_DSN")

requires_postgres = pytest.mark.skipif(
    not ADMIN_DSN, reason="POSTGRES_ADMIN_DSN not set — no local Postgres available"
)


def _dsn_for(db_name: str) -> str:
    # Reuse the admin DSN's host/credentials, swapping only the database name.
    base, _, _admin_db = ADMIN_DSN.rpartition("/")
    return f"{base}/{db_name}"


@pytest.fixture(scope="session")
def postgres_test_dsn(worker_id: str):
    """Session-scoped throwaway database, xdist-safe via worker_id suffix."""
    if not ADMIN_DSN:
        pytest.skip("POSTGRES_ADMIN_DSN not set")
    db_name = f"jobcannon_test_{worker_id}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{db_name}"')
    dsn = _dsn_for(db_name)
    try:
        from jobcannon.db.migrate import run_migrations

        run_migrations(dsn)
        yield dsn
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


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

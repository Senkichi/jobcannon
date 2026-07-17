import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


@pytest.fixture()
def opened_pool():
    """Own throwaway database, NOT the shared session-scoped postgres_test_dsn.

    This module's tests do real, durable conn.commit() calls directly
    against the pooled connection (not the rollback-isolated db_conn
    fixture other tests/host/ modules use), so they must not leak committed
    rows ('factory-co', 'sync-co', ...) into the shared session database —
    mirrors tests/host/test_scan_services_contract.py's wired_services
    fixture. run_migrations is inside the try (mirrors
    tests/host/conftest.py's postgres_test_dsn) so a setup failure still
    reaches drop_throwaway_db.
    """
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations

    dsn, db_name = create_throwaway_db("jobcannon_factory")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        yield pool_mod
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def test_zero_arg_call_yields_working_wrapped_connection(opened_pool):
    from jobcannon.db.pool import connection_factory

    with connection_factory() as conn:
        # Engine-style qmark SQL + both row access styles must work.
        conn.execute("INSERT INTO companies (name) VALUES (?)", ("factory-co",))
        conn.commit()
        row = conn.execute(
            "SELECT name, ats_probe_status FROM companies WHERE name = ?", ("factory-co",)
        ).fetchone()
        assert row["name"] == "factory-co"
        assert row[1] == "pending"


def test_synchronous_normal_sets_session_then_resets(opened_pool):
    from jobcannon.db.pool import connection_factory

    with connection_factory(synchronous="NORMAL") as conn:
        val = conn.execute("SHOW synchronous_commit").fetchone()
        assert val[0] == "off"
        # Must SURVIVE a commit (SQLite PRAGMA is connection-scoped; engine
        # call sites run many transactions on one factory connection).
        conn.execute("INSERT INTO companies (name) VALUES (?)", ("sync-co",))
        conn.commit()
        val = conn.execute("SHOW synchronous_commit").fetchone()
        assert val[0] == "off"
    # After return to pool, the next checkout must be back to default.
    with connection_factory() as conn:
        val = conn.execute("SHOW synchronous_commit").fetchone()
        assert val[0] == "on"


def test_executemany_translates_qmarks(opened_pool):
    from jobcannon.db.pool import connection_factory

    with connection_factory() as conn:
        conn.executemany(
            "INSERT INTO companies (name) VALUES (?)",
            [("many-co-1",), ("many-co-2",)],
        )
        conn.commit()
        rows = conn.execute(
            "SELECT name FROM companies WHERE name IN (?, ?)",
            ("many-co-1", "many-co-2"),
        ).fetchall()
    assert {row["name"] for row in rows} == {"many-co-1", "many-co-2"}

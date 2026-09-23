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


def test_with_write_txn_commits_durably_through_facade(opened_pool):
    """with_write_txn unwraps the EngineCompatConnection facade, yields the
    raw psycopg connection, and lands a durable commit visible to a second
    connection."""
    from jobcannon.db.pool import connection_factory, with_write_txn

    with connection_factory() as conn:
        with with_write_txn(conn) as raw:
            assert not hasattr(raw, "raw")  # yields the raw psycopg conn
            raw.execute("INSERT INTO companies (name) VALUES (%s)", ("wtx-co",))
    with connection_factory() as conn2:
        row = conn2.execute("SELECT name FROM companies WHERE name = ?", ("wtx-co",)).fetchone()
    assert row["name"] == "wtx-co"


def test_with_write_txn_rolls_back_on_body_error(opened_pool):
    """Accepts a bare psycopg connection too; a body exception rolls the
    transaction back and commit_unless_nested never runs — the write is
    invisible to a second connection."""
    from jobcannon.db.pool import get_pool, with_write_txn

    with (
        get_pool().connection() as raw,
        pytest.raises(RuntimeError, match="boom"),
    ):
        with with_write_txn(raw) as yielded:
            assert yielded is raw
            yielded.execute("INSERT INTO companies (name) VALUES (%s)", ("wtx-rb",))
            raise RuntimeError("boom")
    with get_pool().connection() as other:
        assert (
            other.execute("SELECT name FROM companies WHERE name = %s", ("wtx-rb",)).fetchone()
            is None
        )


def test_with_write_txn_nested_in_ambient_txn_defers_commit(opened_pool):
    """Inside a caller-owned `raw.transaction()` the helper's commit is a
    no-op (commit_unless_nested sees _num_transactions > 0) — rolling back
    the ambient block erases the write."""
    from psycopg import Rollback

    from jobcannon.db.pool import get_pool, with_write_txn

    with get_pool().connection() as raw:
        with raw.transaction() as tx:
            with with_write_txn(raw) as yielded:
                yielded.execute("INSERT INTO companies (name) VALUES (%s)", ("wtx-nested",))
            raise Rollback(tx)  # swallowed by the ambient transaction's __exit__
    with get_pool().connection() as other:
        assert (
            other.execute("SELECT name FROM companies WHERE name = %s", ("wtx-nested",)).fetchone()
            is None
        )

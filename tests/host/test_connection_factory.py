import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture()
def opened_pool(postgres_test_dsn):
    from jobcannon.db import pool as pool_mod

    pool_mod.open_pool(postgres_test_dsn)
    try:
        yield pool_mod
    finally:
        pool_mod.close_pool()


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

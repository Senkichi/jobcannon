import psycopg
import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def test_watchlists_uniqueness_partial_indexes(db_conn):
    db_conn.execute("INSERT INTO users (id) VALUES ('user_a')")
    db_conn.execute("INSERT INTO companies (name) VALUES ('acme') RETURNING id")
    company_id = db_conn.execute("SELECT id FROM companies WHERE name='acme'").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO watchlists (user_id, company_id) VALUES ('user_a', %s)", (company_id,)
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db_conn.transaction():
            db_conn.execute(
                "INSERT INTO watchlists (user_id, company_id) VALUES ('user_a', %s)",
                (company_id,),
            )


def test_posted_date_pairing_check(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('acme2')")
    cid = db_conn.execute("SELECT id FROM companies WHERE name='acme2'").fetchone()["id"]
    with pytest.raises(psycopg.errors.CheckViolation):
        with db_conn.transaction():
            db_conn.execute(
                "INSERT INTO postings (dedup_key, company_id, title, company, posted_date) "
                "VALUES ('k|t', %s, 't', 'acme2', '2026-01-01')",  # date without precision
                (cid,),
            )


def test_is_remote_tristate_representable(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('acme3')")
    cid = db_conn.execute("SELECT id FROM companies WHERE name='acme3'").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, is_remote) "
        "VALUES ('k3|t', %s, 't', 'acme3', NULL)",
        (cid,),
    )
    row = db_conn.execute("SELECT is_remote FROM postings WHERE dedup_key='k3|t'").fetchone()
    assert row["is_remote"] is None  # NULL survives — unknown, not false

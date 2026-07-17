import uuid

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

    # Mirror the company block for the posting_id partial index.
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES ('wl|posting', %s, 't', 'acme')",
        (company_id,),
    )
    posting_id = db_conn.execute("SELECT id FROM postings WHERE dedup_key='wl|posting'").fetchone()[
        "id"
    ]
    db_conn.execute(
        "INSERT INTO watchlists (user_id, posting_id) VALUES ('user_a', %s)", (posting_id,)
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db_conn.transaction():
            db_conn.execute(
                "INSERT INTO watchlists (user_id, posting_id) VALUES ('user_a', %s)",
                (posting_id,),
            )

    # Both targets set at once must fail the exactly-one CHECK constraint.
    with pytest.raises(psycopg.errors.CheckViolation):
        with db_conn.transaction():
            db_conn.execute(
                "INSERT INTO watchlists (user_id, posting_id, company_id) "
                "VALUES ('user_a', %s, %s)",
                (posting_id, company_id),
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
    with pytest.raises(psycopg.errors.CheckViolation):
        with db_conn.transaction():
            db_conn.execute(
                "INSERT INTO postings "
                "(dedup_key, company_id, title, company, posted_date_precision) "
                "VALUES ('k|t2', %s, 't', 'acme2', 'exact')",  # precision without date
                (cid,),
            )


def test_is_remote_tristate_representable(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('acme3')")
    cid = db_conn.execute("SELECT id FROM companies WHERE name='acme3'").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, is_remote) VALUES "
        "('k3|true', %s, 't', 'acme3', true), "
        "('k3|false', %s, 't', 'acme3', false), "
        "('k3|null', %s, 't', 'acme3', NULL)",
        (cid, cid, cid),
    )
    null_rows = db_conn.execute(
        "SELECT dedup_key FROM postings WHERE company_id = %s AND is_remote IS NULL", (cid,)
    ).fetchall()
    false_rows = db_conn.execute(
        "SELECT dedup_key FROM postings WHERE company_id = %s AND is_remote = false", (cid,)
    ).fetchall()
    true_rows = db_conn.execute(
        "SELECT dedup_key FROM postings WHERE company_id = %s AND is_remote = true", (cid,)
    ).fetchall()
    assert [r["dedup_key"] for r in null_rows] == ["k3|null"]
    assert [r["dedup_key"] for r in false_rows] == ["k3|false"]
    assert [r["dedup_key"] for r in true_rows] == ["k3|true"]


def test_companies_hit_status_requires_platform_and_slug(db_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        with db_conn.transaction():
            db_conn.execute("INSERT INTO companies (name, ats_probe_status) VALUES ('x', 'hit')")


def test_byo_key_credentials_force_rls_denies_owner_insert(db_conn):
    """FORCE ROW LEVEL SECURITY + zero policies must deny writes even to the
    table owner. db_conn connects as the Postgres superuser (ADMIN_DSN), and
    superusers always bypass RLS regardless of FORCE — a plain INSERT here
    would silently succeed. To actually exercise the owner-bypass closure,
    temporarily reassign table ownership to a throwaway non-superuser role
    and impersonate it via SET ROLE for the duration of the INSERT attempt.
    Both the role and the ownership change are undone by db_conn's own
    per-test ROLLBACK — nothing to clean up explicitly.
    """
    role = f"byo_rls_test_owner_{uuid.uuid4().hex[:8]}"
    db_conn.execute("INSERT INTO users (id) VALUES ('user_rls')")
    db_conn.execute(f"CREATE ROLE {role} NOLOGIN")
    db_conn.execute(f"ALTER TABLE byo_key_credentials OWNER TO {role}")
    db_conn.execute(f"SET ROLE {role}")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db_conn.transaction():
                db_conn.execute(
                    "INSERT INTO byo_key_credentials (user_id, provider, encrypted_key) "
                    "VALUES (%s, 'anthropic', %s)",
                    ("user_rls", b"fake-encrypted-key-bytes"),
                )
    finally:
        db_conn.execute("RESET ROLE")

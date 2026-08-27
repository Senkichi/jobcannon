"""jobcannon/db/migrations/m0007_revoked_subjects.py — the tombstone table
issue #159's revocation gate reads/writes through
jobcannon.db._revoked_subjects. Unlike test_m0006_analytics_consent_version's
ALTER-TABLE-against-populated-table concern, m0007 is a brand-new CREATE
TABLE with no pre-existing rows anywhere to migrate — nothing to prove here
beyond "the shape landed correctly and the index exists"; DAL behavior
(revoke/is_revoked/prune, upsert-extends-window) lives in
tests/host/test_revoked_subjects_dal.py."""

from __future__ import annotations

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def test_revoked_subjects_table_columns(db_conn):
    rows = db_conn.execute(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_name = 'revoked_subjects' ORDER BY column_name"
    ).fetchall()
    by_name = {row["column_name"]: row for row in rows}
    assert set(by_name) == {"clerk_user_id", "revoked_at", "expires_at"}

    assert by_name["clerk_user_id"]["data_type"] == "text"
    assert by_name["clerk_user_id"]["is_nullable"] == "NO"

    assert by_name["revoked_at"]["data_type"] == "timestamp with time zone"
    assert by_name["revoked_at"]["is_nullable"] == "NO"
    assert by_name["revoked_at"]["column_default"] is not None

    assert by_name["expires_at"]["data_type"] == "timestamp with time zone"
    assert by_name["expires_at"]["is_nullable"] == "NO"


def test_revoked_subjects_primary_key_is_clerk_user_id(db_conn):
    row = db_conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'revoked_subjects'::regclass AND i.indisprimary"
    ).fetchone()
    assert row["attname"] == "clerk_user_id"


def test_revoked_subjects_has_an_index_on_expires_at(db_conn):
    rows = db_conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'revoked_subjects'"
    ).fetchall()
    names = {row["indexname"] for row in rows}
    assert "idx_revoked_subjects_expires_at" in names


def test_revoked_subjects_starts_empty(db_conn):
    row = db_conn.execute("SELECT count(*) AS n FROM revoked_subjects").fetchone()
    assert row["n"] == 0

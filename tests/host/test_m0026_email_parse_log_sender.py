# PORTED from tests/test_migration_209524590_email_parse_log_per_sender.py @ a7f0f38a85dfa0af4d305c04da833785f723d649 (private job-cannon). Ledger L-0583.
"""Tests for Migration 209524590 — add email_parse_log_sender table (T2.6, D19).

Covers:
- Table creation with correct schema
- Index creation on (sender_label, processed_at)
- Idempotent re-run on empty DB
- Idempotent re-run on populated DB
- UNIQUE(run_id, sender_label) enforced

# PORT-SEAM: (L-0279, the migration itself) the public table
# (jobcannon/db/migrations/m0026_email_parse_log_sender.py) adds a
# user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE column
# (job-cannon private is single-user; this port is multi-tenant) and widens
# the private UNIQUE(run_id, sender_label) constraint to
# UNIQUE(user_id, run_id, sender_label) -- see the migration module's own
# docstring for the full rationale. Every test below carries a user_id
# accordingly.
#
# Same shape as tests/host/test_m0012_profiles_companies_workplace_type.py:
# information_schema/pg_indexes existence checks against the already-migrated
# db_conn fixture, real-Postgres UNIQUE-violation and value round-trip
# checks, plus a plain assertion on the MIGRATION module constant.
#
# DROPPED (private-only surface, listed in the PR body):
# - test_idempotent_on_empty_db and test_idempotent_on_populated_db drove
#   MIGRATION.sql a second time by hand against a raw sqlite connection and
#   asserted the CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
#   re-run was a silent no-op. The public migration runner
#   (jobcannon/db/migrate.py) applies each migration from a
#   schema_migrations SET-MEMBERSHIP ledger exactly once -- a migration is
#   never handed its own SQL to re-execute by the runner, and there is no
#   public analogue of "re-run migration N by hand against an
#   already-migrated database" to test. The set-membership guarantee itself
#   is covered generically by tests/host/test_migrate.py, not per-migration.
"""

from __future__ import annotations

# PORT-SEAM: os/sqlite3/tempfile imports dropped -- db_conn (tests/host/conftest.py)
# replaces the private migrated_db fixture's raw sqlite3 file below.
import psycopg
import pytest

# PORT-SEAM: MIGRATION_REBUILD_SANCTIONED opt-out marker dropped -- the public
# tests/host/conftest.py db_conn fixture applies the real Postgres migration
# chain per-DSN (session-scoped), not a per-test schema rebuild, so the private
# rebuild-guard opt-out this module needed does not apply here.
from jobcannon.db.migrations.m0026_email_parse_log_sender import MIGRATION


# PORT-SEAM: replaces the private migrated_db fixture (tempfile.mkstemp + a
# hand-rolled sqlite3 migration run) -- db_conn (tests/host/conftest.py) is
# already migrated real Postgres.
def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


# PORT-SEAM: sqlite_master/PRAGMA table_info -> information_schema.columns (Postgres)
def test_table_created(db_conn):
    rows = db_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'email_parse_log_sender'"
    ).fetchall()
    col_names = {row["column_name"] for row in rows}
    assert col_names == {
        "id",
        "user_id",  # PORT-SEAM: multi-tenant column (L-0279)
        "run_id",
        "sender_label",
        "processed_at",
        "emails_seen",
        "jobs_parsed",
        "error_count",
        "last_error",
    }


# PORT-SEAM: sqlite_master index lookup -> pg_indexes (Postgres)
def test_index_created(db_conn):
    rows = db_conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'email_parse_log_sender'"
    ).fetchall()
    index_names = {row["indexname"] for row in rows}
    assert "idx_email_parse_log_sender_label_processed_at" in index_names


# PORT-SEAM: renamed <- test_unique_run_id_sender_label; carries a user_id (L-0279 widening)
def test_unique_user_run_id_sender_label(db_conn):
    """UNIQUE(user_id, run_id, sender_label) rejects a duplicate triple."""
    _seed_user(db_conn, "m0026_unique_user")
    db_conn.execute(
        """INSERT INTO email_parse_log_sender
           (user_id, run_id, sender_label, processed_at, emails_seen, jobs_parsed, error_count)
           VALUES (%s, %s, %s, now(), %s, %s, %s)""",
        ("m0026_unique_user", "run-1", "linkedin", 2, 1, 0),
    )

    # PORT-SEAM: sqlite3.IntegrityError -> psycopg.errors.UniqueViolation (Postgres)
    with pytest.raises(psycopg.errors.UniqueViolation):
        db_conn.execute(
            """INSERT INTO email_parse_log_sender
               (user_id, run_id, sender_label, processed_at, emails_seen, jobs_parsed, error_count)
               VALUES (%s, %s, %s, now(), %s, %s, %s)""",
            ("m0026_unique_user", "run-1", "linkedin", 3, 2, 0),
        )


# PORT-SEAM: new test (not in the private suite) -- exercises the L-0279 multi-tenant
# widening directly: (run_id, sender_label) alone is no longer unique across users.
def test_same_run_id_sender_label_ok_across_users(db_conn):
    """The multi-tenant widening: (run_id, sender_label) alone is no longer
    unique -- two different users can independently produce the same
    run_id/sender_label pair without conflict."""
    _seed_user(db_conn, "m0026_user_a")
    _seed_user(db_conn, "m0026_user_b")
    db_conn.execute(
        """INSERT INTO email_parse_log_sender
           (user_id, run_id, sender_label, processed_at, emails_seen, jobs_parsed, error_count)
           VALUES (%s, %s, %s, now(), %s, %s, %s)""",
        ("m0026_user_a", "run-shared", "linkedin", 2, 1, 0),
    )
    # Must not raise -- different user_id, same (run_id, sender_label).
    db_conn.execute(
        """INSERT INTO email_parse_log_sender
           (user_id, run_id, sender_label, processed_at, emails_seen, jobs_parsed, error_count)
           VALUES (%s, %s, %s, now(), %s, %s, %s)""",
        ("m0026_user_b", "run-shared", "linkedin", 5, 3, 0),
    )

    # PORT-SEAM: COUNT-by-run_id replaces the private test's before/after MIGRATION.sql
    # re-run count check (see module docstring DROPPED note for why that check itself
    # doesn't carry) -- verifies both rows landed instead.
    count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM email_parse_log_sender WHERE run_id = 'run-shared'"
    ).fetchone()["n"]
    assert count == 2


# PORT-SEAM: test_unique_run_id_sender_label (private, pre-widening UNIQUE shape) is
# superseded above by test_unique_user_run_id_sender_label + this file's DROPPED note;
# test_idempotent_on_empty_db/test_idempotent_on_populated_db are DROPPED (see module
# docstring DROPPED note).
def test_zero_count_row_is_representable(db_conn):
    """A sender with zero emails this run is a real ZERO-COUNT row, not absent
    (the D19 fix: a lapsed alert subscription must be distinguishable from a
    silently-failing parser)."""
    # PORT-SEAM: carries a user_id (L-0279 widening)
    _seed_user(db_conn, "m0026_zero_user")
    db_conn.execute(
        """INSERT INTO email_parse_log_sender
           (user_id, run_id, sender_label, processed_at, emails_seen, jobs_parsed, error_count)
           VALUES (%s, %s, %s, now(), %s, %s, %s)""",
        ("m0026_zero_user", "run-1", "ziprecruiter", 0, 0, 0),
    )

    # PORT-SEAM: replaces the private test's raw INSERT/commit against migrated_db
    row = db_conn.execute(
        "SELECT emails_seen, jobs_parsed, error_count FROM email_parse_log_sender"
        # PORT-SEAM: scoped to user_id (L-0279 widening)
        " WHERE user_id = 'm0026_zero_user' AND sender_label = 'ziprecruiter'"
    ).fetchone()
    assert row is not None
    assert (row["emails_seen"], row["jobs_parsed"], row["error_count"]) == (
        0,
        0,
        0,
    )  # PORT-SEAM: dict_row (Postgres) replaces sqlite3.Row tuple() coercion


def test_migration_declares_version():
    """Schema sanity: the module exposes the right MIGRATION constant."""
    assert MIGRATION.version == 26  # PORT-SEAM: public migration version (was 209524590)
    assert MIGRATION.description == (
        "email_parse_log_sender table (per-sender IMAP parse outcomes, multi-tenant)"
        # PORT-SEAM: public MIGRATION.description string (L-0279)
    )
    assert len(MIGRATION.sql) == 2

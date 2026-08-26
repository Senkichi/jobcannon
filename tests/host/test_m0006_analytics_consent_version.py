"""jobcannon/db/migrations/m0006_analytics_consent_version.py — the column
that lets jobcannon/db/_events.py's read_consent_state /
read_consent_choice_made enforce CONSENT_VERSION (issue: consent version is
recorded but never enforced).

Same shape as tests/host/test_m0004_users_consent.py for the column-exists /
default checks. test_migration_applies_to_a_users_table_with_pre_existing_rows
additionally proves the migration itself (not just record_consent) works
against the realistic rollout shape — a `users` table that already has
rows — the same monkeypatch-MIGRATIONS technique
tests/host/test_migrate.py::test_migration_failure_does_not_roll_back_earlier_committed_migrations
uses to control exactly which migrations run.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from jobcannon.db._events import db_now_iso, record_consent
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def test_analytics_consent_version_column_exists_and_is_nullable(db_conn):
    rows = db_conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'analytics_consent_version'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["data_type"] == "text"
    assert rows[0]["is_nullable"] == "YES"


def test_analytics_consent_version_defaults_null(db_conn):
    db_conn.execute("INSERT INTO users (id, email) VALUES ('m0006_default_user', 'a@example.org')")
    row = db_conn.execute(
        "SELECT analytics_consent_version FROM users WHERE id = 'm0006_default_user'"
    ).fetchone()
    assert row["analytics_consent_version"] is None


def test_record_consent_writes_the_granted_version_to_the_column(db_conn):
    user_id = "m0006_record_user"
    db_conn.execute("INSERT INTO users (id, email) VALUES (%s, 'b@example.org')", (user_id,))

    record_consent(
        db_conn,
        user_id=user_id,
        consent_type="analytics",
        granted=True,
        consent_version="v7",
        consented_at=db_now_iso(db_conn),
    )

    row = db_conn.execute(
        "SELECT analytics_consent_version FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    assert row["analytics_consent_version"] == "v7"


def test_migration_applies_to_a_users_table_with_pre_existing_rows(monkeypatch):
    """m0006 must succeed as an ALTER TABLE ADD COLUMN against a `users`
    table that already has rows, not only a brand-new empty database. A
    pre-existing row predates version tracking entirely, so its new column
    must land NULL — never enforced retroactively against a version it was
    never recorded against."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations import MIGRATIONS

    dsn, db_name = create_throwaway_db("jobcannon_mig_m0006_populated")
    try:
        pre_m0006 = [m for m in MIGRATIONS if m.version < 6]
        monkeypatch.setattr(migrate_mod, "MIGRATIONS", pre_m0006)
        migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO users (id, email, analytics_consent, analytics_consent_updated_at) "
                "VALUES ('pre_m0006_user', 'c@example.org', true, now())"
            )
            conn.commit()

        monkeypatch.setattr(migrate_mod, "MIGRATIONS", MIGRATIONS)
        migrate_mod.run_migrations(dsn)  # must not raise against the populated table

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT analytics_consent, analytics_consent_version FROM users "
                "WHERE id = 'pre_m0006_user'"
            ).fetchone()
        assert row["analytics_consent"] is True
        assert row["analytics_consent_version"] is None

        # The runtime consequence of that NULL: read_consent_state must
        # treat this pre-existing grant as not consented against ANY
        # current version, since it predates version tracking entirely.
        from jobcannon.db._events import read_consent_state

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            assert read_consent_state(conn, "pre_m0006_user", current_version="v1") is False
    finally:
        drop_throwaway_db(db_name)

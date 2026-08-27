"""jobcannon/db/migrations/m0008_profiles_comp_floor.py — the column
comp_fit scoring anchors against (issue #28 item 2: comp_fit had no backing
profiles column).

Same shape as tests/host/test_m0006_analytics_consent_version.py: column
existence/nullability/default checks, plus
test_migration_applies_to_a_users_table_with_pre_existing_rows's
monkeypatch-MIGRATIONS technique to prove the ALTER TABLE itself succeeds
against a `profiles` table that already has rows, not only a fresh empty
database. This module additionally covers the CHECK constraint (m0008 adds
one, m0006 didn't need one)."""

from __future__ import annotations

import psycopg
import psycopg.errors
import pytest
from psycopg.rows import dict_row

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def test_comp_floor_usd_column_exists_and_is_nullable(db_conn):
    rows = db_conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'profiles' AND column_name = 'comp_floor_usd'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["data_type"] == "integer"
    assert rows[0]["is_nullable"] == "YES"


def test_comp_floor_usd_defaults_null(db_conn):
    _seed_user(db_conn, "m0008_default_user")
    db_conn.execute("INSERT INTO profiles (user_id) VALUES ('m0008_default_user')")

    row = db_conn.execute(
        "SELECT comp_floor_usd FROM profiles WHERE user_id = 'm0008_default_user'"
    ).fetchone()
    assert row["comp_floor_usd"] is None


def test_comp_floor_usd_accepts_zero_and_positive_values(db_conn):
    _seed_user(db_conn, "m0008_positive_user")
    db_conn.execute(
        "INSERT INTO profiles (user_id, comp_floor_usd) VALUES (%s, %s)",
        ("m0008_positive_user", 120000),
    )

    row = db_conn.execute(
        "SELECT comp_floor_usd FROM profiles WHERE user_id = 'm0008_positive_user'"
    ).fetchone()
    assert row["comp_floor_usd"] == 120000


def test_comp_floor_usd_check_constraint_rejects_negative_values(db_conn):
    """profiles_comp_floor_usd_nonneg must fail loud (CheckViolation) on a
    negative value, not silently accept it — comp_fit scoring must never
    anchor against a value that can't represent a real compensation floor."""
    _seed_user(db_conn, "m0008_negative_user")

    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO profiles (user_id, comp_floor_usd) VALUES (%s, %s)",
            ("m0008_negative_user", -1),
        )


def test_migration_applies_to_a_profiles_table_with_pre_existing_rows(monkeypatch):
    """m0008 must succeed as an ALTER TABLE ADD COLUMN + ADD CONSTRAINT
    against a `profiles` table that already has rows, not only a brand-new
    empty database. A pre-existing row predates the column entirely, so its
    new column must land NULL, and the CHECK (which only constrains
    non-NULL values) must not reject the backfilled NULL."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations import MIGRATIONS

    dsn, db_name = create_throwaway_db("jobcannon_mig_m0008_populated")
    try:
        pre_m0008 = [m for m in MIGRATIONS if m.version < 8]
        monkeypatch.setattr(migrate_mod, "MIGRATIONS", pre_m0008)
        migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            conn.execute("INSERT INTO users (id, plan_tier) VALUES ('pre_m0008_user', 'free')")
            conn.execute(
                "INSERT INTO profiles (user_id, seniority_level) VALUES ('pre_m0008_user', 'senior')"
            )
            conn.commit()

        monkeypatch.setattr(migrate_mod, "MIGRATIONS", MIGRATIONS)
        migrate_mod.run_migrations(dsn)  # must not raise against the populated table

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT seniority_level, comp_floor_usd FROM profiles WHERE user_id = 'pre_m0008_user'"
            ).fetchone()
        assert row["seniority_level"] == "senior"
        assert row["comp_floor_usd"] is None
    finally:
        drop_throwaway_db(db_name)

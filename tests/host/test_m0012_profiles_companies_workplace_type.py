"""jobcannon/db/migrations/m0012_profiles_companies_workplace_type.py — the
two columns #169/#170 needed a durable home for: `target_companies` (the
picker's company selection) and `workplace_type` (remote/hybrid/onsite
preference). Before this migration only `target_titles` (m0001) survived
the anon-to-authed handoff; see the migration module's own docstring for
the full rationale.

Same shape as tests/host/test_m0008_profiles_comp_floor.py: column
existence/nullability/type checks, plus
test_migration_applies_to_a_profiles_table_with_pre_existing_rows's
monkeypatch-MIGRATIONS technique to prove both ADD COLUMNs succeed against
a `profiles` table that already has rows, not only a fresh empty database.
Unlike m0008, neither new column carries a CHECK constraint (by design —
see the migration docstring), so there is no constraint-rejection test
here."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def test_target_companies_column_exists_and_is_nullable_jsonb(db_conn):
    rows = db_conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'profiles' AND column_name = 'target_companies'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["data_type"] == "jsonb"
    assert rows[0]["is_nullable"] == "YES"


def test_workplace_type_column_exists_and_is_nullable_text(db_conn):
    rows = db_conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'profiles' AND column_name = 'workplace_type'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["data_type"] == "text"
    assert rows[0]["is_nullable"] == "YES"


def test_target_companies_and_workplace_type_default_null(db_conn):
    _seed_user(db_conn, "m0012_default_user")
    db_conn.execute("INSERT INTO profiles (user_id) VALUES ('m0012_default_user')")

    row = db_conn.execute(
        "SELECT target_companies, workplace_type FROM profiles WHERE user_id = 'm0012_default_user'"
    ).fetchone()
    assert row["target_companies"] is None
    assert row["workplace_type"] is None


def test_target_companies_accepts_a_jsonb_array_and_workplace_type_accepts_text(db_conn):
    _seed_user(db_conn, "m0012_populated_user")
    db_conn.execute(
        "INSERT INTO profiles (user_id, target_companies, workplace_type) VALUES (%s, %s, %s)",
        ("m0012_populated_user", Jsonb(["Acme Corp", "Globex"]), "REMOTE"),
    )

    row = db_conn.execute(
        "SELECT target_companies, workplace_type FROM profiles WHERE user_id = 'm0012_populated_user'"
    ).fetchone()
    assert row["target_companies"] == ["Acme Corp", "Globex"]
    assert row["workplace_type"] == "REMOTE"


def test_target_companies_accepts_an_empty_jsonb_array(db_conn):
    """#169's `upsert_profile` COALESCE-preserve-when-omitted design (see
    jobcannon/db/_profiles.py) depends on `Jsonb([])` being a real, distinct-
    from-NULL storable value — that's what lets a caller pass an empty list
    literally to CLEAR a prior selection rather than accidentally reviving
    it via COALESCE. Assert the column itself round-trips `[]` faithfully,
    independent of the DAL logic that depends on it."""
    _seed_user(db_conn, "m0012_empty_array_user")
    db_conn.execute(
        "INSERT INTO profiles (user_id, target_companies) VALUES (%s, %s)",
        ("m0012_empty_array_user", Jsonb([])),
    )

    row = db_conn.execute(
        "SELECT target_companies FROM profiles WHERE user_id = 'm0012_empty_array_user'"
    ).fetchone()
    assert row["target_companies"] == []
    assert row["target_companies"] is not None


def test_migration_applies_to_a_profiles_table_with_pre_existing_rows(monkeypatch):
    """m0012 must succeed as two ALTER TABLE ADD COLUMN statements against a
    `profiles` table that already has rows, not only a brand-new empty
    database. A pre-existing row predates both columns entirely, so they
    must land NULL — there is no CHECK constraint to interact with, unlike
    m0008's comp_floor_usd, but the ADD COLUMN itself must not choke on
    existing data."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations import MIGRATIONS

    dsn, db_name = create_throwaway_db("jobcannon_mig_m0012_populated")
    try:
        pre_m0012 = [m for m in MIGRATIONS if m.version < 12]
        monkeypatch.setattr(migrate_mod, "MIGRATIONS", pre_m0012)
        migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            conn.execute("INSERT INTO users (id, plan_tier) VALUES ('pre_m0012_user', 'free')")
            conn.execute(
                "INSERT INTO profiles (user_id, seniority_level) VALUES ('pre_m0012_user', 'senior')"
            )
            conn.commit()

        monkeypatch.setattr(migrate_mod, "MIGRATIONS", MIGRATIONS)
        migrate_mod.run_migrations(dsn)  # must not raise against the populated table

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT seniority_level, target_companies, workplace_type "
                "FROM profiles WHERE user_id = 'pre_m0012_user'"
            ).fetchone()
        assert row["seniority_level"] == "senior"
        assert row["target_companies"] is None
        assert row["workplace_type"] is None
    finally:
        drop_throwaway_db(db_name)

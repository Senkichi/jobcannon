"""jobcannon/db/migrations/m0030_profiles_config_shaped_candidate_fields.py —
the five typed nullable columns issue #420's Option B adds so `profiles`
covers every config-shaped candidate-context field the scoring_orchestrator
renderer reads: `work_arrangement` (text — the location preference
hierarchy token, deliberately distinct from m0012's UPPERCASE feed-filter
`workplace_type`), `industries`/`positions`/`education` (jsonb), and
`exclusions` (a jsonb OBJECT mirroring config `profile.exclusions`
verbatim). See the migration module's own docstring for the full rationale.

Same shape as tests/host/test_m0012_profiles_companies_workplace_type.py:
column existence/nullability/type checks, plus
test_migration_applies_to_a_profiles_table_with_pre_existing_rows's
monkeypatch-MIGRATIONS technique to prove the five ADD COLUMNs succeed
against a `profiles` table that already has rows, not only a fresh empty
database. Like m0012 (and unlike m0008), no new column carries a CHECK
constraint — the repo norm is validation at the write boundary — so there
is no constraint-rejection test here.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def test_new_columns_exist_with_expected_types_and_nullability(db_conn):
    rows = db_conn.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'profiles' AND column_name IN "
        "('work_arrangement', 'industries', 'exclusions', 'positions', 'education')"
    ).fetchall()
    by_name = {row["column_name"]: row for row in rows}
    assert set(by_name) == {
        "work_arrangement",
        "industries",
        "exclusions",
        "positions",
        "education",
    }
    assert by_name["work_arrangement"]["data_type"] == "text"
    for name in ("industries", "exclusions", "positions", "education"):
        assert by_name[name]["data_type"] == "jsonb", name
    for row in rows:
        assert row["is_nullable"] == "YES", row["column_name"]


def test_new_columns_default_null(db_conn):
    _seed_user(db_conn, "m0030_default_user")
    db_conn.execute("INSERT INTO profiles (user_id) VALUES ('m0030_default_user')")

    row = db_conn.execute(
        "SELECT work_arrangement, industries, exclusions, positions, education "
        "FROM profiles WHERE user_id = 'm0030_default_user'"
    ).fetchone()
    assert row["work_arrangement"] is None
    assert row["industries"] is None
    assert row["exclusions"] is None
    assert row["positions"] is None
    assert row["education"] is None


def test_new_columns_roundtrip_config_shaped_values(db_conn):
    """The columns must round-trip the exact shapes the config-shaped
    renderer reads: a lowercase arrangement token, a string list for
    industries, the exclusions OBJECT (companies + title_keywords, the two
    keys engine/exclusion_filter.py reads), and object lists for
    positions/education."""
    _seed_user(db_conn, "m0030_populated_user")
    positions = [
        {
            "title": "Senior Data Scientist",
            "company": "Acme Corp",
            "start_date": "2020-01",
            "end_date": None,
        }
    ]
    education = [{"degree": "BS Statistics", "institution": "State U", "graduation": 2015}]
    exclusions = {"companies": ["Bad Co"], "title_keywords": ["intern"]}
    db_conn.execute(
        "INSERT INTO profiles (user_id, work_arrangement, industries, exclusions, "
        "positions, education) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            "m0030_populated_user",
            "remote",
            Jsonb(["fintech", "healthcare"]),
            Jsonb(exclusions),
            Jsonb(positions),
            Jsonb(education),
        ),
    )

    row = db_conn.execute(
        "SELECT work_arrangement, industries, exclusions, positions, education "
        "FROM profiles WHERE user_id = 'm0030_populated_user'"
    ).fetchone()
    assert row["work_arrangement"] == "remote"
    assert row["industries"] == ["fintech", "healthcare"]
    assert row["exclusions"] == exclusions
    assert row["positions"] == positions
    assert row["education"] == education


def test_exclusions_accepts_an_empty_jsonb_object(db_conn):
    """`exclusions` is the one new column whose value is an OBJECT, not a
    list — assert `{}` round-trips distinctly from NULL so a 'no
    exclusions' write can never be mistaken for 'field never set'."""
    _seed_user(db_conn, "m0030_empty_exclusions_user")
    db_conn.execute(
        "INSERT INTO profiles (user_id, exclusions) VALUES (%s, %s)",
        ("m0030_empty_exclusions_user", Jsonb({})),
    )

    row = db_conn.execute(
        "SELECT exclusions FROM profiles WHERE user_id = 'm0030_empty_exclusions_user'"
    ).fetchone()
    assert row["exclusions"] == {}
    assert row["exclusions"] is not None


def test_migration_applies_to_a_profiles_table_with_pre_existing_rows(monkeypatch):
    """m0030 must succeed as five ALTER TABLE ADD COLUMN statements against
    a `profiles` table that already has rows, not only a brand-new empty
    database. A pre-existing row predates all five columns entirely, so
    they must land NULL — there is no CHECK constraint to interact with,
    unlike m0008's comp_floor_usd, but the ADD COLUMNs themselves must not
    choke on existing data."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations import MIGRATIONS

    dsn, db_name = create_throwaway_db("jobcannon_mig_m0030_populated")
    try:
        pre_m0030 = [m for m in MIGRATIONS if m.version < 30]
        monkeypatch.setattr(migrate_mod, "MIGRATIONS", pre_m0030)
        migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            conn.execute("INSERT INTO users (id, plan_tier) VALUES ('pre_m0030_user', 'free')")
            conn.execute(
                "INSERT INTO profiles (user_id, seniority_level) VALUES ('pre_m0030_user', 'senior')"
            )
            conn.commit()

        monkeypatch.setattr(migrate_mod, "MIGRATIONS", MIGRATIONS)
        migrate_mod.run_migrations(dsn)  # must not raise against the populated table

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT seniority_level, work_arrangement, industries, exclusions, "
                "positions, education FROM profiles WHERE user_id = 'pre_m0030_user'"
            ).fetchone()
        assert row["seniority_level"] == "senior"
        for col in ("work_arrangement", "industries", "exclusions", "positions", "education"):
            assert row[col] is None, col
    finally:
        drop_throwaway_db(db_name)

"""jobcannon/db/migrations/m0009_postings_jd_content.py -- the three columns
that arm the D5 scoring gate (#152): jd_content_verdict / jd_content_signal
(text, written by jobcannon.db._jd_full.set_jd_full) and
jd_adjudicated_version (integer, read by
jobcannon.engine.job_scorer.scoring_precheck).

Same shape as tests/host/test_m0006_analytics_consent_version.py for the
column-exists / nullable / pre-existing-rows checks. Write-time stamping
behavior is covered by tests/host/test_jd_full.py; this file only pins the
schema shape.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

_COLUMNS = {
    "jd_content_verdict": "text",
    "jd_content_signal": "text",
    "jd_adjudicated_version": "integer",
}


def test_columns_exist_and_are_nullable(db_conn):
    for column, expected_type in _COLUMNS.items():
        rows = db_conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'postings' AND column_name = %s",
            (column,),
        ).fetchall()
        assert len(rows) == 1, column
        assert rows[0]["data_type"] == expected_type, column
        assert rows[0]["is_nullable"] == "YES", column


def test_columns_default_null_on_insert(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('m0009-co')")
    cid = db_conn.execute("SELECT id FROM companies WHERE name='m0009-co'").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES ('m0009-co|role', %s, 'Role', 'm0009-co')",
        (cid,),
    )
    row = db_conn.execute(
        "SELECT jd_content_verdict, jd_content_signal, jd_adjudicated_version "
        "FROM postings WHERE dedup_key = 'm0009-co|role'"
    ).fetchone()
    assert row["jd_content_verdict"] is None
    assert row["jd_content_signal"] is None
    assert row["jd_adjudicated_version"] is None


def test_migration_applies_to_a_postings_table_with_pre_existing_rows(monkeypatch):
    """m0009 must succeed as an ALTER TABLE ADD COLUMN against a `postings`
    table that already has rows, not only a brand-new empty database. A
    pre-existing row predates the D5 gate entirely, so its new columns must
    land NULL -- fail-open, never a retroactive gate."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations import MIGRATIONS

    dsn, db_name = create_throwaway_db("jobcannon_mig_m0009_populated")
    try:
        pre_m0009 = [m for m in MIGRATIONS if m.version < 9]
        monkeypatch.setattr(migrate_mod, "MIGRATIONS", pre_m0009)
        migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            conn.execute("INSERT INTO companies (name) VALUES ('pre-m0009-co')")
            cid = conn.execute("SELECT id FROM companies WHERE name='pre-m0009-co'").fetchone()[0]
            conn.execute(
                "INSERT INTO postings (dedup_key, company_id, title, company) "
                "VALUES ('pre-m0009-co|role', %s, 'Role', 'pre-m0009-co')",
                (cid,),
            )
            conn.commit()

        monkeypatch.setattr(migrate_mod, "MIGRATIONS", MIGRATIONS)
        migrate_mod.run_migrations(dsn)  # must not raise against the populated table

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT jd_content_verdict, jd_adjudicated_version FROM postings "
                "WHERE dedup_key = 'pre-m0009-co|role'"
            ).fetchone()
        assert row["jd_content_verdict"] is None
        assert row["jd_adjudicated_version"] is None
    finally:
        drop_throwaway_db(db_name)

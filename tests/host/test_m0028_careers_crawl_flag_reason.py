"""tests/host/test_m0028_careers_crawl_flag_reason.py — migration 28
(``companies.careers_crawl_flag_reason``, issue #370).

Schema tests follow ``tests/host/test_m0012_profiles_companies_workplace_type.py``'s
shape: column existence/nullability/type checks against the rollback-isolated
``db_conn`` fixture, plus the monkeypatch-``MIGRATIONS`` technique proving the
``ADD COLUMN`` succeeds against a ``companies`` table that already has rows.

The functional test at the bottom is the actual regression check for #370:
before this migration, ``record_legitimacy_flag``'s ``UPDATE`` and
``crawl_careers_batch``'s ``careers_crawl_flag_reason IS NULL`` reader both
raised ``psycopg.errors.UndefinedColumn`` against a real Postgres connection
-- the writer/reader pair landed in #359 against a schema that never got the
column that backs them. It drives the REAL ``record_legitimacy_flag`` writer
(``jobcannon/engine/careers_crawler/_cohort_legitimacy.py``) through the real
``ScanServices.connection_factory`` (``jobcannon/db/pool.py``'s pooled
``EngineCompatConnection``, the same qmark-translation seam every other
engine-on-Postgres contract test in this directory uses -- see
``test_scan_services_contract.py``'s ``wired_services`` fixture, which the
one below mirrors), then re-runs the exact ``careers_crawl_flag_reason IS
NULL`` predicate both of ``crawl_careers_batch``'s lane queries apply
(``jobcannon/engine/careers_crawler/__init__.py`` L191/L214).

``crawl_careers_batch`` itself is not called directly: its two lane queries
also carry pre-existing, unrelated Postgres-incompatibilities (a bare
``ats_probe_status IS NOT 'hit'`` predicate, which is not valid Postgres
``IS [NOT]`` syntax outside ``IS [NOT] DISTINCT FROM``, and an untranslated
``datetime('now', ? || ' days')`` shape the compat shim's date-function
rewrite does not match -- see ``jobcannon/db/compat.py``'s
"KNOWN-UNSUPPORTED" section) that are out of #370's column-only scope.
Exercising the ``careers_crawl_flag_reason IS NULL`` fragment directly
through the same compat-translated connection is the smallest slice that
proves this migration's column is what #370 needed, without also asserting
those separate, pre-existing gaps are fixed.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def test_careers_crawl_flag_reason_column_exists_and_is_nullable_text(db_conn):
    rows = db_conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'companies' AND column_name = 'careers_crawl_flag_reason'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["data_type"] == "text"
    assert rows[0]["is_nullable"] == "YES"


def test_careers_crawl_flag_reason_defaults_null(db_conn):
    company_id = db_conn.execute(
        "INSERT INTO companies (name) VALUES ('m0028 Default Co') RETURNING id"
    ).fetchone()["id"]

    row = db_conn.execute(
        "SELECT careers_crawl_flag_reason FROM companies WHERE id = %s", (company_id,)
    ).fetchone()
    assert row["careers_crawl_flag_reason"] is None


def test_careers_crawl_flag_reason_accepts_text(db_conn):
    reason = "aggregator_suspected:2_distinct_employers_in_10_sampled"
    company_id = db_conn.execute(
        "INSERT INTO companies (name, careers_crawl_flag_reason) VALUES (%s, %s) RETURNING id",
        ("m0028 Flagged Co", reason),
    ).fetchone()["id"]

    row = db_conn.execute(
        "SELECT careers_crawl_flag_reason FROM companies WHERE id = %s", (company_id,)
    ).fetchone()
    assert row["careers_crawl_flag_reason"] == reason


def test_migration_applies_to_a_companies_table_with_pre_existing_rows(monkeypatch):
    """m0028 must succeed as a single ALTER TABLE ADD COLUMN against a
    `companies` table that already has rows, not only a brand-new empty
    database -- same shape as test_m0012's own pre-existing-rows test."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations import MIGRATIONS

    dsn, db_name = create_throwaway_db("jobcannon_mig_m0028_populated")
    try:
        pre_m0028 = [m for m in MIGRATIONS if m.version < 28]
        monkeypatch.setattr(migrate_mod, "MIGRATIONS", pre_m0028)
        migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            conn.execute("INSERT INTO companies (name) VALUES ('pre_m0028_co')")
            conn.commit()

        monkeypatch.setattr(migrate_mod, "MIGRATIONS", MIGRATIONS)
        migrate_mod.run_migrations(dsn)  # must not raise against the populated table

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT name, careers_crawl_flag_reason FROM companies WHERE name = 'pre_m0028_co'"
            ).fetchone()
        assert row["name"] == "pre_m0028_co"
        assert row["careers_crawl_flag_reason"] is None
    finally:
        drop_throwaway_db(db_name)


@pytest.fixture()
def wired_crawler_services():
    """Own throwaway database + pool, mirroring
    ``test_scan_services_contract.py``'s ``wired_services`` fixture: this
    test does real, durable commits (``record_legitimacy_flag``'s own
    ``conn.commit()`` call, unmocked), so it needs an isolated database
    rather than the shared session-scoped ``postgres_test_dsn`` every
    rollback-isolated test in this directory reads."""
    from jobcannon.db import _companies, _jd_full, _jobs
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.engine import services

    dsn, db_name = create_throwaway_db("jobcannon_m0028_crawler")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        services.set_services(
            services.ScanServices(
                connection_factory=pool_mod.connection_factory,
                upsert_job=_jobs.upsert_job,
                set_jd_full=_jd_full.set_jd_full,
                upsert_company=_companies.upsert_company,
                config={},
                get_secret=lambda name, *, config=None: None,
                jd_storage_max_chars=50_000,
            )
        )
        yield services.get_services()
    finally:
        services.clear_services()
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def test_record_legitimacy_flag_then_batch_select_filter_run_without_undefined_column(
    wired_crawler_services,
):
    """The #370 regression, exercised end-to-end against real Postgres:
    ``record_legitimacy_flag``'s UPDATE (the writer, landed in #359) and
    ``crawl_careers_batch``'s ``careers_crawl_flag_reason IS NULL`` reader
    (both lanes) must both run without ``UndefinedColumn`` now that m0028
    has added the column."""
    from jobcannon.engine.careers_crawler._cohort_legitimacy import record_legitimacy_flag

    svc = wired_crawler_services
    with svc.connection_factory() as conn:
        flagged_id = conn.execute(
            "INSERT INTO companies (name) VALUES (?) RETURNING id",
            ("m0028 Aggregator Suspect",),
        ).fetchone()[0]
        clean_id = conn.execute(
            "INSERT INTO companies (name) VALUES (?) RETURNING id",
            ("m0028 Legitimate Co",),
        ).fetchone()[0]
        conn.commit()

    # Writer: the exact statement record_legitimacy_flag runs (L-0464, #359).
    reason = "aggregator_suspected:3_distinct_employers_in_10_sampled"
    record_legitimacy_flag(flagged_id, reason)

    # Reader: the exact `careers_crawl_flag_reason IS NULL` predicate both
    # of crawl_careers_batch's lane queries apply (jobcannon/engine/
    # careers_crawler/__init__.py L191/L214), run through the same
    # compat-translated connection.
    with svc.connection_factory() as conn:
        row = conn.execute(
            "SELECT careers_crawl_flag_reason FROM companies WHERE id = ?", (flagged_id,)
        ).fetchone()
        assert row[0] == reason

        unflagged_ids = {
            r[0]
            for r in conn.execute(
                "SELECT id FROM companies WHERE careers_crawl_flag_reason IS NULL AND id IN (?, ?)",
                (flagged_id, clean_id),
            ).fetchall()
        }
    assert unflagged_ids == {clean_id}

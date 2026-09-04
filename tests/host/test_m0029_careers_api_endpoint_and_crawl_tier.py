"""tests/host/test_m0029_careers_api_endpoint_and_crawl_tier.py — migration 29
(``companies.careers_api_endpoint``, ``companies.careers_crawl_tier``,
``company_scan_log.jobs_matched``; issues #385, #347, #386).

Schema tests follow ``tests/host/test_m0028_careers_crawl_flag_reason.py``'s
shape: column existence/nullability/type checks against the rollback-isolated
``db_conn`` fixture, plus the monkeypatch-``MIGRATIONS`` technique proving
the three ``ADD COLUMN`` statements succeed against tables that already have
rows.

The functional test at the bottom is the actual regression check for #386:
before this migration, ``_bench_predicate.py``'s ``build_bench_predicate_sql``
fragment (``SUM(CASE WHEN jobs_matched > 0 ...)``) raised
``psycopg.errors.UndefinedColumn`` against a real Postgres connection —
``company_scan_log.jobs_matched`` was read by that predicate's SQL text but
never backed by any migration (m0001/m0013/m0023 are the only three that
ever touch ``company_scan_log``, and none of them add it — see m0029's own
docstring). It drives the real ``record_scan_outcome`` writer
(``jobcannon/engine/ats_scanner/_scan_log.py``, already passing
``jobs_matched`` today but silently dropped by that writer's present-column
introspection before this migration) and then re-runs the exact bench
predicate fragment ``crawl_careers_batch`` interpolates into both lane
queries, through the same pooled ``EngineCompatConnection`` seam
``test_m0028_careers_crawl_flag_reason.py``'s ``wired_crawler_services``
fixture uses.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


@pytest.mark.parametrize(
    "table,column,expected_type",
    [
        ("companies", "careers_api_endpoint", "text"),
        ("companies", "careers_crawl_tier", "text"),
        ("company_scan_log", "jobs_matched", "integer"),
    ],
)
def test_column_exists_and_is_nullable(db_conn, table, column, expected_type):
    rows = db_conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["data_type"] == expected_type
    assert rows[0]["is_nullable"] == "YES"


def test_companies_columns_default_null(db_conn):
    company_id = db_conn.execute(
        "INSERT INTO companies (name) VALUES ('m0029 Default Co') RETURNING id"
    ).fetchone()["id"]

    row = db_conn.execute(
        "SELECT careers_api_endpoint, careers_crawl_tier FROM companies WHERE id = %s",
        (company_id,),
    ).fetchone()
    assert row["careers_api_endpoint"] is None
    assert row["careers_crawl_tier"] is None


def test_companies_columns_accept_text(db_conn):
    company_id = db_conn.execute(
        "INSERT INTO companies (name, careers_api_endpoint, careers_crawl_tier) "
        "VALUES (%s, %s, %s) RETURNING id",
        ("m0029 Wired Co", "https://acme.example/api/jobs", "static"),
    ).fetchone()["id"]

    row = db_conn.execute(
        "SELECT careers_api_endpoint, careers_crawl_tier FROM companies WHERE id = %s",
        (company_id,),
    ).fetchone()
    assert row["careers_api_endpoint"] == "https://acme.example/api/jobs"
    assert row["careers_crawl_tier"] == "static"


def test_jobs_matched_defaults_null_and_accepts_int(db_conn):
    company_id = db_conn.execute(
        "INSERT INTO companies (name) VALUES ('m0029 Scan Co') RETURNING id"
    ).fetchone()["id"]
    default_row_id = db_conn.execute(
        "INSERT INTO company_scan_log (company_id, source, scanned_at) "
        "VALUES (%s, 'careers_crawler', now()) RETURNING id",
        (company_id,),
    ).fetchone()["id"]
    hit_row_id = db_conn.execute(
        "INSERT INTO company_scan_log (company_id, source, jobs_matched, scanned_at) "
        "VALUES (%s, 'careers_crawler', %s, now()) RETURNING id",
        (company_id, 3),
    ).fetchone()["id"]

    default_row = db_conn.execute(
        "SELECT jobs_matched FROM company_scan_log WHERE id = %s", (default_row_id,)
    ).fetchone()
    hit_row = db_conn.execute(
        "SELECT jobs_matched FROM company_scan_log WHERE id = %s", (hit_row_id,)
    ).fetchone()
    assert default_row["jobs_matched"] is None
    assert hit_row["jobs_matched"] == 3


def test_migration_applies_to_tables_with_pre_existing_rows(monkeypatch):
    """m0029 must succeed as three ALTER TABLE ADD COLUMN statements against
    `companies` and `company_scan_log` tables that already have rows, not
    only a brand-new empty database — same shape as test_m0028's own
    pre-existing-rows test."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations import MIGRATIONS

    dsn, db_name = create_throwaway_db("jobcannon_mig_m0029_populated")
    try:
        pre_m0029 = [m for m in MIGRATIONS if m.version < 29]
        monkeypatch.setattr(migrate_mod, "MIGRATIONS", pre_m0029)
        migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            conn.execute("INSERT INTO companies (name) VALUES ('pre_m0029_co')")
            conn.execute(
                "INSERT INTO company_scan_log (company_id, source, scanned_at) "
                "SELECT id, 'careers_crawler', now() FROM companies "
                "WHERE name = 'pre_m0029_co'"
            )
            conn.commit()

        monkeypatch.setattr(migrate_mod, "MIGRATIONS", MIGRATIONS)
        migrate_mod.run_migrations(dsn)  # must not raise against the populated tables

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            company_row = conn.execute(
                "SELECT name, careers_api_endpoint, careers_crawl_tier FROM companies "
                "WHERE name = 'pre_m0029_co'"
            ).fetchone()
            log_row = conn.execute(
                "SELECT jobs_matched FROM company_scan_log WHERE company_id = "
                "(SELECT id FROM companies WHERE name = 'pre_m0029_co')"
            ).fetchone()
        assert company_row["name"] == "pre_m0029_co"
        assert company_row["careers_api_endpoint"] is None
        assert company_row["careers_crawl_tier"] is None
        assert log_row["jobs_matched"] is None
    finally:
        drop_throwaway_db(db_name)


@pytest.fixture()
def wired_crawler_services():
    """Own throwaway database + pool, mirroring
    ``test_m0028_careers_crawl_flag_reason.py``'s ``wired_crawler_services``
    fixture: this test does real, durable commits (``record_scan_outcome``'s
    caller owns the commit), so it needs an isolated database rather than
    the shared session-scoped ``postgres_test_dsn``."""
    from jobcannon.db import _companies, _jd_full, _jobs
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.engine import services

    dsn, db_name = create_throwaway_db("jobcannon_m0029_crawler")
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


def test_record_scan_outcome_then_bench_predicate_run_without_undefined_column(
    wired_crawler_services,
):
    """The #386 regression, exercised end-to-end against real Postgres:
    ``record_scan_outcome`` (the writer, already passing ``jobs_matched``
    today) and ``build_bench_predicate_sql``'s ``SUM(CASE WHEN
    jobs_matched > 0 ...)`` fragment (the reader, both crawl_careers_batch
    lanes) must both run without ``UndefinedColumn`` now that m0029 has
    added the column."""
    from jobcannon.engine.ats_scanner._scan_log import record_scan_outcome
    from jobcannon.engine.careers_crawler._bench_predicate import build_bench_predicate_sql

    svc = wired_crawler_services
    with svc.connection_factory() as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES (?) RETURNING id",
            ("m0029 Scan Outcome Co",),
        ).fetchone()[0]
        conn.commit()

    # Writer: the exact call _persistence.py makes
    # (jobs_matched=company_jobs_found).
    with svc.connection_factory() as conn:
        record_scan_outcome(
            conn,
            company_id=company_id,
            source="careers_crawler",
            jobs_found=2,
            jobs_matched=2,
            jobs_new=1,
        )
        conn.commit()

    # Reader: the exact bench-predicate fragment both crawl_careers_batch
    # lanes interpolate into their WHERE clause.
    sql, params = build_bench_predicate_sql(21)
    with svc.connection_factory() as conn:
        row = conn.execute(
            f"SELECT {sql} FROM companies c WHERE c.id = ?", (*params, company_id)
        ).fetchone()
    # The predicate SQL is `NOT EXISTS(<is-benched>)`, so True means eligible
    # (NOT benched) — a single hit row (jobs_matched=2) clears benching
    # outright for this company.
    assert row[0] is True

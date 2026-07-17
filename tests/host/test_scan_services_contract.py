"""1B contract test: the real Postgres adapter satisfies ScanServices exactly
as tests/engine/test_scan_seam.py's fake did — driving the engine's own
_upsert_one_ats_api_job against a live Postgres database."""

import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


@pytest.fixture()
def wired_services():
    """Own throwaway database, NOT the shared session-scoped postgres_test_dsn.

    This test does real, durable commits by design (that is the point — it
    proves the adapter's writes are visible cross-connection on live
    Postgres), unlike every other tests/host/ test, which reads db_conn
    inside a rollback-isolated transaction. Sharing postgres_test_dsn would
    leak a committed "AshbyCo" company/posting row into the same session
    database that other test modules' unqualified `SELECT ... FROM postings`
    queries assume is empty — verified empirically 2026-07-17 (running the
    full tests/host/ suite after adding this test broke three unrelated
    test_upsert_job.py assertions that picked up the leftover row instead of
    their own). An isolated throwaway database removes the shared state
    entirely instead of requiring every other test file to add WHERE
    clauses to defend against it.
    """
    from jobcannon.db import _companies, _jd_full, _jobs
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.engine import services

    dsn, db_name = create_throwaway_db("jobcannon_contract")
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


def test_engine_upsert_path_lands_in_postgres(wired_services):
    from jobcannon.engine.ats_scanner import _run

    svc = wired_services
    # company row must exist first (the engine's scan phase creates it via upsert_company)
    with svc.connection_factory() as conn:
        # ats_slug is required alongside ats_probe_status="hit" — m0001's
        # hit-state CHECK (ats_probe_status <> 'hit' OR (ats_platform IS NOT
        # NULL AND ats_slug IS NOT NULL)) fails closed (returns None) without
        # it; the plan's literal test omitted ats_slug (plan bug, fixed here).
        company_id = svc.upsert_company(
            conn, "AshbyCo", ats_platform="ashby", ats_slug="ashbyco", ats_probe_status="hit"
        )
    assert isinstance(company_id, int)

    # Long enough to clear the engine's I-13 density gate (>=200 chars) and the
    # I-17 content contract, so the jd-promotion path actually runs — which
    # executes the engine's inline `SELECT jd_full FROM jobs WHERE dedup_key = ?`
    # (_run.py:1247-1249) and therefore exercises the table rewrite + HybridRow.
    desc = (
        "We are hiring a Staff Data Engineer to build our analytics platform. "
        "Responsibilities include designing pipelines, mentoring engineers, and "
        "partnering with product teams on experimentation infrastructure. "
        "Qualifications: 8+ years of data engineering, strong SQL and Python, "
        "experience with batch and streaming systems at scale."
    )
    summary: dict = {"jobs_new": 0, "errors": []}
    all_new_job_keys: list = []
    job_dict = {"title": "Staff Data Engineer", "company_source": "Ashby", "description": desc}

    with (
        svc.connection_factory() as conn_outer,
        svc.connection_factory(synchronous="NORMAL") as scan_conn,
    ):
        _run._upsert_one_ats_api_job(
            conn_outer,
            scan_conn,
            "AshbyCo",
            job_dict,
            summary,
            all_new_job_keys,
            company_id=company_id,
            ats_platform="ashby",
        )

    assert summary["errors"] == []
    assert summary["jobs_new"] == 1
    assert len(all_new_job_keys) == 1

    with svc.connection_factory() as conn:
        row = conn.execute(
            "SELECT title, company, company_id, jd_full FROM postings WHERE dedup_key = ?",
            (all_new_job_keys[0],),
        ).fetchone()
    assert row["title"] == "Staff Data Engineer"
    assert row[1] == "AshbyCo"  # positional access — HybridRow contract
    assert row["company_id"] == company_id
    assert row["jd_full"] == desc  # promoted via svc.set_jd_full through the engine path


def test_optional_hooks_all_default_none(wired_services):
    svc = wired_services
    for hook in (
        "score_and_persist_job",
        "enrich_job",
        "run_heal_pass",
        "find_careers_url",
        "scrape_careers_page",
        "run_homepage_discovery",
        "run_detection",
        "identity_reconcile_settings",
        "promote_ats_scheduler_batch",
        "reconcile_company_ats",
        "owner_identity_passes",
        "resolve_slug_collision",
        "prober_extensions",
    ):
        assert getattr(svc, hook) is None

"""Smoke test: run_ats_scan actually completes on Postgres end-to-end via
run_scan_task() — jobcannon.host.scan_tasks + jobcannon.host.wiring, the real
integration path scripts/run_scan_once.py and PR-10's worker both drive.

Variant shipped: NARROW-HERMETIC, not the "stub a single ATS-API posting"
variant. Stubbing the network fetch would mean monkeypatching either the
platform-scanner registry or the requests session — fragile, and liable to
silently drift from the real fetch_postings contract. Instead this test
seeds one company on the ``jobvite`` platform (NON_SCANNABLE_PLATFORMS in
jobcannon.engine.ats_registry — "registered stub, no public API"). Phase A's
own dispatch (``_scan_one_company_via_ats_api``'s
``if platform in NON_SCANNABLE_PLATFORMS`` branch, ats_scanner/_run.py) skips
run_platform_scan and sets job_dicts=[] BEFORE any network call — fully
deterministic, zero network dependency, no mocking required.

This still drives every reachable dialect-risk SQL site this PR fixes: the
three Phase A/A2/C eligibility COUNT queries (dormancy gate's make_interval,
the neutralized high-score-history clause), the Phase A SELECT (same
clauses), the company_scan_log INSERT, and the companies UPDATE that sets
last_scanned_at/jobs_found_total/consecutive_empty_scans. It also incidentally
exercises _cache_scan_result's jsonb write (last_scan_postings_json), not
covered by any existing live-Postgres test.

It does NOT drive _upsert_one_ats_api_job or produce a postings row — that
INSERT/UPDATE path is already covered live by
tests/host/test_scan_services_contract.py::test_engine_upsert_path_lands_in_postgres,
which calls the same helper directly. The NON-NEGOTIABLE assertion (the scan
completes without a dialect error, and last_scanned_at gets set) is met in
full; the company_scan_log-row assertion is included as a bonus since this
path reaches it for free.
"""

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def test_run_scan_task_completes_on_postgres_and_sets_last_scanned_at():
    from jobcannon.db.migrate import run_migrations
    from jobcannon.engine import services
    from jobcannon.host.config import HostConfig
    from jobcannon.host.scan_tasks import run_scan_task
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    dsn, db_name = create_throwaway_db("jobcannon_scan_smoke")
    try:
        run_migrations(dsn)
        init_engine_seams(HostConfig(database_url=dsn, runtime={}))
        try:
            svc = services.get_services()
            with svc.connection_factory() as conn:
                conn.execute(
                    "INSERT INTO companies "
                    "(name, name_raw, ats_platform, ats_slug, ats_probe_status, scan_enabled) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("TestCo", "TestCo", "jobvite", "testco", "hit", True),
                )
                conn.commit()

            summary = run_scan_task(company_names=["TestCo"])

            # NON-NEGOTIABLE: no dialect error (UndefinedColumn / CheckViolation /
            # UndefinedFunction) surfaced as a per-company error string.
            assert summary["errors"] == []
            assert summary["companies_scanned"] == 1

            with svc.connection_factory() as conn:
                company_row = conn.execute(
                    "SELECT last_scanned_at, jobs_found_total, consecutive_empty_scans, "
                    "last_scan_postings_json FROM companies WHERE name = ?",
                    ("TestCo",),
                ).fetchone()
                log_row = conn.execute(
                    "SELECT jobs_found FROM company_scan_log WHERE company_id = "
                    "(SELECT id FROM companies WHERE name = ?)",
                    ("TestCo",),
                ).fetchone()

            # NON-NEGOTIABLE: last_scanned_at gets set.
            assert company_row["last_scanned_at"] is not None
            assert company_row["jobs_found_total"] == 0
            assert company_row["consecutive_empty_scans"] == 1
            # Bonus: this path reaches company_scan_log and the jsonb cache write
            # for free (both untested live-on-Postgres before this PR).
            assert log_row is not None
            assert log_row["jobs_found"] == 0
            assert company_row["last_scan_postings_json"] == []
        finally:
            teardown_engine_seams()
    finally:
        drop_throwaway_db(db_name)

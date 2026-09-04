"""Tests for jobcannon.engine.careers_crawler._persistence (ledger L-0465).

Drives ``_upsert_and_log`` / ``_update_timestamp_on_error`` against a real
on-disk sqlite3 DB, matching the ``tests/engine/`` convention (see
``tests/engine/helpers/ats_scan_services.py``, ``tests/engine/
test_ats_scanner_run.py``) of running engine SQL directly with no
compat.py translation layer. The schema here is
``tests/engine/helpers/ats_scan_services.py``'s minimal companies/jobs/
company_scan_log tables plus the two ``company_scan_log`` columns m0023
(this row's sibling migration) adds (``source``, ``failure_reason`` --
``companies.ats_link_discovery_last_at`` is also added by m0023 but no
code in this ledger row reads or writes it, so the test schema omits it),
plus ``companies.careers_crawl_tier`` (m0029, public #347) -- ``
_upsert_and_log``'s companies UPDATE now writes it (see ``tier_used``
below).

``record_scan_outcome`` is wired to the REAL
``jobcannon.engine.ats_scanner._scan_log.record_scan_outcome`` (ledger
L-0077, already landed) rather than a fake, since this port's whole point
is exercising that already-landed writer's first live caller end-to-end.
``upsert_job`` stays the helper's fake (real INSERT against the minimal
``jobs`` table) -- the real ``jobcannon.db._jobs.upsert_job`` is
host/psycopg-layer and out of the engine test harness's reach, same as
every other ``tests/engine/`` test.

carried_files is [] for L-0465 (private has no test module for
_persistence.py either -- job_finder's careers_crawler tests exercise it
only indirectly through crawl_careers_batch integration tests, which are
a later unit's scope), so every test here is new.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobcannon.engine import services
from jobcannon.engine.ats_scanner._scan_log import record_scan_outcome
from jobcannon.engine.careers_crawler._bench_predicate import BENCH_CRAWLER_SOURCE
from jobcannon.engine.careers_crawler._persistence import (
    _job_identity_conflicts,
    _update_timestamp_on_error,
    _upsert_and_log,
)

from tests.engine.helpers.ats_scan_services import (
    create_scan_schema,
    make_scan_services,
    open_connection,
)


@pytest.fixture
def crawler_db_path(tmp_path):
    """A real on-disk sqlite3 DB with the minimal scan schema plus m0023's
    two company_scan_log columns, and a ScanServices bundle (backed by that
    same DB, record_scan_outcome wired to the real L-0077 writer) registered
    for the test."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    create_scan_schema(conn)
    conn.execute("ALTER TABLE company_scan_log ADD COLUMN source TEXT")
    conn.execute("ALTER TABLE company_scan_log ADD COLUMN failure_reason TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN careers_crawl_tier TEXT")
    conn.commit()
    conn.close()

    services.set_services(make_scan_services(str(db_path), record_scan_outcome=record_scan_outcome))
    yield str(db_path)


def _insert_company(path: str, *, name: str = "Acme Corp") -> int:
    with open_connection(path) as conn:
        cur = conn.execute("INSERT INTO companies (name) VALUES (?)", (name,))
        conn.commit()
        return cur.lastrowid


def test_success_path_upserts_jobs_and_logs_outcome(crawler_db_path):
    company_id = _insert_company(crawler_db_path)
    summary = {"jobs_found": 0, "jobs_new": 0, "errors": [], "companies_crawled": 0}
    all_new_job_keys: list[str] = []

    _upsert_and_log(
        jobs=[
            {"title": "Software Engineer", "url": "https://acme.example/1", "location": "Remote"},
            {"title": "Data Scientist", "url": "https://acme.example/2", "location": "Remote"},
        ],
        company_id=company_id,
        company_name="Acme Corp",
        now="2026-09-03T00:00:00",
        summary=summary,
        all_new_job_keys=all_new_job_keys,
        tier_used="static",
    )

    assert summary["jobs_found"] == 2
    assert summary["jobs_new"] == 2
    assert summary["companies_crawled"] == 1
    assert len(all_new_job_keys) == 2

    with open_connection(crawler_db_path) as conn:
        jobs = conn.execute("SELECT title FROM jobs WHERE company_id = ?", (company_id,)).fetchall()
        assert len(jobs) == 2

        company = conn.execute(
            "SELECT careers_crawl_last_at, last_scanned_at, careers_crawl_tier, jobs_found_total "
            "FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        assert company["careers_crawl_last_at"] == "2026-09-03T00:00:00"
        assert company["last_scanned_at"] == "2026-09-03T00:00:00"
        assert company["careers_crawl_tier"] == "static"
        assert company["jobs_found_total"] == 2

        # jobs_matched/jobs_new are NOT selected here: this test's schema
        # (matching m0023's actual scope) carries no such columns, and
        # record_scan_outcome's NULL-omission + present-column rule means
        # those kwargs are silently dropped rather than erroring -- see
        # jobcannon/engine/ats_scanner/_scan_log.py's module docstring.
        log_row = conn.execute(
            "SELECT company_id, source, jobs_found, failure_reason "
            "FROM company_scan_log WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        assert log_row["source"] == BENCH_CRAWLER_SOURCE == "careers_crawler"
        assert log_row["jobs_found"] == 2
        assert log_row["failure_reason"] is None


def test_failure_reason_persisted_on_zero_hit(crawler_db_path):
    company_id = _insert_company(crawler_db_path)
    summary = {"jobs_found": 0, "jobs_new": 0, "errors": [], "companies_crawled": 0}

    _upsert_and_log(
        jobs=[],
        company_id=company_id,
        company_name="Acme Corp",
        now="2026-09-03T00:00:00",
        summary=summary,
        all_new_job_keys=[],
        tier_used="static",
        failure_reason="zero_jobs",
    )

    with open_connection(crawler_db_path) as conn:
        log_row = conn.execute(
            "SELECT failure_reason, jobs_found FROM company_scan_log WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        assert log_row["failure_reason"] == "zero_jobs"
        assert log_row["jobs_found"] == 0


def test_identity_conflict_drops_job_silently(crawler_db_path):
    company_id = _insert_company(crawler_db_path, name="Acme Corp")
    summary = {"jobs_found": 0, "jobs_new": 0, "errors": [], "companies_crawled": 0}

    _upsert_and_log(
        jobs=[
            {
                "title": "Software Engineer",
                "url": "https://acme.example/1",
                "hiring_organization": "Totally Different Company",
            }
        ],
        company_id=company_id,
        company_name="Acme Corp",
        now="2026-09-03T00:00:00",
        summary=summary,
        all_new_job_keys=[],
        tier_used="static",
    )

    # jobs_found still counts the scraped total (I-333 identity gate drops
    # the job from the upsert, not from the crawl-yield count).
    assert summary["jobs_found"] == 1
    assert summary["jobs_new"] == 0
    with open_connection(crawler_db_path) as conn:
        jobs = conn.execute("SELECT title FROM jobs WHERE company_id = ?", (company_id,)).fetchall()
        assert jobs == []


def test_job_identity_conflicts_true_when_org_slug_mismatches():
    scraped_job = {"hiring_organization": "Beta Industries"}
    assert _job_identity_conflicts(scraped_job, "Acme Corp") is True


def test_job_identity_conflicts_false_when_no_evidence():
    assert _job_identity_conflicts({}, "Acme Corp") is False


def test_record_scan_outcome_unset_is_fail_open(crawler_db_path):
    """svc.record_scan_outcome is optional (L-0465) -- when unwired, the
    companies UPDATE still happens and no exception propagates; only the
    company_scan_log row is skipped."""
    company_id = _insert_company(crawler_db_path)
    services.set_services(make_scan_services(crawler_db_path, record_scan_outcome=None))
    summary = {"jobs_found": 0, "jobs_new": 0, "errors": [], "companies_crawled": 0}

    _upsert_and_log(
        jobs=[],
        company_id=company_id,
        company_name="Acme Corp",
        now="2026-09-03T00:00:00",
        summary=summary,
        all_new_job_keys=[],
        tier_used="static",
    )

    assert summary["companies_crawled"] == 1
    with open_connection(crawler_db_path) as conn:
        company = conn.execute(
            "SELECT careers_crawl_last_at FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert company["careers_crawl_last_at"] == "2026-09-03T00:00:00"
        rows = conn.execute(
            "SELECT * FROM company_scan_log WHERE company_id = ?", (company_id,)
        ).fetchall()
        assert rows == []


def test_update_timestamp_on_error(crawler_db_path):
    company_id = _insert_company(crawler_db_path)

    _update_timestamp_on_error(company_id, "2026-09-03T01:00:00")

    with open_connection(crawler_db_path) as conn:
        company = conn.execute(
            "SELECT careers_crawl_last_at FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        assert company["careers_crawl_last_at"] == "2026-09-03T01:00:00"

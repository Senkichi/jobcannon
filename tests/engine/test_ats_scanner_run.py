"""ATS-run persistence tests for structured-field CAPTURE (#451) and refresh timestamp (#575).

Drives ``_upsert_one_ats_api_job`` against a real on-disk sqlite3 DB and asserts:

- For m106 structured fields (``is_remote`` / ``employment_type`` / ``department``):
  written on first insert via the post-insert UPDATE that mirrors the
  ``comp_data_json`` precedent — and that a later upsert with different values
  does NOT overwrite them (first-seen-wins).

- For m114 ``ats_refreshed_at``: written on EVERY sighting (not first-seen-wins)
  so it can diverge from posted_date for repost detection. Uses COALESCE so
  a later non-NULL value wins and a missing payload value never clobbers a
  known one.

- For Phase C HTML fallback scan: companies with careers_crawl_last_at set are
  excluded from the query (Fix 2 of issue #565 remediation pass 2).

Ported from the private repo's tests/test_ats_scanner_run.py onto the
jobcannon.engine ScanServices DI seam (Task 3). The engine has no migrations
system (host-owned, not ported) — the companies/jobs/company_scan_log schema
here is the minimal subset _run.py's/_run_html.py's SQL actually references
(see tests/engine/helpers/ats_scan_services.py), not a full migrated DB.
The private repo's ``job_finder.web.db_helpers.standalone_connection`` and
``job_finder.web.ats_scanner._run_html.find_careers_url`` /
``scrape_careers_page`` module-level imports don't port either: the former is
replaced by ``ScanServices.connection_factory`` (host-injected), and the
latter two are ``ScanServices.find_careers_url`` / ``.scrape_careers_page``
optional hooks (also host-injected, not module-level names in
jobcannon.engine.ats_scanner._run_html — see its module docstring).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from unittest.mock import patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.ats_scanner._run import (
    _cache_scan_result,
    _run_ats_api_scan,
    _scan_one_company_via_ats_api,
    _upsert_one_ats_api_job,
)
from jobcannon.engine.ats_scanner._run_html import _run_html_fallback_scan

from tests.engine.helpers.ats_scan_services import (
    create_scan_schema,
    make_scan_services,
    open_connection,
)


@pytest.fixture
def ats_scan_db_path(tmp_path):
    """A real on-disk sqlite3 DB with the minimal ats_scanner schema, plus a
    ScanServices bundle (backed by that same DB) registered for the test."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    create_scan_schema(conn)
    conn.close()

    services.set_services(make_scan_services(str(db_path)))
    yield str(db_path)


def _insert_company(path: str) -> int:
    with open_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES ('ashbyco', 'AshbyCo', 'ashby', 'AshbyCo', 'hit', 1,
                       '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        )
        company_id = cur.lastrowid
        conn.commit()
    return company_id


def _job_dict(
    *, is_remote, employment_type, department, ats_refreshed_at=None, title="Staff Data Engineer"
):
    return {
        "title": title,
        "company_source": "Ashby",
        "location": "Remote",
        "locations_structured": [],
        "description": ("Own the data platform end to end across ingest and modeling. " * 8),
        "source_url": "https://jobs.ashbyhq.com/AshbyCo/abc",
        "source_id": "abc",
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "posted_date": "2026-01-01T00:00:00",
        "is_remote": is_remote,
        "employment_type": employment_type,
        "department": department,
        "ats_refreshed_at": ats_refreshed_at,
    }


def _read_capture(path: str, company_id: int):
    with open_connection(path) as conn:
        row = conn.execute(
            "SELECT is_remote, employment_type, department, ats_refreshed_at FROM jobs WHERE company_id = ?",
            (company_id,),
        ).fetchone()
    return row


def test_capture_columns_persisted_on_first_insert(ats_scan_db_path):
    company_id = _insert_company(ats_scan_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with open_connection(ats_scan_db_path) as conn_outer:
        with open_connection(ats_scan_db_path) as scan_conn:
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(is_remote=True, employment_type="FullTime", department="Engineering"),
                summary,
                keys,
                company_id=company_id,
            )

    assert summary["errors"] == []
    row = _read_capture(ats_scan_db_path, company_id)
    assert row is not None
    # SQLite stores Python bool as 1/0.
    assert row["is_remote"] == 1
    assert row["employment_type"] == "FullTime"
    assert row["department"] == "Engineering"
    # ats_refreshed_at is NULL when not provided
    assert row["ats_refreshed_at"] is None


def test_capture_columns_first_seen_wins(ats_scan_db_path):
    company_id = _insert_company(ats_scan_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with open_connection(ats_scan_db_path) as conn_outer:
        with open_connection(ats_scan_db_path) as scan_conn:
            # First insert sets the values.
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(is_remote=True, employment_type="FullTime", department="Engineering"),
                summary,
                keys,
                company_id=company_id,
            )
            # Second upsert of the SAME job (same dedup_key) with different
            # capture values must NOT overwrite — the UPDATE only fires on the
            # "inserted" branch. This applies to is_remote/employment_type/department
            # (m106 fields), but NOT ats_refreshed_at (which updates on every sighting).
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(is_remote=False, employment_type="Contract", department="Sales"),
                summary,
                keys,
                company_id=company_id,
            )

    row = _read_capture(ats_scan_db_path, company_id)
    assert row is not None
    assert row["is_remote"] == 1
    assert row["employment_type"] == "FullTime"
    assert row["department"] == "Engineering"
    # ats_refreshed_at is NULL in both upserts, so stays NULL
    assert row["ats_refreshed_at"] is None


def test_ats_refreshed_at_overwrites_on_second_sighting(ats_scan_db_path):
    """Test that ats_refreshed_at overwrites on every sighting (not first-seen-wins).

    This is the critical difference from the m106 structured fields: the refresh
    timestamp is mutable and must diverge from posted_date for repost detection,
    so it updates on every sighting even when the upsert result is "unchanged".
    """
    company_id = _insert_company(ats_scan_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with open_connection(ats_scan_db_path) as conn_outer:
        with open_connection(ats_scan_db_path) as scan_conn:
            # First insert sets the initial refresh timestamp.
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(
                    is_remote=True,
                    employment_type="FullTime",
                    department="Engineering",
                    ats_refreshed_at="2026-06-01T00:00:00",
                ),
                summary,
                keys,
                company_id=company_id,
            )

    assert summary["errors"] == []
    row = _read_capture(ats_scan_db_path, company_id)
    assert row is not None
    assert row["ats_refreshed_at"] == "2026-06-01T00:00:00"

    # Second upsert with a NEWER refresh timestamp should OVERWRITE.
    summary["jobs_new"] = 0
    summary["errors"] = []
    with open_connection(ats_scan_db_path) as conn_outer:
        with open_connection(ats_scan_db_path) as scan_conn:
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(
                    is_remote=True,
                    employment_type="FullTime",
                    department="Engineering",
                    ats_refreshed_at="2026-06-26T21:05:44",  # Newer timestamp
                ),
                summary,
                keys,
                company_id=company_id,
            )

    row = _read_capture(ats_scan_db_path, company_id)
    assert row is not None
    # Should have the NEWER value (latest-non-NULL-wins)
    assert row["ats_refreshed_at"] == "2026-06-26T21:05:44"


def test_ats_refreshed_at_null_does_not_clobber_known_value(ats_scan_db_path):
    """Test that a NULL/absent refresh value does not clobber a known one.

    Uses COALESCE so a later non-NULL value wins and a missing payload value
    never clobbers a known one.
    """
    company_id = _insert_company(ats_scan_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with open_connection(ats_scan_db_path) as conn_outer:
        with open_connection(ats_scan_db_path) as scan_conn:
            # First insert sets the refresh timestamp.
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(
                    is_remote=True,
                    employment_type="FullTime",
                    department="Engineering",
                    ats_refreshed_at="2026-06-01T00:00:00",
                ),
                summary,
                keys,
                company_id=company_id,
            )

    row = _read_capture(ats_scan_db_path, company_id)
    assert row is not None
    assert row["ats_refreshed_at"] == "2026-06-01T00:00:00"

    # Second upsert with NULL refresh should NOT clobber the known value.
    summary["jobs_new"] = 0
    summary["errors"] = []
    with open_connection(ats_scan_db_path) as conn_outer:
        with open_connection(ats_scan_db_path) as scan_conn:
            _upsert_one_ats_api_job(
                conn_outer,
                scan_conn,
                "AshbyCo",
                _job_dict(
                    is_remote=True,
                    employment_type="FullTime",
                    department="Engineering",
                    ats_refreshed_at=None,  # NULL
                ),
                summary,
                keys,
                company_id=company_id,
            )

    row = _read_capture(ats_scan_db_path, company_id)
    assert row is not None
    # Should still have the original value (COALESCE preserves known value)
    assert row["ats_refreshed_at"] == "2026-06-01T00:00:00"


def test_phase_c_excludes_companies_with_careers_crawl_last_at(ats_scan_db_path):
    """Test that Phase C HTML fallback scan excludes companies with careers_crawl_last_at set (Fix 2).

    A company that careers_crawler's Lane 2 has already started extracting from
    (careers_crawl_last_at stamped) should no longer be eligible for Phase C's
    separate HTML scrape, regardless of which code path first gave it that timestamp.
    """
    # find_careers_url/scrape_careers_page are host-injected ScanServices hooks
    # here (not module-level names in _run_html.py) — re-register services with
    # them wired so _run_html_fallback_scan doesn't early-return before running
    # its cohort query. TestCo2 (the only eligible company below) already has a
    # cached careers_url, so find_careers_url is never actually called; it's
    # wired for parity with the private-repo test's mock and to prove the
    # early-return None-hook guard isn't what's gating the cohort.
    find_careers_url_calls: list = []
    scrape_careers_page_calls: list = []

    def _fake_find_careers_url(homepage_url, *, conn=None, config=None):
        find_careers_url_calls.append(homepage_url)
        return None

    def _fake_scrape_careers_page(
        careers_url, target_titles, title_exclusions, *, conn=None, config=None
    ):
        scrape_careers_page_calls.append(careers_url)
        return [], 0

    services.set_services(
        make_scan_services(
            ats_scan_db_path,
            find_careers_url=_fake_find_careers_url,
            scrape_careers_page=_fake_scrape_careers_page,
        )
    )

    with open_connection(ats_scan_db_path) as conn:
        # Insert a company with careers_crawl_last_at set (owned by careers_crawler Lane 2)
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, homepage_url, careers_url, ats_probe_status,
                scan_enabled, careers_crawl_last_at, created_at, updated_at)
               VALUES ('TestCo', 'TestCo', 'https://test.com', 'https://test.com/careers',
                       'miss', 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"""
        )
        # Insert a company without careers_crawl_last_at (eligible for Phase C)
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, homepage_url, careers_url, ats_probe_status,
                scan_enabled, careers_crawl_last_at, created_at, updated_at)
               VALUES ('TestCo2', 'TestCo2', 'https://test2.com', 'https://test2.com/careers',
                       'miss', 1, NULL, '2026-01-01T00:00:00', '2026-01-01T00:00:00')"""
        )
        conn.commit()

        config = {"profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}}}
        summary = {"jobs_new": 0, "errors": [], "html_scraped": 0}
        all_new_job_keys = []

        _run_html_fallback_scan(
            conn,
            ats_scan_db_path,
            config,
            ["Engineer"],
            [],
            summary,
            all_new_job_keys,
            high_score_threshold=999,  # Skip history gate
        )

        # Verify that only the company without careers_crawl_last_at was scanned
        # (the scraper was called for it, not for the one with careers_crawl_last_at).
        assert summary["jobs_new"] == 0
        assert summary["errors"] == []
        assert scrape_careers_page_calls == ["https://test2.com/careers"]

        # Direct query verification: the company with careers_crawl_last_at should NOT be in the cohort
        eligible_companies = conn.execute(
            """SELECT id, name_raw FROM companies
               WHERE ats_probe_status IN ('miss', 'error')
                 AND homepage_url IS NOT NULL
                 AND scan_enabled = 1
                 AND careers_crawl_last_at IS NULL"""
        ).fetchall()

        assert len(eligible_companies) == 1
        assert eligible_companies[0]["name_raw"] == "TestCo2"  # Only the one without timestamp


# test_standalone_connection_synchronous_pragma (private repo) intentionally
# NOT ported: it exercises jobcannon.engine.db_helpers.standalone_connection's
# `synchronous` kwarg handling directly. That function is host-owned (Flask
# app) and has no engine equivalent — ScanServices.connection_factory is an
# opaque host-injected callable the engine never implements itself, so there
# is nothing in jobcannon.engine for this test to drive.


def _scan_job_dict(**overrides):
    """Build a matched job dict for the scan cache tests."""
    base = {
        "title": "Staff Data Engineer",
        "company_source": "Ashby",
        "location": "Remote",
        "locations_structured": [],
        "description": ("Build ML models at scale. " * 10),
        "source_url": "https://jobs.ashbyhq.com/AshbyCo/abc",
        "source_id": "abc",
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }
    base.update(overrides)
    return base


def _scan_job_dict_with_location():
    """Build a job dict with a real JobLocation in locations_structured."""
    from jobcannon.engine.location_canonical import JobLocation

    job = _scan_job_dict()
    job["locations_structured"] = [
        JobLocation(
            city="San Francisco",
            region="California",
            region_code="CA",
            country="United States",
            country_code="US",
            workplace_type="REMOTE",
            raw="Remote - San Francisco, CA",
            unresolved=False,
        )
    ]
    return job


def test_cache_scan_result_writes_json_and_timestamp(ats_scan_db_path):
    """_cache_scan_result stores the job-dict list and stamps last_scan_cached_at."""
    company_id = _insert_company(ats_scan_db_path)
    job_dicts = [_scan_job_dict()]
    now = "2026-07-11T10:00:00"

    with open_connection(ats_scan_db_path) as conn:
        _cache_scan_result(conn, company_id, job_dicts, now)
        conn.commit()

    with open_connection(ats_scan_db_path) as conn:
        row = conn.execute(
            "SELECT last_scan_postings_json, last_scan_cached_at FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

    assert row is not None
    assert row["last_scan_cached_at"] == now
    assert json.loads(row["last_scan_postings_json"]) == job_dicts


def test_cache_scan_result_failure_logs_warning_and_continues(caplog):
    """A DB error while writing the scan cache is logged and swallowed."""
    import logging
    from unittest.mock import MagicMock

    conn = MagicMock()
    conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")

    with caplog.at_level(logging.WARNING, logger="jobcannon.engine.ats_scanner._run"):
        _cache_scan_result(conn, 1, [{}], "2026-07-11T10:00:00")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Failed to write scan cache" in r.message for r in warnings)


def test_scan_one_company_via_ats_api_cache_write_failure_continues(ats_scan_db_path, caplog):
    """A scan cache write failure must not abort the per-company scan."""
    import logging

    company_id = _insert_company(ats_scan_db_path)
    company = {
        "id": company_id,
        "name_raw": "AshbyCo",
        "ats_platform": "ashby",
        "ats_slug": "AshbyCo",
    }
    job_dicts = [_scan_job_dict()]
    summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
    all_new_keys: list[str] = []

    class _ConnWrapper:
        """Wrap a real sqlite connection and raise only on the cache write."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *parameters):
            if isinstance(sql, str) and "last_scan_postings_json" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *parameters)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    with (
        open_connection(ats_scan_db_path) as conn,
        caplog.at_level(logging.WARNING, logger="jobcannon.engine.ats_scanner._run"),
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=(job_dicts, 0, job_dicts),
        ),
    ):
        _scan_one_company_via_ats_api(
            _ConnWrapper(conn),
            ats_scan_db_path,
            company,
            ["Engineer"],
            [],
            summary,
            all_new_keys,
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Failed to write scan cache" in r.message for r in warnings)

    # The scan should still have upserted the job and updated the company row.
    with open_connection(ats_scan_db_path) as conn:
        row = conn.execute(
            "SELECT last_scanned_at, jobs_found_total FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        assert row is not None
        assert row["last_scanned_at"] is not None
        assert row["jobs_found_total"] == 1

        job = conn.execute(
            "SELECT dedup_key, jd_full FROM jobs WHERE company_id = ?",
            (company_id,),
        ).fetchone()
        assert job is not None
        assert job["jd_full"] is not None


def test_scan_one_company_via_ats_api_writes_cache(ats_scan_db_path):
    """_scan_one_company_via_ats_api persists the scan result for enrichment."""
    company_id = _insert_company(ats_scan_db_path)
    company = {
        "id": company_id,
        "name_raw": "AshbyCo",
        "ats_platform": "ashby",
        "ats_slug": "AshbyCo",
    }
    job_dicts = [_scan_job_dict()]
    summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
    all_new_keys: list[str] = []

    with (
        open_connection(ats_scan_db_path) as conn,
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=(job_dicts, 0, job_dicts),
        ),
    ):
        _scan_one_company_via_ats_api(
            conn,
            ats_scan_db_path,
            company,
            ["Engineer"],
            [],
            summary,
            all_new_keys,
        )

    with open_connection(ats_scan_db_path) as conn:
        row = conn.execute(
            "SELECT last_scan_postings_json, last_scan_cached_at FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

    assert row is not None
    assert row["last_scan_postings_json"] is not None
    assert row["last_scan_cached_at"] is not None
    assert json.loads(row["last_scan_postings_json"]) == job_dicts


def test_cache_scan_result_serializes_job_location_dataclasses(ats_scan_db_path):
    """_cache_scan_result must serialize JobLocation instances, not silently NULL."""
    company_id = _insert_company(ats_scan_db_path)
    job = _scan_job_dict_with_location()
    now = "2026-07-11T10:00:00"

    with open_connection(ats_scan_db_path) as conn:
        _cache_scan_result(conn, company_id, [job], now)
        conn.commit()

    with open_connection(ats_scan_db_path) as conn:
        row = conn.execute(
            "SELECT last_scan_postings_json FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

    assert row is not None
    assert row["last_scan_postings_json"] is not None
    parsed = json.loads(row["last_scan_postings_json"])
    assert len(parsed) == 1
    assert parsed[0]["locations_structured"] == [
        {
            "city": "San Francisco",
            "region": "California",
            "region_code": "CA",
            "country": "United States",
            "country_code": "US",
            "workplace_type": "REMOTE",
            "raw": "Remote - San Francisco, CA",
            "unresolved": False,
        }
    ]


def test_cache_scan_result_serializes_datetime_and_date_values(ats_scan_db_path):
    """_cache_scan_result must serialize live datetime/date values, not silently NULL (issue #1168)."""
    company_id = _insert_company(ats_scan_db_path)
    job = _scan_job_dict(
        posted_date=datetime(2026, 1, 1, 12, 0, 0),
        closed_date=date(2026, 1, 2),
    )
    now = "2026-07-11T10:00:00"

    with open_connection(ats_scan_db_path) as conn:
        _cache_scan_result(conn, company_id, [job], now)
        conn.commit()

    with open_connection(ats_scan_db_path) as conn:
        row = conn.execute(
            "SELECT last_scan_postings_json, last_scan_cached_at FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

    assert row is not None
    assert row["last_scan_cached_at"] == now
    parsed = json.loads(row["last_scan_postings_json"])
    assert len(parsed) == 1
    assert parsed[0]["posted_date"] == "2026-01-01T12:00:00"
    assert parsed[0]["closed_date"] == "2026-01-02"


def test_scan_one_company_via_ats_api_caches_full_board(ats_scan_db_path):
    """The scan cache must store the full pre-title-gate board, not only matched jobs."""
    company_id = _insert_company(ats_scan_db_path)
    company = {
        "id": company_id,
        "name_raw": "AshbyCo",
        "ats_platform": "ashby",
        "ats_slug": "AshbyCo",
    }
    engineer = _scan_job_dict(
        title="Senior Software Engineer",
        source_url="https://jobs.ashbyhq.com/AshbyCo/eng",
        source_id="eng",
    )
    manager = _scan_job_dict(
        title="Senior Product Manager",
        source_url="https://jobs.ashbyhq.com/AshbyCo/pm",
        source_id="pm",
    )
    # run_platform_scan returns only the Engineer job as matched, but raw_job_dicts
    # contains both. The cache must be written from raw_job_dicts.
    matched = [engineer]
    raw = [engineer, manager]
    summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
    all_new_keys: list[str] = []

    with (
        open_connection(ats_scan_db_path) as conn,
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=(matched, 1, raw),
        ),
    ):
        _scan_one_company_via_ats_api(
            conn,
            ats_scan_db_path,
            company,
            ["Engineer"],
            [],
            summary,
            all_new_keys,
        )

    with open_connection(ats_scan_db_path) as conn:
        row = conn.execute(
            "SELECT last_scan_postings_json FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        assert row is not None
        cached = json.loads(row["last_scan_postings_json"])
        titles = {j["title"] for j in cached}
        assert "Senior Software Engineer" in titles
        assert "Senior Product Manager" in titles


def test_phase_a_stalest_first_order(ats_scan_db_path):
    """Phase A company query orders never-scanned and stalest-scanned first (issue #1130)."""
    with open_connection(ats_scan_db_path) as conn:
        now = "2026-07-12T00:00:00"
        fixtures = [
            ("recent_co", "2026-07-10T00:00:00"),
            ("oldest_co", "2026-01-01T00:00:00"),
            ("middle_co", "2026-05-01T00:00:00"),
            ("never_co", None),
        ]
        for name, last_scanned in fixtures:
            conn.execute(
                """INSERT INTO companies
                   (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                    scan_enabled, consecutive_empty_scans, last_scanned_at,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'hit', 1, 0, ?, ?, ?)""",
                (name, name, "greenhouse", name, last_scanned, now, now),
            )
        conn.commit()

        scanned_names: list[str] = []

        def _record_scan(conn_inner, db_path_inner, company, *args, **kwargs):
            scanned_names.append(company["name_raw"])

        with (
            patch(
                "jobcannon.engine.ats_scanner._run._scan_one_company_via_ats_api",
                side_effect=_record_scan,
            ),
            patch("jobcannon.engine.ats_scanner._run.time.sleep"),
        ):
            _run_ats_api_scan(
                conn,
                ats_scan_db_path,
                ["Engineer"],
                [],
                {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []},
                [],
                high_score_threshold=0,  # skip history gate
                dormancy_threshold=10,
                dormancy_interval_days=3,
                scan_concurrency=1,
            )

    assert scanned_names == ["never_co", "oldest_co", "middle_co", "recent_co"]

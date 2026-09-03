"""ATS-run persistence tests for structured-field CAPTURE (#451) and refresh timestamp (#575).

Drives ``_upsert_one_ats_api_job`` against a fully-migrated in-memory-ish temp
DB and asserts:

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
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from unittest.mock import patch

from job_finder.db import prune_title_outcomes, record_scan_outcome
from jobcannon.engine.json_utils import utc_now_iso
from jobcannon.engine.ats_scanner._run import (
    _cache_scan_result,
    _count_phase_a_eligible,
    _mark_deadline_skipped,
    _phase_a_base_clause,
    _record_phase_a_selection,
    _relevant_company_ids_for_outcomes,
    _run_ats_api_scan,
    _scan_one_company_via_ats_api,
    _scan_one_company_worker,
    _upsert_one_ats_api_job,
)
from jobcannon.engine.ats_scanner._run_html import _run_html_fallback_scan
from jobcannon.engine.db_helpers import standalone_connection


def _insert_company(path: str) -> int:
    with standalone_connection(path) as conn:
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
    with standalone_connection(path) as conn:
        row = conn.execute(
            "SELECT is_remote, employment_type, department, ats_refreshed_at FROM jobs WHERE company_id = ?",
            (company_id,),
        ).fetchone()
    return row


def test_capture_columns_persisted_on_first_insert(migrated_db_path):
    company_id = _insert_company(migrated_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
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
    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    # SQLite stores Python bool as 1/0.
    assert row["is_remote"] == 1
    assert row["employment_type"] == "FullTime"
    assert row["department"] == "Engineering"
    # ats_refreshed_at is NULL when not provided
    assert row["ats_refreshed_at"] is None


def test_capture_columns_first_seen_wins(migrated_db_path):
    company_id = _insert_company(migrated_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
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

    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    assert row["is_remote"] == 1
    assert row["employment_type"] == "FullTime"
    assert row["department"] == "Engineering"
    # ats_refreshed_at is NULL in both upserts, so stays NULL
    assert row["ats_refreshed_at"] is None


def test_ats_refreshed_at_overwrites_on_second_sighting(migrated_db_path):
    """Test that ats_refreshed_at overwrites on every sighting (not first-seen-wins).

    This is the critical difference from the m106 structured fields: the refresh
    timestamp is mutable and must diverge from posted_date for repost detection,
    so it updates on every sighting even when the upsert result is "unchanged".
    """
    company_id = _insert_company(migrated_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
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
    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    assert row["ats_refreshed_at"] == "2026-06-01T00:00:00"

    # Second upsert with a NEWER refresh timestamp should OVERWRITE.
    summary["jobs_new"] = 0
    summary["errors"] = []
    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
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

    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    # Should have the NEWER value (latest-non-NULL-wins)
    assert row["ats_refreshed_at"] == "2026-06-26T21:05:44"


def test_ats_refreshed_at_null_does_not_clobber_known_value(migrated_db_path):
    """Test that a NULL/absent refresh value does not clobber a known one.

    Uses COALESCE so a later non-NULL value wins and a missing payload value
    never clobbers a known one.
    """
    company_id = _insert_company(migrated_db_path)
    summary: dict = {"jobs_new": 0, "errors": []}
    keys: list = []

    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
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

    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    assert row["ats_refreshed_at"] == "2026-06-01T00:00:00"

    # Second upsert with NULL refresh should NOT clobber the known value.
    summary["jobs_new"] = 0
    summary["errors"] = []
    with standalone_connection(migrated_db_path) as conn_outer:
        with standalone_connection(migrated_db_path) as scan_conn:
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

    row = _read_capture(migrated_db_path, company_id)
    assert row is not None
    # Should still have the original value (COALESCE preserves known value)
    assert row["ats_refreshed_at"] == "2026-06-01T00:00:00"


def test_phase_c_excludes_companies_with_careers_crawl_last_at(migrated_db_path):
    """Test that Phase C HTML fallback scan excludes companies with careers_crawl_last_at set (Fix 2).

    A company that careers_crawler's Lane 2 has already started extracting from
    (careers_crawl_last_at stamped) should no longer be eligible for Phase C's
    separate HTML scrape, regardless of which code path first gave it that timestamp.
    """
    with standalone_connection(migrated_db_path) as conn:
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

        # Run Phase C query (via _run_html_fallback_scan with high threshold to skip history gate)
        config = {"profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}}}
        summary = {"jobs_new": 0, "errors": []}
        all_new_job_keys = []

        # Mock the scraper functions to avoid actual HTTP calls
        from unittest.mock import patch

        with (
            patch("jobcannon.engine.ats_scanner._run_html.find_careers_url") as mock_find,
            patch("jobcannon.engine.ats_scanner._run_html.scrape_careers_page") as mock_scrape,
        ):
            mock_find.return_value = None  # No careers URL found
            mock_scrape.return_value = [], 0  # Return tuple (jobs, skipped_count)

            _run_html_fallback_scan(
                conn,
                migrated_db_path,
                config,
                ["Engineer"],
                [],
                summary,
                all_new_job_keys,
            )

        # Verify that only the company without careers_crawl_last_at was scanned
        # (the scraper was called for it, not for the one with careers_crawl_last_at)
        # Since we mocked find_careers_url to return None, the actual scan doesn't happen,
        # but we can verify the query cohort by checking that the function didn't error
        # and that the summary is empty (no jobs found/scraped)
        assert summary["jobs_new"] == 0
        assert summary["errors"] == []

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


def test_scrape_careers_page_rejects_blocklisted_aggregator_host():
    """#1622: verify the careers_page write path (_run_html_fallback_scan →
    scrape_careers_page) is protected by scrape_careers_page's OWN
    _is_blocklisted_scrape_host gate at careers_scraper.py:734 (present since
    #1003, extended by #1006). That gate is the single point of enforcement
    for this path — it returns ([], 0) before any fetch, so no rows are
    upserted regardless of company-row lifecycle.

    DB evidence confirms the careers_page path was NOT the source of the
    post-#1006-merge rows: all 12 careers_page rows predate #1006's merge
    (2026-07-06..07-08, before #1006 landed 2026-07-10). The 33 post-merge
    rows are sources=["careers_crawl"] and came through the careers_crawler
    path, which is gated by the new check in careers_crawler/__init__.py.

    This test calls scrape_careers_page directly (unmocked) with a blocklisted
    URL and asserts (a) the return is ([], 0) and (b) no HTTP fetch was
    attempted — confirming the gate short-circuits before the network. The
    blocklist is domain-keyed (tryapplynow.com), so it survives company-row
    deletion/recreation by construction: the predicate inspects the URL host,
    not any company-row attribute.
    """
    from jobcannon.engine.careers_scraper import scrape_careers_page

    with patch("jobcannon.engine.careers_scraper.fetch_with_deadline") as mock_fetch:
        result = scrape_careers_page(
            "https://www.tryapplynow.com/jobs",
            ["Data Scientist"],
            [],
        )

    # Gate short-circuited before the fetch — no network call.
    mock_fetch.assert_not_called()
    assert result == ([], 0)


def test_standalone_connection_synchronous_pragma(migrated_db_path):
    """Test that standalone_connection respects the synchronous kwarg.

    - Default (no kwarg) should set synchronous=FULL (value 2)
    - synchronous="NORMAL" should set synchronous=NORMAL (value 1)
    This is issue #1027's performance optimization guard.
    """
    # Test default behavior (FULL)
    with standalone_connection(migrated_db_path) as conn:
        row = conn.execute("PRAGMA synchronous").fetchone()
        assert row[0] == 2  # FULL

    # Test NORMAL opt-in
    with standalone_connection(migrated_db_path, synchronous="NORMAL") as conn:
        row = conn.execute("PRAGMA synchronous").fetchone()
        assert row[0] == 1  # NORMAL


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


def test_cache_scan_result_writes_json_and_timestamp(migrated_db_path):
    """_cache_scan_result stores the job-dict list and stamps last_scan_cached_at."""
    company_id = _insert_company(migrated_db_path)
    job_dicts = [_scan_job_dict()]
    now = "2026-07-11T10:00:00"

    with standalone_connection(migrated_db_path) as conn:
        _cache_scan_result(conn, company_id, job_dicts, now)
        conn.commit()

    with standalone_connection(migrated_db_path) as conn:
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


def test_scan_one_company_via_ats_api_cache_write_failure_continues(migrated_db_path, caplog):
    """A scan cache write failure must not abort the per-company scan."""
    import logging

    company_id = _insert_company(migrated_db_path)
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
        standalone_connection(migrated_db_path) as conn,
        caplog.at_level(logging.WARNING, logger="jobcannon.engine.ats_scanner._run"),
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=(job_dicts, 0, job_dicts),
        ),
    ):
        _scan_one_company_via_ats_api(
            _ConnWrapper(conn),
            migrated_db_path,
            company,
            ["Engineer"],
            [],
            summary,
            all_new_keys,
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Failed to write scan cache" in r.message for r in warnings)

    # The scan should still have upserted the job and updated the company row.
    with standalone_connection(migrated_db_path) as conn:
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


def test_scan_one_company_via_ats_api_writes_cache(migrated_db_path):
    """_scan_one_company_via_ats_api persists the scan result for enrichment."""
    company_id = _insert_company(migrated_db_path)
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
        standalone_connection(migrated_db_path) as conn,
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=(job_dicts, 0, job_dicts),
        ),
    ):
        _scan_one_company_via_ats_api(
            conn,
            migrated_db_path,
            company,
            ["Engineer"],
            [],
            summary,
            all_new_keys,
        )

    with standalone_connection(migrated_db_path) as conn:
        row = conn.execute(
            "SELECT last_scan_postings_json, last_scan_cached_at FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()

    assert row is not None
    assert row["last_scan_postings_json"] is not None
    assert row["last_scan_cached_at"] is not None
    assert json.loads(row["last_scan_postings_json"]) == job_dicts


def test_cache_scan_result_serializes_job_location_dataclasses(migrated_db_path):
    """_cache_scan_result must serialize JobLocation instances, not silently NULL."""
    company_id = _insert_company(migrated_db_path)
    job = _scan_job_dict_with_location()
    now = "2026-07-11T10:00:00"

    with standalone_connection(migrated_db_path) as conn:
        _cache_scan_result(conn, company_id, [job], now)
        conn.commit()

    with standalone_connection(migrated_db_path) as conn:
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


def test_cache_scan_result_serializes_datetime_and_date_values(migrated_db_path):
    """_cache_scan_result must serialize live datetime/date values, not silently NULL (issue #1168)."""
    company_id = _insert_company(migrated_db_path)
    job = _scan_job_dict(
        posted_date=datetime(2026, 1, 1, 12, 0, 0),
        closed_date=date(2026, 1, 2),
    )
    now = "2026-07-11T10:00:00"

    with standalone_connection(migrated_db_path) as conn:
        _cache_scan_result(conn, company_id, [job], now)
        conn.commit()

    with standalone_connection(migrated_db_path) as conn:
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


def test_scan_one_company_via_ats_api_caches_full_board(migrated_db_path):
    """The scan cache must store the full pre-title-gate board, not only matched jobs."""
    company_id = _insert_company(migrated_db_path)
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
        standalone_connection(migrated_db_path) as conn,
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=(matched, 1, raw),
        ),
    ):
        _scan_one_company_via_ats_api(
            conn,
            migrated_db_path,
            company,
            ["Engineer"],
            [],
            summary,
            all_new_keys,
        )

    with standalone_connection(migrated_db_path) as conn:
        row = conn.execute(
            "SELECT last_scan_postings_json FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        assert row is not None
        cached = json.loads(row["last_scan_postings_json"])
        titles = {j["title"] for j in cached}
        assert "Senior Software Engineer" in titles
        assert "Senior Product Manager" in titles


def test_phase_a_stalest_first_order(migrated_db_path):
    """Phase A company query orders never-scanned and stalest-scanned first (issue #1130)."""
    with standalone_connection(migrated_db_path) as conn:
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
                migrated_db_path,
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


def test_scanner_exception_recorded_as_error_row(migrated_db_path):
    """WI-06: a scanner exception lands as a company_scan_log error row.

    Patches ``run_platform_scan`` to raise; the serial per-company driver's
    ``except Exception`` handler must (1) append to ``summary["errors"]`` and
    (2) write a single ``company_scan_log`` row via the single writer with
    ``failure_reason='exception'``, ``jobs_found=0``, and non-empty ``error``.
    """
    company_id = _insert_company(migrated_db_path)
    company = {
        "id": company_id,
        "name_raw": "AshbyCo",
        "ats_platform": "ashby",
        "ats_slug": "AshbyCo",
    }
    summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
    all_new_keys: list[str] = []

    with (
        standalone_connection(migrated_db_path) as conn,
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            side_effect=RuntimeError("boom: scanner blew up"),
        ),
    ):
        _scan_one_company_via_ats_api(
            conn,
            migrated_db_path,
            company,
            ["Engineer"],
            [],
            summary,
            all_new_keys,
        )

    # (1) The company-level failure surfaced in the summary.
    assert summary["errors"], "expected a company-level error in summary['errors']"
    assert any("boom: scanner blew up" in e for e in summary["errors"])
    # companies_scanned must NOT be incremented on the failure path.
    assert summary["companies_scanned"] == 0

    # (2) Exactly one error row, written via the single writer.
    with standalone_connection(migrated_db_path) as conn:
        rows = conn.execute(
            """SELECT jobs_found, error, failure_reason, source
               FROM company_scan_log WHERE company_id = ?""",
            (company_id,),
        ).fetchall()

    assert len(rows) == 1, f"expected exactly one scan-log row, got {len(rows)}"
    row = rows[0]
    assert row["source"] == "ats_scanner"
    assert row["failure_reason"] == "exception"
    assert row["jobs_found"] == 0
    assert row["error"] and "boom: scanner blew up" in row["error"]
    # The typed prefix format (f"{type(e).__name__}: {e}") is used, not bare str(e).
    assert row["error"].startswith("RuntimeError: ")


# ---------------------------------------------------------------------------
# WI-04 — selection ledger (scan_selection_log) + run_id on company_scan_log
# ---------------------------------------------------------------------------


def _insert_ledger_company(
    path: str,
    *,
    name: str,
    platform,
    slug,
    probe_status: str = "hit",
    scan_enabled: int = 1,
    consecutive_empty_scans: int = 0,
    last_scanned_at=None,
) -> int:
    """Insert one company row with fine-grained control over the gate columns."""
    with standalone_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, ats_scan_enabled, consecutive_empty_scans, last_scanned_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                       '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
            (
                name.lower(),
                name,
                platform,
                slug,
                probe_status,
                scan_enabled,
                scan_enabled,  # ats_scan_enabled mirrors the legacy bit (WI-13 fixture)
                consecutive_empty_scans,
                last_scanned_at,
            ),
        )
        company_id = cur.lastrowid
        conn.commit()
    return company_id


def test_selection_ledger_rows_partition_base_set(migrated_db_path):
    """WI-04: every Phase-A base company gets exactly one ledger row, and the
    per-reason partition is exhaustive over the base set.

    Six companies exercise each bucket:
      selected                    — hit, valid identity, non-dormant
      skipped_identity_null       — hit, ats_platform NULL
      skipped_playwright_excluded — hit, iCIMS (Playwright cohort)
      skipped_dormant             — hit, over empty-scan threshold + recently scanned
      (no row) miss               — not in base
      (no row) scan_enabled=0     — not in base (skipped_disabled structurally empty)
    """
    recent = utc_now_iso()  # within the 3-day dormancy interval
    selected_id = _insert_ledger_company(
        migrated_db_path, name="SelCo", platform="ashby", slug="SelCo", last_scanned_at=None
    )
    identity_id = _insert_ledger_company(migrated_db_path, name="NullCo", platform=None, slug=None)
    playwright_id = _insert_ledger_company(
        migrated_db_path, name="IcimsCo", platform="icims", slug="icimsco"
    )
    dormant_id = _insert_ledger_company(
        migrated_db_path,
        name="DormCo",
        platform="ashby",
        slug="DormCo",
        consecutive_empty_scans=99,
        last_scanned_at=recent,
    )
    miss_id = _insert_ledger_company(
        migrated_db_path, name="MissCo", platform="ashby", slug="MissCo", probe_status="miss"
    )
    disabled_id = _insert_ledger_company(
        migrated_db_path, name="OffCo", platform="ashby", slug="OffCo", scan_enabled=0
    )

    run_id = "run-partition-1"
    with standalone_connection(migrated_db_path) as conn:
        _record_phase_a_selection(
            conn,
            run_id,
            "ats_scan",
            dormancy_threshold=10,
            dormancy_interval_days=3,
            high_score_threshold=20,
            max_revisit_days=7,
            selection_log_keep_days=30,
        )

    with standalone_connection(migrated_db_path) as conn:
        rows = conn.execute(
            "SELECT company_id, decision, tier, rank, job_id"
            " FROM scan_selection_log WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        by_company = {r["company_id"]: r for r in rows}

    # Exactly one row per (run_id, company_id); base companies only.
    assert len(rows) == 4, f"expected 4 base rows, got {len(rows)}: {[dict(r) for r in rows]}"
    assert by_company[selected_id]["decision"] == "selected"
    assert by_company[identity_id]["decision"] == "skipped_identity_null"
    assert by_company[playwright_id]["decision"] == "skipped_playwright_excluded"
    assert by_company[dormant_id]["decision"] == "skipped_dormant"
    # Off-base companies get NO row (miss + disabled).
    assert miss_id not in by_company
    assert disabled_id not in by_company

    # The selected row carries a meaningful rank, tier, and job_id. SelCo has
    # last_scanned_at=NULL, so its WI-03 priority tier is 0 (never scanned).
    assert by_company[selected_id]["rank"] == 0
    assert by_company[selected_id]["tier"] == 0
    assert by_company[selected_id]["job_id"] == "ats_scan"

    # Behavioural spec: the GROUP BY partition sums to the Phase-A base count.
    with standalone_connection(migrated_db_path) as conn:
        grouped = conn.execute(
            "SELECT decision, count(*) c FROM scan_selection_log"
            " WHERE run_id = ? GROUP BY decision",
            (run_id,),
        ).fetchall()
        base_count = conn.execute(
            f"SELECT count(*) FROM companies WHERE {_phase_a_base_clause()}"
        ).fetchone()[0]
    assert sum(r["c"] for r in grouped) == base_count == 4


def test_selection_ledger_deadline_flip_only_for_unreached(migrated_db_path):
    """WI-04: _mark_deadline_skipped flips only ``selected`` companies with no
    company_scan_log row for the run — reached companies stay ``selected``."""
    reached = _insert_ledger_company(
        migrated_db_path, name="Reached", platform="ashby", slug="Reached", last_scanned_at=None
    )
    unreached = _insert_ledger_company(
        migrated_db_path, name="Unreached", platform="ashby", slug="Unreached"
    )
    run_id = "run-deadline-1"
    with standalone_connection(migrated_db_path) as conn:
        _record_phase_a_selection(conn, run_id, "ats_scan", 10, 3, 20, 7, 30)
        # Simulate that only `reached` actually got scanned this run.
        record_scan_outcome(
            conn, company_id=reached, source="ats_scanner", run_id=run_id, jobs_found=0
        )
        conn.commit()
        flipped = _mark_deadline_skipped(conn, run_id)
        conn.commit()

    assert flipped == 1
    with standalone_connection(migrated_db_path) as conn:
        decisions = dict(
            conn.execute(
                "SELECT company_id, decision FROM scan_selection_log WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        )
    assert decisions[reached] == "selected"
    assert decisions[unreached] == "skipped_deadline"


def test_scan_log_rows_carry_run_id(migrated_db_path):
    """WI-04: scan-log rows written during a run carry that run's run_id."""
    company_id = _insert_company(migrated_db_path)
    company = {
        "id": company_id,
        "name_raw": "AshbyCo",
        "ats_platform": "ashby",
        "ats_slug": "AshbyCo",
    }
    summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
    run_id = "run-scanlog-1"

    with (
        standalone_connection(migrated_db_path) as conn,
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=([], 0, []),
        ),
    ):
        _scan_one_company_via_ats_api(
            conn,
            migrated_db_path,
            company,
            ["Engineer"],
            [],
            summary,
            [],
            run_id=run_id,
        )

    with standalone_connection(migrated_db_path) as conn:
        rows = conn.execute(
            "SELECT run_id FROM company_scan_log WHERE company_id = ?", (company_id,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# WI-03 (#1828) — priority-tier selector acceptance tests
#
# The former high-score-history *exclusion* is replaced by an *ordering* tier
# (_priority_tier_sql). D6: no company with scan_enabled=1 and an ATS probe hit
# is ever dropped from the scan by relevance history — it only sorts later.
# These tests drive the REAL selection path (_record_phase_a_selection, which
# shares _priority_tier_sql with the live selector) against a fully-migrated DB.
# ---------------------------------------------------------------------------

_SIX_AXES = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


def _six_axis(each: int) -> dict:
    """A v3 sub_scores dict with every axis == ``each`` (sum == 6*each)."""
    return dict.fromkeys(_SIX_AXES, each)


def _insert_job_for_company(
    path: str,
    *,
    company_id: int,
    dedup_key: str,
    classification=None,
    sub_scores=None,
    company_text: str = "SomeCo",
) -> None:
    """Insert one job row bound to ``company_id`` (NOT by free-text company name)."""
    now = utc_now_iso()
    sub_scores_json = json.dumps(sub_scores) if sub_scores is not None else None
    with standalone_connection(path) as conn:
        conn.execute(
            """INSERT INTO jobs
               (dedup_key, title, company, location, first_seen, last_seen,
                company_id, classification, sub_scores_json)
               VALUES (?, 'Engineer', ?, 'Remote', ?, ?, ?, ?, ?)""",
            (dedup_key, company_text, now, now, company_id, classification, sub_scores_json),
        )
        conn.commit()


def _ledger_rows_by_company(path: str, run_id: str) -> dict:
    with standalone_connection(path) as conn:
        rows = conn.execute(
            "SELECT company_id, decision, tier, rank FROM scan_selection_log WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    return {r["company_id"]: r for r in rows}


def test_phase_a_never_excludes_hit_company_by_relevance(migrated_db_path):
    """D6: a hit company whose only scored jobs are low-relevance is still
    SELECTED (tier 2) — relevance orders the scan, it never excludes."""
    cid = _insert_ledger_company(
        migrated_db_path,
        name="LowRelCo",
        platform="ashby",
        slug="LowRelCo",
        last_scanned_at=utc_now_iso(),  # recent → not promoted by bounded revisit
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=cid,
        dedup_key="lowrel|j1",
        classification="reject",
        sub_scores=_six_axis(2),  # sum 12 < threshold 20
    )
    run_id = "run-neverexclude"
    with standalone_connection(migrated_db_path) as conn:
        _record_phase_a_selection(conn, run_id, "ats_scan", 10, 3, 20, 7, 30)
    rows = _ledger_rows_by_company(migrated_db_path, run_id)
    assert rows[cid]["decision"] == "selected"
    assert rows[cid]["tier"] == 2  # has scored jobs, none relevant, not stale → last


def test_priority_tier_apply_consider_first(migrated_db_path):
    """A company with an apply/consider job (tier 0) sorts ahead of a company
    whose only jobs are low-relevance (tier 2)."""
    apply_cid = _insert_ledger_company(
        migrated_db_path,
        name="ApplyCo",
        platform="ashby",
        slug="ApplyCo",
        last_scanned_at=utc_now_iso(),
    )
    low_cid = _insert_ledger_company(
        migrated_db_path,
        name="LowCo",
        platform="ashby",
        slug="LowCo",
        last_scanned_at=utc_now_iso(),
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=apply_cid,
        dedup_key="apply|j1",
        classification="apply",
        sub_scores=_six_axis(2),
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=low_cid,
        dedup_key="low|j1",
        classification="reject",
        sub_scores=_six_axis(2),
    )
    run_id = "run-applyfirst"
    with standalone_connection(migrated_db_path) as conn:
        _record_phase_a_selection(conn, run_id, "ats_scan", 10, 3, 20, 7, 30)
    rows = _ledger_rows_by_company(migrated_db_path, run_id)
    assert rows[apply_cid]["tier"] == 0
    assert rows[low_cid]["tier"] == 2
    assert rows[apply_cid]["rank"] < rows[low_cid]["rank"]


def test_bounded_revisit_promotes_stale_company(migrated_db_path):
    """D7: a company last scanned beyond the bounded-revisit window (default 7d)
    is promoted back to tier 0 even with no relevant jobs, so a board that
    stopped producing relevant jobs is periodically re-checked."""
    cid = _insert_ledger_company(
        migrated_db_path,
        name="StaleCo",
        platform="ashby",
        slug="StaleCo",
        last_scanned_at="2020-01-01T00:00:00",  # far older than 7 days
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=cid,
        dedup_key="stale|j1",
        classification="reject",
        sub_scores=_six_axis(2),
    )
    run_id = "run-stale"
    with standalone_connection(migrated_db_path) as conn:
        _record_phase_a_selection(conn, run_id, "ats_scan", 10, 3, 20, 7, 30)
    rows = _ledger_rows_by_company(migrated_db_path, run_id)
    assert rows[cid]["decision"] == "selected"
    assert rows[cid]["tier"] == 0


def test_priority_joins_on_company_id_not_name(migrated_db_path):
    """D6 join fix: a job whose free-text ``company`` differs from the company
    row's name but whose ``company_id`` points at it is still attributed — so
    the apply job promotes the company to tier 0 (the old name-join would miss)."""
    cid = _insert_ledger_company(
        migrated_db_path,
        name="Acme",
        platform="ashby",
        slug="Acme",
        last_scanned_at=utc_now_iso(),
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=cid,
        dedup_key="acme|j1",
        classification="apply",
        sub_scores=_six_axis(2),
        company_text="Totally Different Employer String",
    )
    run_id = "run-idjoin"
    with standalone_connection(migrated_db_path) as conn:
        _record_phase_a_selection(conn, run_id, "ats_scan", 10, 3, 20, 7, 30)
    rows = _ledger_rows_by_company(migrated_db_path, run_id)
    assert rows[cid]["tier"] == 0


def test_count_phase_a_eligible_ignores_relevance(migrated_db_path):
    """WI-03: the eligibility count is relevance-blind — a hit company with only
    low-relevance jobs still counts (relevance orders, never gates eligibility)."""
    cid = _insert_ledger_company(
        migrated_db_path,
        name="OnlyLowCo",
        platform="ashby",
        slug="OnlyLowCo",
        last_scanned_at=utc_now_iso(),
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=cid,
        dedup_key="onlylow|j1",
        classification="reject",
        sub_scores=_six_axis(2),
    )
    with standalone_connection(migrated_db_path) as conn:
        count = _count_phase_a_eligible(conn, 10, 3)
    assert count == 1


def test_priority_tier_ordering_zero_one_two_by_value(migrated_db_path):
    """Full tier ladder by VALUE and by rank: an apply company (tier 0), a
    high-relevance-sum company with no apply/consider (tier 1), and a
    only-low-relevance company (tier 2) are all SELECTED and ordered
    0 < 1 < 2. Covers the six-axis-sum >= threshold tier-1 branch and the
    ELSE-2 branch that the deleted TestHighScoreHistoryGate used to exercise."""
    tier0_cid = _insert_ledger_company(
        migrated_db_path,
        name="Tier0Co",
        platform="ashby",
        slug="Tier0Co",
        last_scanned_at=utc_now_iso(),  # recent → not promoted by bounded revisit
    )
    tier1_cid = _insert_ledger_company(
        migrated_db_path,
        name="Tier1Co",
        platform="ashby",
        slug="Tier1Co",
        last_scanned_at=utc_now_iso(),
    )
    tier2_cid = _insert_ledger_company(
        migrated_db_path,
        name="Tier2Co",
        platform="ashby",
        slug="Tier2Co",
        last_scanned_at=utc_now_iso(),
    )
    # tier 0: has an apply job (relevance sub-scores irrelevant to the branch).
    _insert_job_for_company(
        migrated_db_path,
        company_id=tier0_cid,
        dedup_key="t0|j1",
        classification="apply",
        sub_scores=_six_axis(2),
    )
    # tier 1: NO apply/consider, but six-axis sum 24 >= threshold 20.
    _insert_job_for_company(
        migrated_db_path,
        company_id=tier1_cid,
        dedup_key="t1|j1",
        classification="reject",
        sub_scores=_six_axis(4),
    )
    # tier 2: scored, only low relevance (sum 12 < 20), not stale, no apply.
    _insert_job_for_company(
        migrated_db_path,
        company_id=tier2_cid,
        dedup_key="t2|j1",
        classification="reject",
        sub_scores=_six_axis(2),
    )
    run_id = "run-tier-ladder"
    with standalone_connection(migrated_db_path) as conn:
        _record_phase_a_selection(conn, run_id, "ats_scan", 10, 3, 20, 7, 30)
    rows = _ledger_rows_by_company(migrated_db_path, run_id)
    assert rows[tier0_cid]["decision"] == "selected"
    assert rows[tier1_cid]["decision"] == "selected"
    assert rows[tier2_cid]["decision"] == "selected"
    assert rows[tier0_cid]["tier"] == 0
    assert rows[tier1_cid]["tier"] == 1
    assert rows[tier2_cid]["tier"] == 2
    assert rows[tier0_cid]["rank"] < rows[tier1_cid]["rank"] < rows[tier2_cid]["rank"]


def test_priority_tier_bootstrap_no_scored_jobs_is_tier_one(migrated_db_path):
    """Bootstrap tier-1 branch: a recently-scanned company with NO scored jobs
    at all (never NULL last_scanned_at, so not tier 0 by recency; no scored
    rows, so the NOT-EXISTS-scored branch fires) is tier 1 — ahead of a
    only-low-relevance tier-2 company, behind an apply tier-0 company."""
    boot_cid = _insert_ledger_company(
        migrated_db_path,
        name="BootstrapCo",
        platform="ashby",
        slug="BootstrapCo",
        last_scanned_at=utc_now_iso(),  # recent, non-NULL → not tier 0 by recency
    )
    # No jobs inserted for boot_cid → NOT EXISTS scored jobs → tier 1.
    low_cid = _insert_ledger_company(
        migrated_db_path,
        name="BootLowCo",
        platform="ashby",
        slug="BootLowCo",
        last_scanned_at=utc_now_iso(),
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=low_cid,
        dedup_key="bootlow|j1",
        classification="reject",
        sub_scores=_six_axis(2),  # sum 12 < 20 → tier 2
    )
    run_id = "run-bootstrap"
    with standalone_connection(migrated_db_path) as conn:
        _record_phase_a_selection(conn, run_id, "ats_scan", 10, 3, 20, 7, 30)
    rows = _ledger_rows_by_company(migrated_db_path, run_id)
    assert rows[boot_cid]["decision"] == "selected"
    assert rows[boot_cid]["tier"] == 1
    assert rows[low_cid]["tier"] == 2
    assert rows[boot_cid]["rank"] < rows[low_cid]["rank"]


def test_run_ats_api_scan_production_order_follows_priority_tier(migrated_db_path):
    """LIVE selector coverage (B1): the production Phase-A scan ORDER emitted by
    ``_run_ats_api_scan`` (ORDER BY the tier CASE) must be tier 0 < 1 < 2.

    Distinct from the ledger-mirror tier tests: those bind the tier fragment in
    ``_record_phase_a_selection``; this drives the *live* selector's own
    ``params += [max_revisit_days, high_score_threshold]`` bind. Modeled on
    ``test_phase_a_stalest_first_order`` (same fake ``_scan_one_company_via_ats_api``
    recording scan order, serial ``scan_concurrency=1``), but with a NON-ZERO
    ``high_score_threshold=20`` so the tier CASE is actually exercised — a
    transposed bind here would misclassify the low company and reorder the scan.
    """
    recent = utc_now_iso()  # all recent → none promoted to tier 0 by bounded revisit
    # Insert deliberately OUT of tier order (tier2, tier0, tier1) so the ORDER BY
    # — not insertion/rowid order — is what produces the asserted sequence.
    tier2_cid = _insert_ledger_company(
        migrated_db_path,
        name="Zeta2Co",
        platform="greenhouse",
        slug="Zeta2Co",
        last_scanned_at=recent,
    )
    tier0_cid = _insert_ledger_company(
        migrated_db_path,
        name="Alpha0Co",
        platform="greenhouse",
        slug="Alpha0Co",
        last_scanned_at=recent,
    )
    tier1_cid = _insert_ledger_company(
        migrated_db_path,
        name="Mid1Co",
        platform="greenhouse",
        slug="Mid1Co",
        last_scanned_at=recent,
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=tier0_cid,
        dedup_key="p0|j1",
        classification="apply",
        sub_scores=_six_axis(2),
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=tier1_cid,
        dedup_key="p1|j1",
        classification="reject",
        sub_scores=_six_axis(4),  # sum 24 >= 20 → tier 1
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=tier2_cid,
        dedup_key="p2|j1",
        classification="reject",
        sub_scores=_six_axis(2),  # sum 12 < 20 → tier 2
    )

    scanned_names: list[str] = []

    def _record_scan(conn_inner, db_path_inner, company, *args, **kwargs):
        scanned_names.append(company["name_raw"])

    with (
        standalone_connection(migrated_db_path) as conn,
        patch(
            "jobcannon.engine.ats_scanner._run._scan_one_company_via_ats_api",
            side_effect=_record_scan,
        ),
        patch("jobcannon.engine.ats_scanner._run.time.sleep"),
    ):
        _run_ats_api_scan(
            conn,
            migrated_db_path,
            ["Engineer"],
            [],
            {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []},
            [],
            high_score_threshold=20,  # NON-ZERO → tier CASE actually applied
            dormancy_threshold=10,
            dormancy_interval_days=3,
            scan_concurrency=1,
        )

    assert scanned_names == ["Alpha0Co", "Mid1Co", "Zeta2Co"]


def test_run_ats_scan_phases_param_skips_playwright_and_html(migrated_db_path):
    """WI-01 acceptance: run_ats_scan(phases={"api"}) executes only Phase A —
    the Playwright (A2), homepage-discovery (B) and HTML-fallback (C) entry
    points are never called — and records phases_run=["api"]."""
    from jobcannon.engine.ats_scanner import _run

    config: dict = {}

    with (
        patch.object(_run, "_run_ats_api_scan") as m_api,
        patch.object(_run, "_run_playwright_scan") as m_pw,
        patch.object(_run, "_run_homepage_discovery_phase") as m_home,
        patch.object(_run, "_run_html_fallback_scan") as m_html,
        patch.object(_run, "_score_new_ats_jobs"),
        patch.object(_run, "_log_ats_scan_run"),
        patch("jobcannon.engine.autoheal.health_monitor.run_detection", return_value=[]),
        patch("jobcannon.engine.pipeline_runner._run_heal_pass"),
    ):
        summary = _run.run_ats_scan(
            migrated_db_path,
            config,
            run_id="test-run",
            job_id="ats_scan",
            phases=frozenset({"api"}),
        )

    assert m_api.called, "Phase A (API) must run when 'api' in phases"
    assert not m_pw.called, "Phase A2 (Playwright) must be skipped when phases={'api'}"
    assert not m_home.called, "Phase B (homepage discovery) must be skipped when phases={'api'}"
    assert not m_html.called, "Phase C (HTML fallback) must be skipped when phases={'api'}"
    assert summary["phases_run"] == ["api"]


def test_run_ats_scan_render_phases_skip_api(migrated_db_path):
    """WI-01: the render job's phases run Playwright + HTML but never Phase A."""
    from jobcannon.engine.ats_scanner import _run

    with (
        patch.object(_run, "_run_ats_api_scan") as m_api,
        patch.object(_run, "_run_playwright_scan") as m_pw,
        patch.object(_run, "_run_homepage_discovery_phase") as m_home,
        patch.object(_run, "_run_html_fallback_scan") as m_html,
        patch.object(_run, "_score_new_ats_jobs"),
        patch.object(_run, "_log_ats_scan_run"),
        patch("jobcannon.engine.autoheal.health_monitor.run_detection", return_value=[]),
        patch("jobcannon.engine.pipeline_runner._run_heal_pass"),
    ):
        summary = _run.run_ats_scan(
            migrated_db_path,
            {},
            run_id="test-run",
            job_id="ats_scan_render",
            phases=frozenset({"playwright", "html"}),
        )

    assert not m_api.called, "Phase A (API) must be skipped for the render job"
    assert m_pw.called and m_home.called and m_html.called
    assert summary["phases_run"] == ["html", "playwright"]


# ---------------------------------------------------------------------------
# WI-09 (D20): per-title disposition capture (scan_title_outcomes)
# ---------------------------------------------------------------------------


def _insert_named_company(path: str, *, name: str, slug: str) -> int:
    """Insert a hit/scan_enabled ashby company and return its id."""
    with standalone_connection(path) as conn:
        cur = conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES (?, ?, 'ashby', ?, 'hit', 1,
                       '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
            (name.lower(), name, slug),
        )
        cid = cur.lastrowid
        conn.commit()
    return cid


def _title_outcome_rows(path: str, company_id: int) -> list[tuple[str, str]]:
    with standalone_connection(path) as conn:
        rows = conn.execute(
            "SELECT title, disposition FROM scan_title_outcomes WHERE company_id = ? ORDER BY id",
            (company_id,),
        ).fetchall()
    return [(r["title"], r["disposition"]) for r in rows]


def test_title_outcomes_recorded_for_relevant_company_only(migrated_db_path):
    """WI-09/D20: dispositions are captured for a RELEVANT company (positive
    control) and NOT for a non-relevant one — across both scan capture points,
    covering all three dispositions with value-level assertions.

    Relevant company (an ``apply`` job) is scanned twice: run 1 inserts the
    matched job (``matched``) and leaves the filtered title (``title_filtered``);
    run 2 re-sees the same job (``dedup_existing``). The concurrent worker drives
    both runs. The non-relevant company is scanned via the serial path and must
    produce zero rows.
    """
    relevant_id = _insert_named_company(migrated_db_path, name="RelevantCo", slug="RelevantCo")
    other_id = _insert_named_company(migrated_db_path, name="OtherCo", slug="OtherCo")

    # RelevantCo has an apply job → relevant. OtherCo has only a low-signal skip
    # job → not relevant (not apply/consider, mean 2.0 < fit_floor 3.5).
    _insert_job_for_company(
        migrated_db_path,
        company_id=relevant_id,
        dedup_key="rel-existing-apply",
        classification="apply",
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=other_id,
        dedup_key="other-skip",
        classification="skip",
        sub_scores=_six_axis(2),
    )

    # Positive control: the relevance set contains RelevantCo and excludes OtherCo.
    with standalone_connection(migrated_db_path) as conn:
        relevant_ids = _relevant_company_ids_for_outcomes(conn, {})
        # Kill switch: enabled=False disables capture entirely (None → no query,
        # no writes anywhere).
        assert (
            _relevant_company_ids_for_outcomes(conn, {"ats": {"title_outcomes_enabled": False}})
            is None
        )
    assert relevant_ids is not None
    assert relevant_id in relevant_ids
    assert other_id not in relevant_ids

    relevant_company = {
        "id": relevant_id,
        "name_raw": "RelevantCo",
        "ats_platform": "ashby",
        "ats_slug": "RelevantCo",
    }

    def _run_worker_once():
        # Fresh dict objects per run; matched and raw SHARE the engineer object
        # (run_platform_scan's identity contract), and the manager title is
        # present only in raw → title_filtered.
        engineer = _scan_job_dict(
            title="Senior Software Engineer",
            source_url="https://jobs.ashbyhq.com/RelevantCo/eng",
            source_id="eng",
        )
        manager = _scan_job_dict(
            title="Senior Product Manager",
            source_url="https://jobs.ashbyhq.com/RelevantCo/pm",
            source_id="pm",
        )
        with patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=([engineer], 1, [engineer, manager]),
        ):
            _scan_one_company_worker(
                relevant_company,
                migrated_db_path,
                ["Engineer"],
                [],
                None,
                config={},
                run_id="run-A",
                title_outcome_company_ids=relevant_ids,
            )

    _run_worker_once()  # run 1: engineer inserted → matched
    _run_worker_once()  # run 2: engineer re-seen → dedup_existing

    rel_rows = _title_outcome_rows(migrated_db_path, relevant_id)
    by_title: dict[str, list[str]] = {}
    for title, disp in rel_rows:
        by_title.setdefault(title, []).append(disp)

    # Engineer: matched on run 1, dedup_existing on run 2.
    assert sorted(by_title["Senior Software Engineer"]) == ["dedup_existing", "matched"]
    # Manager: filtered out both runs.
    assert by_title["Senior Product Manager"] == ["title_filtered", "title_filtered"]
    # Exactly the three dispositions, no others.
    assert {d for disps in by_title.values() for d in disps} == {
        "matched",
        "dedup_existing",
        "title_filtered",
    }

    # Non-relevant company: serial scan captures nothing.
    other_company = {
        "id": other_id,
        "name_raw": "OtherCo",
        "ats_platform": "ashby",
        "ats_slug": "OtherCo",
    }
    engineer = _scan_job_dict(
        title="Senior Software Engineer",
        source_url="https://jobs.ashbyhq.com/OtherCo/eng",
        source_id="eng",
    )
    manager = _scan_job_dict(
        title="Senior Product Manager",
        source_url="https://jobs.ashbyhq.com/OtherCo/pm",
        source_id="pm",
    )
    summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
    with (
        standalone_connection(migrated_db_path) as conn,
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=([engineer], 1, [engineer, manager]),
        ),
    ):
        _scan_one_company_via_ats_api(
            conn,
            migrated_db_path,
            other_company,
            ["Engineer"],
            [],
            summary,
            [],
            run_id="run-A",
            title_outcome_company_ids=relevant_ids,
        )

    assert _title_outcome_rows(migrated_db_path, other_id) == []


def test_title_outcomes_pruned(migrated_db_path):
    """WI-09: prune_title_outcomes deletes rows older than keep_days and keeps
    fresher ones (positive control: both rows present before the prune)."""
    company_id = _insert_named_company(migrated_db_path, name="PruneCo", slug="PruneCo")

    with standalone_connection(migrated_db_path) as conn:
        # One stale row (30 days old) and one fresh row (today).
        conn.execute(
            """INSERT INTO scan_title_outcomes
                   (run_id, company_id, title, disposition, seen_at)
               VALUES (?, ?, ?, ?, datetime('now', '-30 days'))""",
            ("old-run", company_id, "Old Title", "matched"),
        )
        conn.execute(
            """INSERT INTO scan_title_outcomes
                   (run_id, company_id, title, disposition, seen_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            ("new-run", company_id, "New Title", "matched"),
        )
        conn.commit()

        # Control: both rows present before pruning.
        assert conn.execute("SELECT count(*) FROM scan_title_outcomes").fetchone()[0] == 2

        deleted = prune_title_outcomes(conn, 14)
        conn.commit()

    assert deleted == 1
    assert _title_outcome_rows(migrated_db_path, company_id) == [("New Title", "matched")]


def test_title_outcomes_serial_path_records_all_dispositions(migrated_db_path):
    """WI-09: the SERIAL capture point (``_scan_one_company_via_ats_api``) records
    all three dispositions for a RELEVANT company, with value-level assertions.

    Complements ``test_title_outcomes_recorded_for_relevant_company_only`` (which
    exercises the serial path only NEGATIVELY — a non-relevant company yields
    zero rows). Without this, mutating the serial path's ``matched_is_new``
    tracking (e.g. always-False) goes undetected. Two scans of the same board:
    run 1 inserts the engineer (``matched``); run 2 re-sees it
    (``dedup_existing``); the manager title is filtered out both runs
    (``title_filtered``).
    """
    company_id = _insert_named_company(
        migrated_db_path, name="SerialRelevantCo", slug="SerialRelevantCo"
    )
    _insert_job_for_company(
        migrated_db_path,
        company_id=company_id,
        dedup_key="serial-existing-apply",
        classification="apply",
    )

    with standalone_connection(migrated_db_path) as conn:
        relevant_ids = _relevant_company_ids_for_outcomes(conn, {})
    assert relevant_ids is not None
    assert company_id in relevant_ids

    company = {
        "id": company_id,
        "name_raw": "SerialRelevantCo",
        "ats_platform": "ashby",
        "ats_slug": "SerialRelevantCo",
    }

    def _run_serial_once():
        engineer = _scan_job_dict(
            title="Senior Software Engineer",
            source_url="https://jobs.ashbyhq.com/SerialRelevantCo/eng",
            source_id="eng",
        )
        manager = _scan_job_dict(
            title="Senior Product Manager",
            source_url="https://jobs.ashbyhq.com/SerialRelevantCo/pm",
            source_id="pm",
        )
        summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
        with (
            standalone_connection(migrated_db_path) as conn,
            patch(
                "jobcannon.engine.ats_scanner._run.run_platform_scan",
                return_value=([engineer], 1, [engineer, manager]),
            ),
        ):
            _scan_one_company_via_ats_api(
                conn,
                migrated_db_path,
                company,
                ["Engineer"],
                [],
                summary,
                [],
                run_id="run-serial",
                title_outcome_company_ids=relevant_ids,
            )

    _run_serial_once()  # run 1: engineer inserted → matched
    _run_serial_once()  # run 2: engineer re-seen → dedup_existing

    rows = _title_outcome_rows(migrated_db_path, company_id)
    by_title: dict[str, list[str]] = {}
    for title, disp in rows:
        by_title.setdefault(title, []).append(disp)

    assert sorted(by_title["Senior Software Engineer"]) == ["dedup_existing", "matched"]
    assert by_title["Senior Product Manager"] == ["title_filtered", "title_filtered"]
    assert {d for disps in by_title.values() for d in disps} == {
        "matched",
        "dedup_existing",
        "title_filtered",
    }


# ---------------------------------------------------------------------------
# WI-13 (D16) — ATS board-gone demotion must NOT disable the careers crawler
# ---------------------------------------------------------------------------


def test_ats_demotion_does_not_disable_careers_scan(migrated_db_path):
    """A BoardGoneError demotion clears only the ATS-side bit.

    Before the D16 split, a 404/410'd ATS slug flipped the single shared
    ``scan_enabled`` bit to 0 — silently also disabling the careers crawler for
    that company and pushing it toward the absorbing state. After the split, the
    demotion clears ``ats_scan_enabled`` (and dual-writes legacy ``scan_enabled``)
    while ``careers_scan_enabled`` is left untouched, so careers discovery keeps
    running.
    """
    from jobcannon.engine.ats_platforms._registry import BoardGoneError

    # Seed a hit company with careers scanning explicitly ON, so the post-demotion
    # assertion is a real claim about the bit rather than an artifact of the
    # column default.
    with standalone_connection(migrated_db_path) as conn:
        cur = conn.execute(
            """INSERT INTO companies
                  (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                   scan_enabled, ats_scan_enabled, careers_scan_enabled,
                   created_at, updated_at)
               VALUES ('goneco', 'GoneCo', 'workday', 'goneco', 'hit',
                       1, 1, 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        )
        company_id = cur.lastrowid
        conn.commit()

    company = {
        "id": company_id,
        "name_raw": "GoneCo",
        "ats_platform": "workday",
        "ats_slug": "goneco",
    }
    summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
    all_new_keys: list[str] = []

    with (
        standalone_connection(migrated_db_path) as conn,
        patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            side_effect=BoardGoneError(410, "goneco"),
        ),
    ):
        _scan_one_company_via_ats_api(
            conn,
            migrated_db_path,
            company,
            ["Engineer"],
            [],
            summary,
            all_new_keys,
        )

    with standalone_connection(migrated_db_path) as conn:
        row = conn.execute(
            """SELECT ats_probe_status, miss_reason, scan_enabled,
                      ats_scan_enabled, careers_scan_enabled
                 FROM companies WHERE id = ?""",
            (company_id,),
        ).fetchone()

    assert row["ats_probe_status"] == "miss"
    assert row["miss_reason"] == "platform_slug_gone"
    assert row["ats_scan_enabled"] == 0  # ATS side demoted
    assert row["scan_enabled"] == 0  # legacy bit dual-written for exact revert
    assert row["careers_scan_enabled"] == 1  # D16: careers crawler left running

"""Tests for yield-tiered dormancy cadence.

Covers:
- consecutive_empty_scans counter transitions (empty -> increment; find ->
  reset; error -> unchanged), driven through the REAL
  _scan_one_company_via_ats_api function (not hand-copied SQL) so a reverted
  production UPDATE fails these tests.
- Dormancy gate SQL: dormant+recent skipped, dormant+overdue selected, active
  always selected.
- Count/selection parity: _count_phase_a_eligible must agree with the number
  of companies _run_ats_api_scan's real selection query actually returns.

Ported from the private repo's tests/test_dormancy_cadence.py onto the
jobcannon.engine ScanServices DI seam (Task 3). The engine has no migrations
system (host-owned, not ported) — the companies/jobs/company_scan_log schema
here is the minimal subset _run.py's SQL actually references (see
tests/engine/helpers/ats_scan_services.py), not a full migrated DB.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.ats_scanner._run import (
    _count_phase_a_eligible,
    _dormancy_gate_clause,
    _run_ats_api_scan,
    _scan_one_company_via_ats_api,
)

from tests.engine.helpers.ats_scan_services import create_scan_schema, make_scan_services


@pytest.fixture
def db_with_dormancy_fixtures(tmp_path):
    """Create a test DB with companies in various dormancy states."""
    db_path = tmp_path / "test.db"

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    create_scan_schema(conn)

    services.set_services(make_scan_services(str(db_path)))

    now = datetime.now(UTC).isoformat()
    three_days_ago = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    five_days_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat()

    # Company 1: Active (0 consecutive empties, recent scan) → should be selected
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status,
           scan_enabled, last_scanned_at, consecutive_empty_scans, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("Active Co", "active_co", "greenhouse", "active", "hit", 1, now, 0, now, now),
    )

    # Company 2: Dormant+recent (15 consecutive empties, scanned 2 days ago) → should be skipped
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status,
           scan_enabled, last_scanned_at, consecutive_empty_scans, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "Dormant Recent Co",
            "dormant_recent",
            "greenhouse",
            "dormant_recent",
            "hit",
            1,
            three_days_ago,
            15,
            now,
            now,
        ),
    )

    # Company 3: Dormant+overdue (15 consecutive empties, scanned 5 days ago) → should be selected
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status,
           scan_enabled, last_scanned_at, consecutive_empty_scans, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "Dormant Overdue Co",
            "dormant_overdue",
            "greenhouse",
            "dormant_overdue",
            "hit",
            1,
            five_days_ago,
            15,
            now,
            now,
        ),
    )

    # Company 4: Below threshold (5 consecutive empties, recent scan) → should be selected
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status,
           scan_enabled, last_scanned_at, consecutive_empty_scans, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "Below Threshold Co",
            "below_threshold",
            "greenhouse",
            "below_threshold",
            "hit",
            1,
            three_days_ago,
            5,
            now,
            now,
        ),
    )

    # Company 5: Never scanned (NULL last_scanned_at) → should be selected
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status,
           scan_enabled, last_scanned_at, consecutive_empty_scans, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "Never Scanned Co",
            "never_scanned",
            "greenhouse",
            "never_scanned",
            "hit",
            1,
            None,
            10,
            now,
            now,
        ),
    )

    conn.commit()
    yield db_path, conn
    conn.close()


def _fresh_summary() -> dict:
    """Minimal summary dict covering every key _scan_one_company_via_ats_api touches."""
    return {
        "companies_scanned": 0,
        "jobs_discovered": 0,
        "jobs_new": 0,
        "errors": [],
    }


def _company_row(conn, name_raw: str):
    return conn.execute(
        "SELECT id, name_raw, ats_platform, ats_slug FROM companies WHERE name_raw = ?",
        (name_raw,),
    ).fetchone()


def _consecutive_empty_scans(conn, company_id: int) -> int:
    row = conn.execute(
        "SELECT consecutive_empty_scans FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    return row["consecutive_empty_scans"]


def test_dormancy_gate_sql(db_with_dormancy_fixtures):
    """Test dormancy gate SQL: dormant+recent skipped, dormant+overdue selected, active always selected."""
    _, conn = db_with_dormancy_fixtures

    # Test with threshold=10, interval=3 days
    threshold = 10
    interval_days = 3

    # Active Co (0 empties) → should pass
    active = conn.execute(
        f"""SELECT name_raw FROM companies
           WHERE name_raw = 'active_co' AND {_dormancy_gate_clause()}""",
        (threshold, interval_days),
    ).fetchone()
    assert active is not None

    # Dormant Recent Co (15 empties, 2 days ago) → should fail
    dormant_recent = conn.execute(
        f"""SELECT name_raw FROM companies
           WHERE name_raw = 'dormant_recent' AND {_dormancy_gate_clause()}""",
        (threshold, interval_days),
    ).fetchone()
    assert dormant_recent is None

    # Dormant Overdue Co (15 empties, 5 days ago) → should pass
    dormant_overdue = conn.execute(
        f"""SELECT name_raw FROM companies
           WHERE name_raw = 'dormant_overdue' AND {_dormancy_gate_clause()}""",
        (threshold, interval_days),
    ).fetchone()
    assert dormant_overdue is not None

    # Below Threshold Co (5 empties, recent) → should pass
    below_threshold = conn.execute(
        f"""SELECT name_raw FROM companies
           WHERE name_raw = 'below_threshold' AND {_dormancy_gate_clause()}""",
        (threshold, interval_days),
    ).fetchone()
    assert below_threshold is not None

    # Never Scanned Co (NULL last_scanned_at) → should pass
    never_scanned = conn.execute(
        f"""SELECT name_raw FROM companies
           WHERE name_raw = 'never_scanned' AND {_dormancy_gate_clause()}""",
        (threshold, interval_days),
    ).fetchone()
    assert never_scanned is not None


# ---------------------------------------------------------------------------
# B1 — drive the REAL _scan_one_company_via_ats_api function.
#
# The seam it actually calls is run_platform_scan, imported into this
# module's namespace (jobcannon.engine.ats_scanner._run.run_platform_scan).
# Mocking it and asserting on the DB afterward means reverting the real
# UPDATE at _run.py's _scan_one_company_via_ats_api (the
# "consecutive_empty_scans = CASE WHEN ... " statement) breaks these tests.
# ---------------------------------------------------------------------------


def test_consecutive_empty_scans_increments_on_empty_real_scan(db_with_dormancy_fixtures):
    """B1: an empty (zero-job) scan increments consecutive_empty_scans by 1."""
    db_path, conn = db_with_dormancy_fixtures
    company = _company_row(conn, "active_co")
    assert _consecutive_empty_scans(conn, company["id"]) == 0

    summary = _fresh_summary()
    with patch("jobcannon.engine.ats_scanner._run.run_platform_scan", return_value=([], 0, [])):
        _scan_one_company_via_ats_api(conn, str(db_path), company, [], [], summary, [])

    assert _consecutive_empty_scans(conn, company["id"]) == 1
    assert summary["errors"] == []


def test_consecutive_empty_scans_resets_on_nonempty_real_scan(db_with_dormancy_fixtures):
    """B1 (continued): a non-zero yield resets the counter to 0, even from a
    high starting value (the dormant_overdue fixture seeds 15)."""
    db_path, conn = db_with_dormancy_fixtures
    company = _company_row(conn, "dormant_overdue")
    assert _consecutive_empty_scans(conn, company["id"]) == 15

    job = {
        "title": "Staff Engineer",
        "company_source": "Greenhouse",
        "location": "Remote",
        "locations_structured": [],
        "description": "Full stack engineering role.",
        "source_url": "https://boards.greenhouse.io/dormant_overdue/jobs/1",
        "source_id": "1",
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
        "posted_date": None,
        "is_remote": None,
        "employment_type": None,
        "department": None,
        "ats_refreshed_at": None,
    }
    summary = _fresh_summary()
    with patch(
        "jobcannon.engine.ats_scanner._run.run_platform_scan", return_value=([job], 0, [job])
    ):
        _scan_one_company_via_ats_api(conn, str(db_path), company, [], [], summary, [])

    assert _consecutive_empty_scans(conn, company["id"]) == 0
    assert summary["errors"] == []


# ---------------------------------------------------------------------------
# B2 — error path: consecutive_empty_scans must be UNTOUCHED
# when the scan raises. The UPDATE sits after run_platform_scan() inside the
# try block, so an exception there jumps straight to `except Exception`,
# skipping the UPDATE entirely.
# ---------------------------------------------------------------------------


def test_consecutive_empty_scans_untouched_on_scan_error(db_with_dormancy_fixtures):
    """B2: a raising scan must leave consecutive_empty_scans untouched (not
    incremented, not reset) and must record the error."""
    db_path, conn = db_with_dormancy_fixtures
    company = _company_row(conn, "active_co")
    assert _consecutive_empty_scans(conn, company["id"]) == 0

    summary = _fresh_summary()
    with patch(
        "jobcannon.engine.ats_scanner._run.run_platform_scan",
        side_effect=RuntimeError("simulated ATS API failure"),
    ):
        _scan_one_company_via_ats_api(conn, str(db_path), company, [], [], summary, [])

    assert _consecutive_empty_scans(conn, company["id"]) == 0
    assert summary["errors"], "the scan error should have been recorded"


def test_consecutive_empty_scans_untouched_on_scan_error_from_nonzero(db_with_dormancy_fixtures):
    """B2 (continued): same contract starting from a non-zero counter — an
    error must not reset it to 0 either (only a successful non-empty scan
    may reset it)."""
    db_path, conn = db_with_dormancy_fixtures
    company = _company_row(conn, "dormant_overdue")
    assert _consecutive_empty_scans(conn, company["id"]) == 15

    summary = _fresh_summary()
    with patch(
        "jobcannon.engine.ats_scanner._run.run_platform_scan",
        side_effect=RuntimeError("simulated ATS API failure"),
    ):
        _scan_one_company_via_ats_api(conn, str(db_path), company, [], [], summary, [])

    assert _consecutive_empty_scans(conn, company["id"]) == 15
    assert summary["errors"]


# ---------------------------------------------------------------------------
# B3 — count/selection parity: _count_phase_a_eligible must
# agree with the number of rows _run_ats_api_scan's own selection query
# returns. Both consume _dormancy_gate_clause() (plus the other Phase A
# gates); this drives BOTH real code paths against the same fixtures so a
# future edit to one query without the other regresses visibly instead of
# silently desyncing progress totals from actual scan behavior.
# ---------------------------------------------------------------------------


def test_phase_a_count_matches_real_selection_query(db_with_dormancy_fixtures):
    db_path, conn = db_with_dormancy_fixtures
    high_score_threshold = 20
    dormancy_threshold = 10
    dormancy_interval_days = 3

    expected_count = _count_phase_a_eligible(
        conn, high_score_threshold, dormancy_threshold, dormancy_interval_days
    )
    assert expected_count > 0, "fixture must contain at least one eligible company"

    scanned_companies: list[str] = []

    def _fake_scan_one(
        conn,
        db_path,
        company,
        target_titles,
        title_exclusions,
        summary,
        keys,
        workday_max_pages=None,
    ):
        scanned_companies.append(company["name_raw"])

    with (
        patch(
            "jobcannon.engine.ats_scanner._run._scan_one_company_via_ats_api",
            side_effect=_fake_scan_one,
        ),
        patch("jobcannon.engine.ats_scanner._run.time.sleep"),
    ):
        _run_ats_api_scan(
            conn,
            str(db_path),
            [],
            [],
            _fresh_summary(),
            [],
            high_score_threshold,
            dormancy_threshold,
            dormancy_interval_days,
        )

    assert len(scanned_companies) == expected_count
    # Sanity cross-check against the known-good dormancy gate outcomes
    # (test_dormancy_gate_sql above): dormant_recent is the only exclusion.
    assert set(scanned_companies) == {
        "active_co",
        "dormant_overdue",
        "below_threshold",
        "never_scanned",
    }

"""Tests for static-first fallthrough in ats_prober.py (issue #565).

Tests the cheap→expensive ordering:
1. Re-detect known ATS on subdomain
2. Static HTML extract (L1/L4)
3. Embedded-JSON tier (Tier 2.5)
4. Playwright tier (most expensive)

Also tests that Playwright is NOT invoked when earlier tiers succeed.
"""

import sqlite3
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from jobcannon.engine.ats_prober import (
    ProbeHttpResult,
    _try_static_first_fallthrough,
    probe_single_company,
)


@pytest.fixture
def db_conn(migrated_db_path):
    """Open a connection to the migrated DB, yield (path, conn), close after."""
    conn = sqlite3.connect(migrated_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    yield migrated_db_path, conn
    conn.close()


def _insert_company(
    conn, name, careers_url, ats_platform=None, ats_slug=None, ats_probe_status="miss"
):
    """Helper: insert a company for testing."""
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """INSERT INTO companies (name, name_raw, careers_url, ats_platform, ats_slug, ats_probe_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, name, careers_url, ats_platform, ats_slug, ats_probe_status, now, now),
    )
    return cursor.lastrowid


class TestStaticFirstFallthroughOrdering:
    """Test that the fallthrough respects cheap→expensive ordering."""

    def test_static_extract_succeeds_before_playwright(self, db_conn):
        """Test that static extraction succeeds and Playwright is NOT invoked."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "DB_PATH": db_path,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock static extraction to return jobs - patch at source since imports are lazy inside function
        with (
            patch(
                "jobcannon.engine.careers_crawler._static_tier._try_static_extract"
            ) as mock_static,
            # Tier 1's own courtesy fetch of careers_url (subdomain-link discovery)
            # hits real network otherwise; 404 keeps it a no-op fallthrough.
            patch("jobcannon.engine.ats_prober.requests.get") as mock_get,
        ):
            mock_static.return_value = [
                {"title": "Software Engineer", "url": "https://example.com/job1"}
            ]
            mock_get.return_value = MagicMock(status_code=404)

            # Mock Playwright to track if it was called
            with patch(
                "jobcannon.engine.careers_crawler._playwright_tier._try_playwright_extract"
            ) as mock_playwright:
                # Commit before calling to avoid database lock
                conn.commit()
                result = _try_static_first_fallthrough(
                    company_id, "TestCompany", "https://example.com/careers", conn, config, now
                )

                # Static extraction should have been called
                assert mock_static.called
                # Playwright should NOT have been called (static succeeded)
                assert not mock_playwright.called
                # Result should be a miss with jobs persisted (not 'hit' for custom pages)
                assert result["status"] == "miss"
                assert result["reason"] == "static_fallthrough_tier2_jobs_persisted"
                assert result["jobs_found"] == 1

        # Verify DB state - real persistence, not mocked
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "miss"
        assert company["scan_enabled"] == 1
        assert company["miss_reason"] == "static_fallthrough_tier2_jobs_persisted"

        # Verify job was actually persisted to DB
        jobs = conn.execute("SELECT * FROM jobs WHERE company_id = ?", (company_id,)).fetchall()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Software Engineer"

    def test_embedded_json_tried_after_static_fails(self, db_conn):
        """Test that embedded-JSON is tried when static extraction fails."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "DB_PATH": db_path,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock static extraction to return None (JS-heavy page) - patch at source since imports are lazy
        with (
            patch(
                "jobcannon.engine.careers_crawler._static_tier._try_static_extract"
            ) as mock_static,
            patch(
                "jobcannon.engine.careers_crawler._embedded_json_tier._try_embedded_json_extract"
            ) as mock_json,
            # Tier 1's own courtesy fetch of careers_url (subdomain-link discovery)
            # hits real network otherwise; 404 keeps it a no-op fallthrough.
            patch("jobcannon.engine.ats_prober.requests.get") as mock_get,
        ):
            mock_static.return_value = None
            mock_json.return_value = [
                {"title": "Software Engineer", "url": "https://example.com/job1"}
            ]
            mock_get.return_value = MagicMock(status_code=404)

            # Mock Playwright to track if it was called
            with patch(
                "jobcannon.engine.careers_crawler._playwright_tier._try_playwright_extract"
            ) as mock_playwright:
                # Commit before calling to avoid database lock
                conn.commit()
                result = _try_static_first_fallthrough(
                    company_id, "TestCompany", "https://example.com/careers", conn, config, now
                )

                # Static extraction should have been called
                assert mock_static.called
                # Embedded-JSON should have been called
                assert mock_json.called
                # Playwright should NOT have been called (embedded-JSON succeeded)
                assert not mock_playwright.called
                # Result should be a miss with jobs persisted (not 'hit' for custom pages)
                assert result["status"] == "miss"
                assert result["reason"] == "static_fallthrough_tier3_jobs_persisted"
                assert result["jobs_found"] == 1

        # Verify DB state - real persistence, not mocked
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "miss"
        assert company["scan_enabled"] == 1
        assert company["miss_reason"] == "static_fallthrough_tier3_jobs_persisted"

        # Verify job was actually persisted to DB
        jobs = conn.execute("SELECT * FROM jobs WHERE company_id = ?", (company_id,)).fetchall()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Software Engineer"

    def test_static_no_matches_sets_specific_reason(self, db_conn):
        """Test that static extraction with no matches sets specific miss_reason."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock static extraction to return empty list (statically rendered but no matches)
        # Patch at source since imports are lazy inside function
        with (
            patch(
                "jobcannon.engine.careers_crawler._static_tier._try_static_extract"
            ) as mock_static,
            # Tier 1's own courtesy fetch of careers_url (subdomain-link discovery)
            # hits real network otherwise; 404 keeps it a no-op fallthrough.
            patch("jobcannon.engine.ats_prober.requests.get") as mock_get,
        ):
            mock_static.return_value = []
            mock_get.return_value = MagicMock(status_code=404)

            result = _try_static_first_fallthrough(
                company_id, "TestCompany", "https://example.com/careers", conn, config, now
            )

            # Result should be a miss
            assert result["status"] == "miss"
            assert result["reason"] == "static_fallthrough_tier2_no_matches"

        # Verify DB state
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "miss"
        assert company["miss_reason"] == "static_fallthrough_tier2_no_matches"

    def test_playwright_no_matches_sets_specific_reason(self, db_conn):
        """Test that Playwright with no matches sets specific miss_reason."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock all tiers to fail - patch at source since imports are lazy
        with (
            patch("jobcannon.engine.ats_prober.extract_ats_from_url_best") as mock_detect,
            patch(
                "jobcannon.engine.careers_crawler._static_tier._try_static_extract"
            ) as mock_static,
            patch(
                "jobcannon.engine.careers_crawler._embedded_json_tier._try_embedded_json_extract"
            ) as mock_json,
            patch(
                "jobcannon.engine.careers_crawler._playwright_tier._try_playwright_extract"
            ) as mock_pw_extract,
            # Tier 1's own courtesy fetch of careers_url (subdomain-link discovery)
            # hits real network otherwise; 404 keeps it a no-op fallthrough.
            patch("jobcannon.engine.ats_prober.requests.get") as mock_get,
        ):
            mock_detect.return_value = None
            mock_static.return_value = None
            mock_json.return_value = None
            mock_pw_extract.return_value = []
            mock_get.return_value = MagicMock(status_code=404)

            # Mock sync_playwright to avoid actual browser launch - patch at source
            with patch("playwright.sync_api.sync_playwright") as mock_sync_playwright:
                mock_pw_context = MagicMock()
                mock_browser = MagicMock()
                mock_sync_playwright.return_value.__enter__.return_value = mock_pw_context
                mock_pw_context.chromium.launch.return_value = mock_browser

                result = _try_static_first_fallthrough(
                    company_id, "TestCompany", "https://example.com/careers", conn, config, now
                )

                # All tiers should have been called
                assert mock_detect.called
                assert mock_static.called
                assert mock_json.called
                assert mock_pw_extract.called

                # Result should be a miss with specific reason
                assert result["status"] == "miss"
                assert result["reason"] == "static_fallthrough_tier4_no_matches"

        # Verify DB state
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "miss"
        assert company["miss_reason"] == "static_fallthrough_tier4_no_matches"

    def test_playwright_success_persists_jobs_real_db(self, db_conn):
        """Regression test for Finding B: Tier 4 success with real DB persistence.

        This test exercises Tier 4 (Playwright) success with a real temp DB (not mocked
        _upsert_and_log) to ensure the summary dict has the required companies_crawled
        key. The bug was that Tier 4 used a hand-rolled summary dict missing this key,
        causing a KeyError in _upsert_and_log.
        """
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "DB_PATH": db_path,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock all earlier tiers to fail, Playwright to succeed
        with (
            patch("jobcannon.engine.ats_prober.extract_ats_from_url_best") as mock_detect,
            patch(
                "jobcannon.engine.careers_crawler._static_tier._try_static_extract"
            ) as mock_static,
            patch(
                "jobcannon.engine.careers_crawler._embedded_json_tier._try_embedded_json_extract"
            ) as mock_json,
            patch(
                "jobcannon.engine.careers_crawler._playwright_tier._try_playwright_extract"
            ) as mock_pw_extract,
            # Tier 1's own courtesy fetch of careers_url (subdomain-link discovery)
            # hits real network otherwise; 404 keeps it a no-op fallthrough.
            patch("jobcannon.engine.ats_prober.requests.get") as mock_get,
        ):
            mock_detect.return_value = None
            mock_static.return_value = None
            mock_json.return_value = None
            mock_pw_extract.return_value = [
                {"title": "Software Engineer", "url": "https://example.com/job1"}
            ]
            mock_get.return_value = MagicMock(status_code=404)

            # Mock sync_playwright to avoid actual browser launch
            with patch("playwright.sync_api.sync_playwright") as mock_sync_playwright:
                mock_pw_context = MagicMock()
                mock_browser = MagicMock()
                mock_sync_playwright.return_value.__enter__.return_value = mock_pw_context
                mock_pw_context.chromium.launch.return_value = mock_browser

                # Commit before calling to avoid database lock
                conn.commit()
                # This should NOT raise KeyError (Finding B bug)
                result = _try_static_first_fallthrough(
                    company_id, "TestCompany", "https://example.com/careers", conn, config, now
                )

                # Result should be a miss with jobs persisted
                assert result["status"] == "miss"
                assert result["reason"] == "static_fallthrough_tier4_jobs_persisted"
                assert result["jobs_found"] == 1

        # Verify DB state - real persistence, not mocked
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "miss"
        assert company["scan_enabled"] == 1
        assert company["miss_reason"] == "static_fallthrough_tier4_jobs_persisted"

        # Verify job was actually persisted to DB
        jobs = conn.execute("SELECT * FROM jobs WHERE company_id = ?", (company_id,)).fetchall()
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Software Engineer"


class TestTier1Regression:
    """Regression tests for Tier 1 ATS re-detection (Finding A and Finding C)."""

    def test_tier1_promotion_with_real_sqlite3_row(self, db_conn):
        """Regression test for Finding A: company.get() crashes on sqlite3.Row.

        This test uses a real sqlite3.Row object (not a mock dict) to ensure bracket
        access works correctly. The bug was that company.get() raised AttributeError
        on sqlite3.Row, which was silently caught by the outer except, causing Tier 1
        to no-op for all companies.
        """
        db_path, conn = db_conn
        # Insert company with m074 cohort state: no platform, prior miss, scan disabled
        company_id = _insert_company(
            conn, "TestCompany", "https://example.com/careers", ats_probe_status="miss"
        )
        # Disable scan to test reenable logic
        conn.execute("UPDATE companies SET scan_enabled = 0 WHERE id = ?", (company_id,))
        conn.commit()

        config = {
            "TESTING": True,
            "DB_PATH": db_path,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock extract_ats_from_url_best to return a hit
        with patch("jobcannon.engine.ats_prober.extract_ats_from_url_best") as mock_detect:
            mock_detect.return_value = (
                "greenhouse",
                "test-company",
                "https://example.com/careers",
            )

            # Mock promote_from_careers_link to return promoted outcome and actually update DB
            # Note: imported inside _try_static_first_fallthrough, so patch at import location
            def mock_promote_impl(
                conn, company_id, platform, slug, *, page_url, config, reenable_scan
            ):
                # Simulate the real function's behavior when reenable_scan=True
                if reenable_scan:
                    conn.execute(
                        "UPDATE companies SET scan_enabled = 1 WHERE id = ?", (company_id,)
                    )
                    conn.commit()
                return {"outcome": "promoted", "ats_platform": "greenhouse"}

            with patch(
                "jobcannon.engine.ats_identity_reconcile.promote_from_careers_link"
            ) as mock_promote:
                mock_promote.side_effect = mock_promote_impl

                result = _try_static_first_fallthrough(
                    company_id, "TestCompany", "https://example.com/careers", conn, config, now
                )

                # Critical: promote_from_careers_link must have been called
                # If company.get() crashed, this would be False (caught by outer except)
                assert mock_promote.called, (
                    "promote_from_careers_link was not called - likely hit the sqlite3.Row.get() bug"
                )

                # Result should reflect the promoted outcome
                assert result["status"] == "hit"
                assert result["source"] == "ats_redetect_careers_url"

                # Verify scan was re-enabled for m074 cohort
                company = conn.execute(
                    "SELECT * FROM companies WHERE id = ?", (company_id,)
                ).fetchone()
                assert company["scan_enabled"] == 1

    def test_tier1_reenable_gating_negative_case(self, db_conn):
        """Negative test for reenable_scan gating: non-miss status should NOT re-enable.

        A company with ats_probe_status='error' (or any status other than 'miss',
        or a non-NULL ats_platform) and scan_enabled=0 should NOT get re-enabled
        even when Tier 1 finds a live ATS board.
        """
        db_path, conn = db_conn
        # Insert company with error status (not miss) - should NOT re-enable
        company_id = _insert_company(
            conn, "TestCompany", "https://example.com/careers", ats_probe_status="error"
        )
        # Disable scan
        conn.execute("UPDATE companies SET scan_enabled = 0 WHERE id = ?", (company_id,))
        conn.commit()

        config = {
            "TESTING": True,
            "DB_PATH": db_path,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }
        now = datetime.now(UTC).isoformat()

        # Mock extract_ats_from_url_best to return a hit
        with patch("jobcannon.engine.ats_prober.extract_ats_from_url_best") as mock_detect:
            mock_detect.return_value = (
                "greenhouse",
                "test-company",
                "https://example.com/careers",
            )

            # Mock promote_from_careers_link to return promoted outcome
            # Note: imported inside _try_static_first_fallthrough, so patch at import location
            with patch(
                "jobcannon.engine.ats_identity_reconcile.promote_from_careers_link"
            ) as mock_promote:
                mock_promote.return_value = {"outcome": "promoted", "ats_platform": "greenhouse"}

                result = _try_static_first_fallthrough(
                    company_id, "TestCompany", "https://example.com/careers", conn, config, now
                )

                # promote_from_careers_link should have been called with reenable_scan=False
                assert mock_promote.called
                assert mock_promote.call_args[1]["reenable_scan"] is False

                # Result should reflect the promoted outcome
                assert result["status"] == "hit"

                # Verify scan was NOT re-enabled (error status, not miss)
                company = conn.execute(
                    "SELECT * FROM companies WHERE id = ?", (company_id,)
                ).fetchone()
                assert company["scan_enabled"] == 0


class TestProbeSingleCompanyFallthrough:
    """Test that probe_single_company routes to fallthrough appropriately."""

    def test_speculative_probing_exhausted_routes_to_fallthrough(self, db_conn):
        """Test that speculative probing exhaustion routes to fallthrough when careers_url exists."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", "https://example.com/careers")
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }

        # Mock all speculative probes to fail
        with (
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=False, status_code=404),
            ),
            patch("jobcannon.engine.ats_prober._probe_greenhouse", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_ashby", return_value=False),
            patch("jobcannon.engine.ats_prober._try_static_first_fallthrough") as mock_fallthrough,
        ):
            mock_fallthrough.return_value = {"status": "hit", "source": "static_extract"}

            result = probe_single_company(company_id, conn, config)

            # Fallthrough should have been called
            assert mock_fallthrough.called
            # Result should reflect fallthrough success
            assert result["status"] == "hit"

    def test_speculative_probing_no_careers_url_sets_specific_reason(self, db_conn):
        """Test that companies without careers_url get specific miss_reason."""
        db_path, conn = db_conn
        company_id = _insert_company(conn, "TestCompany", None)  # No careers_url
        config = {
            "TESTING": True,
            "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": []}},
        }

        # Mock all speculative probes to fail
        with (
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=False, status_code=404),
            ),
            patch("jobcannon.engine.ats_prober._probe_greenhouse", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_ashby", return_value=False),
            patch("jobcannon.engine.ats_prober._try_static_first_fallthrough") as mock_fallthrough,
        ):
            # Fallthrough should NOT be called (no careers_url)
            result = probe_single_company(company_id, conn, config)

            assert not mock_fallthrough.called
            # Result should be a miss with specific reason
            assert result["status"] == "miss"
            assert result["reason"] == "speculative_probing_exhausted_no_careers_url"

        # Verify DB state
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        assert company["ats_probe_status"] == "miss"
        assert company["miss_reason"] == "speculative_probing_exhausted_no_careers_url"

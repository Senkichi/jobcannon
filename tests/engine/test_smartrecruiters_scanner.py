"""Tests for SmartRecruiters ATS scanner: URL detection, probing, and scanning."""

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from jobcannon.engine import runtime_config
from tests.engine.helpers.ats_session import ats_session_method

# ---------------------------------------------------------------------------
# Tests: SmartRecruiters URL detection
# ---------------------------------------------------------------------------


class TestSmartRecruitersUrlDetection:
    """Tests for SmartRecruiters URL pattern recognition."""

    def test_jobs_url_returns_smartrecruiters_and_slug(self):
        """jobs.smartrecruiters.com/{slug}/... returns ('smartrecruiters', slug)."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://jobs.smartrecruiters.com/LinkedIn3/744000115714244-staff-data-scientist"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "smartrecruiters"
        assert slug == "LinkedIn3"  # Case preserved after PR-4 registry migration

    def test_careers_url_returns_smartrecruiters_and_slug(self):
        """careers.smartrecruiters.com/{slug}/... returns ('smartrecruiters', slug)."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://careers.smartrecruiters.com/AbbVie/positions"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "smartrecruiters"
        assert slug == "AbbVie"

    def test_api_url_returns_smartrecruiters_and_slug(self):
        """API URL returns ('smartrecruiters', slug)."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://api.smartrecruiters.com/v1/companies/Visa/postings"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "smartrecruiters"
        assert slug == "Visa"

    def test_case_insensitive(self):
        """URL detection is case-insensitive."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://JOBS.SMARTRECRUITERS.COM/MyCompany/12345"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "smartrecruiters"

    def test_non_smartrecruiters_url_not_matched(self):
        """Non-SmartRecruiters URLs are not matched."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://www.smartrecruiters.com/about"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform is None


# ---------------------------------------------------------------------------
# Tests: _probe_smartrecruiters
# ---------------------------------------------------------------------------


class TestProbeSmartRecruiters:
    """Tests for the SmartRecruiters probe function."""

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_probe_returns_true_when_jobs_found(self, mock_get):
        """Returns True when API returns 200 with totalFound > 0."""
        from jobcannon.engine.ats_prober import _probe_smartrecruiters

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"totalFound": 851, "content": [{"name": "Engineer"}]}
        mock_get.return_value = mock_resp
        assert _probe_smartrecruiters("Visa") is True

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_probe_returns_false_when_zero_found(self, mock_get):
        """Returns False when API returns 200 but totalFound = 0."""
        from jobcannon.engine.ats_prober import _probe_smartrecruiters

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"totalFound": 0, "content": []}
        mock_get.return_value = mock_resp
        assert _probe_smartrecruiters("EmptyCompany") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_probe_returns_false_on_404(self, mock_get):
        """Returns False when API returns 404."""
        from jobcannon.engine.ats_prober import _probe_smartrecruiters

        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_smartrecruiters("nonexistent") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_probe_returns_false_on_exception(self, mock_get):
        """Returns False on connection error."""
        from jobcannon.engine.ats_prober import _probe_smartrecruiters

        mock_get.side_effect = Exception("connection refused")
        assert _probe_smartrecruiters("Visa") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_probe_sends_accept_json_header(self, mock_get):
        """Probe sends Accept: application/json header."""
        from jobcannon.engine.ats_prober import _probe_smartrecruiters

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"totalFound": 1, "content": []}
        mock_get.return_value = mock_resp
        _probe_smartrecruiters("Visa")
        _, kwargs = mock_get.call_args
        assert kwargs.get("headers", {}).get("Accept") == "application/json"


# ---------------------------------------------------------------------------
# Tests: scan_smartrecruiters
# ---------------------------------------------------------------------------


@patch("jobcannon.engine.ats_platforms._fetch_smartrecruiters_description", return_value="")
class TestScanSmartRecruiters:
    """Tests for the SmartRecruiters job scanner.

    Class-level patch disables the per-job detail fetch so list-endpoint
    behavior stays focused and test run hermetic. A separate class
    (TestFetchSmartRecruitersDescription) covers the detail fetch itself.
    """

    @pytest.fixture(autouse=True)
    def _no_detail_fetch_sleep(self):
        """Zero the per-page pacing sleep in _fetch_postings_with_completeness.

        _fetch_postings_with_completeness does time.sleep(_PAGE_FETCH_SLEEP_S)
        between pages; with 150 postings (test_scan_paginates) that was ~0.15s.
        Patch the constant to 0 (not time.sleep) to avoid touching the shared time
        module. No test asserts on the pacing.
        """
        with patch(
            "jobcannon.engine.ats_platforms._platforms_smartrecruiters._PAGE_FETCH_SLEEP_S",
            0,
        ):
            yield

    def _make_posting(self, title, city="Austin", region="TX", country="US", posting_id="12345"):
        return {
            "id": posting_id,
            "name": title,
            "location": {"city": city, "region": region, "country": country},
            "company": {"identifier": "TestCo", "name": "Test Company"},
        }

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_scan_returns_matched_jobs(self, mock_get_session, _mock_detail):
        """scan_smartrecruiters returns jobs matching target titles."""
        from jobcannon.engine.ats_platforms import scan_smartrecruiters

        mock_get = ats_session_method(mock_get_session, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "totalFound": 2,
            "content": [
                self._make_posting("Senior Data Scientist", posting_id="111"),
                self._make_posting("Retail Associate", posting_id="222"),
            ],
        }
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"
        assert results[0]["company_source"] == "SmartRecruiters"
        assert results[0]["location"] == "Austin, TX, US"
        assert "TestCo" in results[0]["source_url"]
        assert "111" in results[0]["source_url"]

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_scan_applies_exclusions(self, mock_get_session, _mock_detail):
        """Filters out jobs matching exclusion keywords."""
        from jobcannon.engine.ats_platforms import scan_smartrecruiters

        mock_get = ats_session_method(mock_get_session, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "totalFound": 1,
            "content": [self._make_posting("Junior Data Scientist")],
        }
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], ["junior"])
        assert len(results) == 0

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_scan_handles_empty_response(self, mock_get_session, _mock_detail):
        """Returns empty list when no postings."""
        from jobcannon.engine.ats_platforms import scan_smartrecruiters

        mock_get = ats_session_method(mock_get_session, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"totalFound": 0, "content": []}
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert results == []

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_scan_handles_http_error(self, mock_get_session, _mock_detail):
        """Returns empty list on non-200 status."""
        from jobcannon.engine.ats_platforms import scan_smartrecruiters

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.return_value = MagicMock(status_code=500)
        assert scan_smartrecruiters("TestCo", ["data scientist"], []) == []

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_scan_paginates(self, mock_get_session, _mock_detail):
        """Fetches multiple pages when totalFound > page_size."""
        from jobcannon.engine.ats_platforms import scan_smartrecruiters

        mock_get = ats_session_method(mock_get_session, "get")
        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "totalFound": 150,
            "content": [
                self._make_posting(f"Data Analyst {i}", posting_id=str(i)) for i in range(100)
            ],
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {
            "totalFound": 150,
            "content": [
                self._make_posting(f"Data Analyst {i}", posting_id=str(i)) for i in range(100, 150)
            ],
        }
        mock_get.side_effect = [page1, page2]

        results = scan_smartrecruiters("TestCo", ["data analyst"], [])
        assert len(results) == 150
        assert mock_get.call_count == 2

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_scan_request_exception(self, mock_get_session, _mock_detail):
        """Returns empty list on request exception."""
        from jobcannon.engine.ats_platforms import scan_smartrecruiters

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.side_effect = Exception("network error")
        assert scan_smartrecruiters("TestCo", ["data scientist"], []) == []

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_scan_location_assembly(self, mock_get_session, _mock_detail):
        """Assembles location from city, region, country fields."""
        from jobcannon.engine.ats_platforms import scan_smartrecruiters

        mock_get = ats_session_method(mock_get_session, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "totalFound": 1,
            "content": [
                {
                    "id": "999",
                    "name": "Data Scientist",
                    "location": {"city": "San Francisco", "region": "CA", "country": "US"},
                }
            ],
        }
        mock_get.return_value = mock_resp

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert results[0]["location"] == "San Francisco, CA, US"


# ---------------------------------------------------------------------------
# Tests: _fetch_smartrecruiters_description (per-job detail fetch)
# ---------------------------------------------------------------------------


class TestFetchSmartRecruitersDescription:
    """Tests for the SmartRecruiters per-job detail fetcher."""

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_fetches_and_strips_html_description(self, mock_get_session):
        """Returns concatenated sections, HTML-stripped."""
        from jobcannon.engine.ats_platforms import _fetch_smartrecruiters_description

        mock_get = ats_session_method(mock_get_session, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": "<p>Build <b>great</b> products.</p>"},
                    "qualifications": {"text": "5+ years of Python experience."},
                }
            }
        }
        mock_get.return_value = mock_resp

        text = _fetch_smartrecruiters_description("TestCo", "999")
        assert "Build" in text
        assert "great" in text
        assert "5+ years" in text
        assert "<b>" not in text

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_fetches_all_four_sections(self, mock_get_session):
        """All four known sections (company, job, qualifications, additional) are included."""
        from jobcannon.engine.ats_platforms import _fetch_smartrecruiters_description

        mock_get = ats_session_method(mock_get_session, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobAd": {
                "sections": {
                    "companyDescription": {"text": "We are TestCo."},
                    "jobDescription": {"text": "Write great code."},
                    "qualifications": {"text": "Python required."},
                    "additionalInformation": {"text": "Remote OK."},
                }
            }
        }
        mock_get.return_value = mock_resp

        text = _fetch_smartrecruiters_description("TestCo", "999")
        assert "We are TestCo" in text
        assert "Write great code" in text
        assert "Python required" in text
        assert "Remote OK" in text

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_404_returns_empty_string(self, mock_get_session):
        """Detail 404 returns empty string, no exception."""
        from jobcannon.engine.ats_platforms import _fetch_smartrecruiters_description

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.return_value = MagicMock(status_code=404)
        assert _fetch_smartrecruiters_description("TestCo", "DNE") == ""

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_network_exception_returns_empty_string(self, mock_get_session):
        """Network error returns empty string, no exception."""
        from jobcannon.engine.ats_platforms import _fetch_smartrecruiters_description

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.side_effect = Exception("timeout")
        assert _fetch_smartrecruiters_description("TestCo", "999") == ""

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_missing_jobAd_returns_empty_string(self, mock_get_session):
        """Response without jobAd.sections returns empty string."""
        from jobcannon.engine.ats_platforms import _fetch_smartrecruiters_description

        mock_get = ats_session_method(mock_get_session, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"unrelated": "shape"}
        mock_get.return_value = mock_resp
        assert _fetch_smartrecruiters_description("TestCo", "999") == ""

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_scan_smartrecruiters_populates_description_from_detail(
        self, mock_get_session_list, mock_get_session_detail
    ):
        """End-to-end: scan_smartrecruiters calls detail endpoint and sets description."""
        from jobcannon.engine.ats_platforms import scan_smartrecruiters

        # Both the list endpoint (_platforms_smartrecruiters.get_session) and the
        # detail endpoint (_detail_fetchers.get_session) are patched separately
        # (they are two distinct local get_session bindings), but this test's
        # side_effect list assumes both calls land on ONE ordered mock — so make
        # both patched get_session() calls return the same fake Session object.
        shared_session = MagicMock()
        mock_get_session_list.return_value = shared_session
        mock_get_session_detail.return_value = shared_session
        mock_get = shared_session.get

        list_resp = MagicMock(status_code=200)
        list_resp.json.return_value = {
            "totalFound": 1,
            "content": [
                {
                    "id": "abc-123",
                    "name": "Senior Data Scientist",
                    "location": {"city": "SF", "region": "CA", "country": "US"},
                }
            ],
        }
        detail_resp = MagicMock(status_code=200)
        detail_resp.json.return_value = {
            "jobAd": {
                "sections": {
                    "jobDescription": {"text": "Role details here."},
                    "qualifications": {"text": "Must know Python."},
                }
            }
        }
        mock_get.side_effect = [list_resp, detail_resp]

        results = scan_smartrecruiters("TestCo", ["data scientist"], [])
        assert len(results) == 1
        assert "Role details here" in results[0]["description"]
        assert "Must know Python" in results[0]["description"]
        # Second call is the detail fetch
        detail_call_url = mock_get.call_args_list[1][0][0]
        assert (
            detail_call_url
            == "https://api.smartrecruiters.com/v1/companies/TestCo/postings/abc-123"
        )


# ---------------------------------------------------------------------------
# Tests: _fetch_postings_with_completeness (completeness gate)
# ---------------------------------------------------------------------------


class TestSmartRecruitersCompleteness:
    """The reconciler may only expire against a complete live board."""

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_under_cap_board_is_complete(self, mock_get_session):
        """totalFound <= cap and fully paginated → complete=True."""
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _fetch_postings_with_completeness,
        )

        mock_get = ats_session_method(mock_get_session, "get")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "totalFound": 2,
            "content": [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}],
        }
        mock_get.return_value = resp

        postings, complete = _fetch_postings_with_completeness("TestCo")
        assert complete is True
        assert len(postings) == 2

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_over_cap_board_is_incomplete_but_non_empty(self, mock_get_session):
        """totalFound > cap → complete=False AND the first `_MAX_RESULTS` are returned.

        Regression guard: pre-fix, an over-cap board hit a `break` BEFORE
        `out.extend`, so discovery returned ZERO postings (AbbVie 1460 -> 0).
        Now it returns the first `_MAX_RESULTS` (the tail is unfetchable) with
        completeness False.
        """
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _MAX_RESULTS,
            _fetch_postings_with_completeness,
        )

        mock_get = ats_session_method(mock_get_session, "get")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "totalFound": _MAX_RESULTS + 500,
            "content": [{"id": str(i), "name": f"Job {i}"} for i in range(100)],
        }
        mock_get.return_value = resp

        postings, complete = _fetch_postings_with_completeness("TestCo")
        assert complete is False
        # Discovery is NOT zeroed: the first _MAX_RESULTS postings came back.
        assert len(postings) == _MAX_RESULTS

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_over_cap_warning_fires_once_per_board_not_per_page(self, mock_get_session, caplog):
        """Multi-page over-cap response logs warning exactly once, not per page.

        Regression guard: pre-fix, `totalFound` was re-read on every
        page iteration, so the over-cap warning fired once per page (20 times for
        a 2522-posting board with cap 2000 and page size 100). After the fix,
        `totalFound` is captured only on the first page, so the warning fires
        exactly once per board fetch.
        """
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _MAX_RESULTS,
            _fetch_postings_with_completeness,
        )

        mock_get = ats_session_method(mock_get_session, "get")
        # Simulate a 20-page over-cap response (e.g., 2500 postings, cap 2000)
        pages = []
        for i in range(20):  # 20 pages of 100 = 2000 (hits cap)
            page = MagicMock(status_code=200)
            page.json.return_value = {
                "totalFound": 2500,
                "content": [
                    {"id": str(j), "name": f"Job {j}"} for j in range(i * 100, (i + 1) * 100)
                ],
            }
            pages.append(page)
        mock_get.side_effect = pages

        caplog.set_level(logging.WARNING)
        postings, complete = _fetch_postings_with_completeness("TestCo")

        # Verify functional behavior unchanged
        assert complete is False
        assert len(postings) == _MAX_RESULTS  # Cap at _MAX_RESULTS

        # Verify warning fires exactly once
        warning_records = [
            record for record in caplog.records if "board has 2500 postings" in record.message
        ]
        assert len(warning_records) == 1, (
            f"Expected exactly 1 over-cap warning, got {len(warning_records)}"
        )

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_empty_board_is_complete(self, mock_get_session):
        """totalFound=0 (genuinely empty) → complete=True (safe to reconcile)."""
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _fetch_postings_with_completeness,
        )

        mock_get = ats_session_method(mock_get_session, "get")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"totalFound": 0, "content": []}
        mock_get.return_value = resp

        postings, complete = _fetch_postings_with_completeness("TestCo")
        assert postings == []
        assert complete is True

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_first_page_error_is_incomplete(self, mock_get_session):
        """Network/HTTP error before any page arrives → complete=False (no expiry)."""
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _fetch_postings_with_completeness,
        )

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.side_effect = Exception("network error")
        postings, complete = _fetch_postings_with_completeness("TestCo")
        assert postings == []
        assert complete is False

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_first_page_410_raises_board_gone(self, mock_get_session):
        """First-page HTTP 410 → BoardGoneError (the company slug no longer
        resolves). The scan path demotes the stale hit on this signal."""
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _fetch_postings_with_completeness,
        )
        from jobcannon.engine.ats_platforms._registry import BoardGoneError

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.return_value = MagicMock(status_code=410)
        with pytest.raises(BoardGoneError) as exc_info:
            _fetch_postings_with_completeness("DefunctCo")
        assert exc_info.value.status == 410

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_first_page_403_does_not_raise_board_gone(self, mock_get_session):
        """First-page HTTP 403 (blocked, NOT gone) → incomplete, no raise."""
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _fetch_postings_with_completeness,
        )

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.return_value = MagicMock(status_code=403)
        postings, complete = _fetch_postings_with_completeness("BlockedCo")
        assert postings == []
        assert complete is False

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_fetch_postings_wrapper_returns_list_only(self, mock_get_session):
        """Thin _fetch_postings wrapper returns just the postings list."""
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import _fetch_postings

        mock_get = ats_session_method(mock_get_session, "get")
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"totalFound": 1, "content": [{"id": "1", "name": "A"}]}
        mock_get.return_value = resp

        result = _fetch_postings("TestCo")
        assert isinstance(result, list)

    # -----------------------------------------------------------------------
    # Tests: parallel page-fetch concurrency
    # -----------------------------------------------------------------------

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_page_fetch_respects_concurrency_bound_with_overlap(self, mock_get_session):
        """Recorded-concurrency test: bound respected AND overlap proven.

        totalFound=500 (5 pages of 100); page 1 is serial, leaving 4 pages
        for the parallel pool. With page_fetch_concurrency=2, max concurrent
        page fetches must be exactly 2.
        """
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        runtime_config.set_config_provider(lambda: {"ats": {"page_fetch_concurrency": 2}})

        tracker = {"max_concurrent": 0, "active": 0, "lock": threading.Lock()}

        def _page(_url, **_kwargs):
            offset = _kwargs["params"]["offset"]
            with tracker["lock"]:
                tracker["active"] += 1
                tracker["max_concurrent"] = max(tracker["max_concurrent"], tracker["active"])
            if offset > 0:
                time.sleep(0.05)
            with tracker["lock"]:
                tracker["active"] -= 1
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "totalFound": 500,
                "content": [
                    {"id": str(i), "name": f"Job {i}"} for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.side_effect = _page

        with patch(
            "jobcannon.engine.ats_platforms._platforms_smartrecruiters._PAGE_FETCH_SLEEP_S", 0
        ):
            postings, complete = _fetch_postings_with_completeness("TestCo")

        assert complete is True
        assert len(postings) == 500
        assert tracker["max_concurrent"] == 2

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_page_fetch_concurrency_clamps_to_range(self, mock_get_session):
        """page_fetch_concurrency config values outside 1-6 are clamped."""
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        test_cases = [
            (0, 1),
            (-5, 1),
            (1, 1),
            (4, 4),
            (6, 6),
            (10, 6),
            (100, 6),
        ]

        for config_value, expected_concurrency in test_cases:
            runtime_config.set_config_provider(
                lambda config_value=config_value: {"ats": {"page_fetch_concurrency": config_value}}
            )

            tracker = {"max_concurrent": 0, "active": 0, "lock": threading.Lock()}

            def _page(_url, tracker=tracker, **_kwargs):
                offset = _kwargs["params"]["offset"]
                with tracker["lock"]:
                    tracker["active"] += 1
                    tracker["max_concurrent"] = max(tracker["max_concurrent"], tracker["active"])
                if offset > 0:
                    time.sleep(0.05)
                with tracker["lock"]:
                    tracker["active"] -= 1
                resp = MagicMock(status_code=200)
                # 8 parallel pages past page 1 so bounds up to ceiling (6)
                # are observable.
                resp.json.return_value = {
                    "totalFound": 100 + 8 * _PAGE_SIZE,
                    "content": [
                        {"id": str(i), "name": f"Job {i}"}
                        for i in range(offset, offset + _PAGE_SIZE)
                    ],
                }
                return resp

            mock_get = ats_session_method(mock_get_session, "get")
            mock_get.side_effect = _page

            with patch(
                "jobcannon.engine.ats_platforms._platforms_smartrecruiters._PAGE_FETCH_SLEEP_S",
                0,
            ):
                _fetch_postings_with_completeness("TestCo")

            assert tracker["max_concurrent"] == expected_concurrency, (
                f"config={config_value}, expected={expected_concurrency}, "
                f"got={tracker['max_concurrent']}"
            )

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_page_fetch_failure_isolated_to_that_page(self, mock_get_session):
        """One page failing in the parallel pool degrades only that page.

        totalFound=500 (5 pages of 100); page at offset=200 returns HTTP
        500. The other pages still land; the fetch does not crash and
        completeness correctly reflects the resulting partial total.
        """
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        runtime_config.set_config_provider(lambda: {"ats": {"page_fetch_concurrency": 4}})

        def _page(_url, **_kwargs):
            offset = _kwargs["params"]["offset"]
            if offset == 200:
                return MagicMock(status_code=500)
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "totalFound": 500,
                "content": [
                    {"id": str(i), "name": f"Job {i}"} for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.side_effect = _page

        with patch(
            "jobcannon.engine.ats_platforms._platforms_smartrecruiters._PAGE_FETCH_SLEEP_S", 0
        ):
            postings, complete = _fetch_postings_with_completeness("TestCo")

        # 4 of 5 pages landed (400 postings); the failed page contributed 0.
        assert len(postings) == 400
        assert complete is False
        fetched_ids = {int(p["id"]) for p in postings}
        assert not any(200 <= i < 300 for i in fetched_ids)

    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.get_session")
    def test_parallel_pages_assembled_in_offset_order_despite_completion_order(
        self, mock_get_session
    ):
        """Output order is deterministic (offset order) regardless of which
        parallel page's HTTP response comes back first."""
        from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        runtime_config.set_config_provider(lambda: {"ats": {"page_fetch_concurrency": 4}})
        total = 500

        def _page(_url, **_kwargs):
            offset = _kwargs["params"]["offset"]
            if offset > 0:
                # Higher offset -> shorter sleep -> completes first.
                time.sleep(0.08 - (offset / total) * 0.06)
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "totalFound": total,
                "content": [
                    {"id": str(i), "name": f"Job {i}"} for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_get = ats_session_method(mock_get_session, "get")
        mock_get.side_effect = _page

        with patch(
            "jobcannon.engine.ats_platforms._platforms_smartrecruiters._PAGE_FETCH_SLEEP_S", 0
        ):
            postings, complete = _fetch_postings_with_completeness("TestCo")

        assert complete is True
        assert len(postings) == total
        ids = [int(p["id"]) for p in postings]
        assert ids == sorted(ids)

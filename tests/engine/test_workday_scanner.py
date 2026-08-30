"""Tests for Workday ATS scanner: URL detection, probing, and scanning."""

import threading
import time
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest

from jobcannon.engine import runtime_config
from tests.engine.helpers.ats_session import ats_session_method

# ---------------------------------------------------------------------------
# Tests: Workday URL detection in ats_detection.py
# ---------------------------------------------------------------------------


class TestWorkdayUrlDetection:
    """Tests for Workday URL pattern recognition in extract_ats_from_urls."""

    def test_workday_human_url_returns_workday_and_slug(self):
        """Human-facing myworkdayjobs.com URL returns ('workday', 'subdomain/board')."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://walmart.wd5.myworkdayjobs.com/WalmartExternal"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"
        assert slug == "walmart.wd5/WalmartExternal"

    def test_workday_human_url_with_en_us_prefix(self):
        """Human URL with en-US locale prefix still extracts correctly."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/some-path"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"
        assert slug == "walmart.wd5/WalmartExternal"

    def test_workday_api_url_returns_workday_and_slug(self):
        """API URL returns ('workday', 'subdomain/board')."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://walmart.wd5.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/jobs"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"
        assert slug == "walmart.wd5/WalmartExternal"

    def test_workday_case_insensitive(self):
        """Workday URL detection is case-insensitive."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://WALMART.WD5.MYWORKDAYJOBS.COM/WalmartExternal"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "workday"

    def test_workday_url_does_not_match_non_workday(self):
        """Non-Workday URLs are not matched."""
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://www.walmart.com/careers"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform is None


# ---------------------------------------------------------------------------
# Tests: _probe_workday
# ---------------------------------------------------------------------------


class TestProbeWorkday:
    """Tests for the Workday probe function."""

    @patch("jobcannon.engine.ats_prober.requests.post")
    def test_probe_returns_true_on_200(self, mock_post):
        """_probe_workday returns True when API returns 200."""
        from jobcannon.engine.ats_prober import _probe_workday

        mock_post.return_value = MagicMock(status_code=200)
        assert _probe_workday("walmart.wd5/WalmartExternal") is True

    @patch("jobcannon.engine.ats_prober.requests.post")
    def test_probe_returns_false_on_404(self, mock_post):
        """_probe_workday returns False when API returns 404."""
        from jobcannon.engine.ats_prober import _probe_workday

        mock_post.return_value = MagicMock(status_code=404)
        assert _probe_workday("invalid/board") is False

    @patch("jobcannon.engine.ats_prober.requests.post")
    def test_probe_returns_false_on_exception(self, mock_post):
        """_probe_workday returns False on connection error."""
        from jobcannon.engine.ats_prober import _probe_workday

        mock_post.side_effect = Exception("connection refused")
        assert _probe_workday("walmart.wd5/WalmartExternal") is False

    def test_probe_returns_false_on_invalid_slug(self):
        """_probe_workday returns False for slug without '/'."""
        from jobcannon.engine.ats_prober import _probe_workday

        assert _probe_workday("no-slash") is False

    @patch("jobcannon.engine.ats_prober.requests.post")
    def test_probe_sends_post_request_with_correct_url(self, mock_post):
        """_probe_workday constructs correct API URL from slug."""
        from jobcannon.engine.ats_prober import _probe_workday

        mock_post.return_value = MagicMock(status_code=200)
        _probe_workday("walmart.wd5/WalmartExternal")
        args, kwargs = mock_post.call_args
        assert (
            args[0] == "https://walmart.wd5.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/jobs"
        )


# ---------------------------------------------------------------------------
# Tests: scan_workday
# ---------------------------------------------------------------------------


@patch("jobcannon.engine.ats_platforms._fetch_workday_description", return_value="")
class TestScanWorkday:
    """Tests for the Workday job scanner.

    The class-level patch disables the per-job detail fetch so these tests
    stay hermetic and focused on list-endpoint behavior. A separate test
    class (TestFetchWorkdayDescription) exercises the detail fetch itself.
    """

    @pytest.fixture(autouse=True)
    def _no_scan_sleeps(self):
        """Zero scan_workday's per-page pacing sleep.

        _fetch_postings sleeps _PAGE_FETCH_SLEEP_S between pages.
        test_scan_paginates_correctly (2 pages, 25 postings) would otherwise pay
        ~0.1s per page. Patch the constant to 0 (not time.sleep — avoids the
        shared-time-module trap). No test asserts pacing.
        """
        with patch("jobcannon.engine.ats_platforms._platforms_workday._PAGE_FETCH_SLEEP_S", 0):
            yield

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_scan_returns_matched_jobs(self, mock_get_session, _mock_detail):
        """scan_workday returns jobs matching target titles."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "total": 2,
            "jobPostings": [
                {
                    "title": "Senior Data Scientist",
                    "locationsText": "Sunnyvale, CA",
                    "externalPath": "Senior-Data-Scientist_R-12345",
                },
                {
                    "title": "Retail Associate",
                    "locationsText": "Dallas, TX",
                    "externalPath": "Retail-Associate_R-99999",
                },
            ],
        }
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"
        assert results[0]["company_source"] == "Workday"
        assert results[0]["location"] == "Sunnyvale, CA"
        assert urlsplit(results[0]["source_url"]).hostname == "walmart.wd5.myworkdayjobs.com"

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_scan_applies_exclusions(self, mock_get_session, _mock_detail):
        """scan_workday filters out jobs matching exclusion keywords."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Junior Data Scientist",
                    "locationsText": "Remote",
                    "externalPath": "Junior-DS_R-001",
                },
            ],
        }
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=["junior"],
        )
        assert len(results) == 0

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_scan_handles_empty_response(self, mock_get_session, _mock_detail):
        """scan_workday returns empty list when API returns no postings."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"total": 0, "jobPostings": []}
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results == []

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_scan_handles_http_error(self, mock_get_session, _mock_detail):
        """scan_workday returns empty list on a transient (non-gone) non-200.

        404/410 now bifurcate to BoardGoneError (see the gone test below); a 503
        is a transient block that must still degrade to an empty list, never
        demote a real board."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms import scan_workday

        mock_post.return_value = MagicMock(status_code=503)
        results = scan_workday(
            "acme.wd5/Blocked",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results == []

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_scan_raises_board_gone_on_first_page_404(self, mock_get_session, _mock_detail):
        """A first-page 404/410 propagates BoardGoneError through the public
        scan_workday entry (run_platform_scan calls fetch_postings OUTSIDE its
        try), so scan callers can catch it and demote the stale hit."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms import scan_workday
        from jobcannon.engine.ats_platforms._registry import BoardGoneError

        mock_post.return_value = MagicMock(status_code=404)
        with pytest.raises(BoardGoneError):
            scan_workday("invalid/board", target_titles=["data scientist"], exclusions=[])

    def test_scan_rejects_invalid_slug_format(self, _mock_detail):
        """scan_workday returns empty list for slug without '/'."""
        from jobcannon.engine.ats_platforms import scan_workday

        results = scan_workday("no-slash", ["data scientist"], [])
        assert results == []

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_scan_paginates_correctly(self, mock_get_session, _mock_detail):
        """scan_workday fetches multiple pages when total > page_size."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms import scan_workday

        page1_response = MagicMock(status_code=200)
        page1_response.json.return_value = {
            "total": 25,
            "jobPostings": [
                {"title": f"Data Scientist {i}", "locationsText": "", "externalPath": f"DS-{i}"}
                for i in range(20)
            ],
        }
        page2_response = MagicMock(status_code=200)
        page2_response.json.return_value = {
            "total": 25,
            "jobPostings": [
                {"title": f"Data Scientist {i}", "locationsText": "", "externalPath": f"DS-{i}"}
                for i in range(20, 25)
            ],
        }
        mock_post.side_effect = [page1_response, page2_response]

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(results) == 25
        assert mock_post.call_count == 2

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_scan_request_exception_returns_empty(self, mock_get_session, _mock_detail):
        """scan_workday returns empty list on request exception."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms import scan_workday

        mock_post.side_effect = Exception("network error")
        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results == []

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_scan_source_url_format(self, mock_get_session, _mock_detail):
        """scan_workday builds correct source_url from externalPath."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms import scan_workday

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Data Scientist",
                    "locationsText": "Remote",
                    "externalPath": "/job/Data-Scientist_R-12345",
                }
            ],
        }
        mock_post.return_value = mock_response

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert results[0]["source_url"] == (
            "https://walmart.wd5.myworkdayjobs.com/en-US/WalmartExternal/job/Data-Scientist_R-12345"
        )


# ---------------------------------------------------------------------------
# Tests: _fetch_postings_with_completeness
# ---------------------------------------------------------------------------


@patch("jobcannon.engine.ats_platforms._fetch_workday_description", return_value="")
class TestFetchPostingsWithCompleteness:
    """Tests for the completeness signal returned by _fetch_postings_with_completeness.

    Completeness rules:
      - complete=True  when total_fetched >= total (including genuine empty board).
      - complete=False when total exceeds what the page budget can fetch — but
        the partial postings still come back non-empty.
      - complete=False when a network/HTTP error prevents any page from arriving.
      - complete=False when pagination stops before total_fetched >= total.
    """

    @pytest.fixture(autouse=True)
    def _no_sleeps(self):
        with patch("jobcannon.engine.ats_platforms._platforms_workday._PAGE_FETCH_SLEEP_S", 0):
            yield

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_fully_fetched_board_is_complete(self, mock_get_session, _mock_detail):
        """total=3, one page of 3 postings → complete=True."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "total": 3,
            "jobPostings": [
                {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"} for i in range(3)
            ],
        }
        mock_post.return_value = mock_resp

        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal")
        assert complete is True
        assert len(postings) == 3

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_board_over_budget_is_incomplete_but_non_empty(self, mock_get_session, _mock_detail):
        """Board larger than the page budget → complete=False AND non-empty.

        Regression guard: pre-fix, a board with total > the cap returned an
        EMPTY postings list (discovery silently zeroed). Now discovery gets
        the first ``max_pages`` pages, and only completeness is False.
        """
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        # total=500 (25 pages of 20) but budget capped at 3 pages → 60 fetched.
        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "total": 500,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_post.side_effect = _page

        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal", max_pages=3)
        assert complete is False
        # Discovery is NOT zeroed: first 3 pages (60 postings) came back.
        assert len(postings) == 3 * _PAGE_SIZE
        assert mock_post.call_count == 3

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_large_board_within_budget_is_complete(self, mock_get_session, _mock_detail):
        """A >200-posting board fully paginates when within the page budget."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        total = 250  # 13 pages of 20 (last page is partial) — exceeds the old 200 cap.

        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            end = min(offset + _PAGE_SIZE, total)
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "total": total,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, end)
                ],
            }
            return resp

        mock_post.side_effect = _page

        postings, complete = _fetch_postings_with_completeness(
            "acme.wd5/AcmeExternal", max_pages=100
        )
        assert complete is True
        assert len(postings) == total

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_total_reported_only_on_first_page_is_not_truncated(
        self, mock_get_session, _mock_detail
    ):
        """Real Workday CXS: ``total`` is populated ONLY on the offset=0 page;
        every subsequent page returns ``total=0`` (with 20 valid postings).

        Regression for the silent 40-job cap: the loop re-read ``total`` each
        page, so page 2 overwrote it with 0 and the ``total_fetched >= total``
        break fired at ``40 >= 0`` — truncating EVERY board to 2 pages
        regardless of size (Nvidia 2000 / Salesforce 1461 / Adobe 1091 all
        cut to 40). All other completeness tests reported the real total on
        every page, so none caught this. The fix captures ``total`` once.
        """
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        total = 130  # 7 pages (last partial) — well past the old 2-page/40 cap.

        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            end = min(offset + _PAGE_SIZE, total)
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                # Real total on the first page, 0 on every later page.
                "total": total if offset == 0 else 0,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, end)
                ],
            }
            return resp

        mock_post.side_effect = _page

        postings, complete = _fetch_postings_with_completeness(
            "acme.wd5/AcmeExternal", max_pages=100
        )
        # The whole board comes back — NOT truncated to 40 (the old cap).
        assert len(postings) == total
        assert complete is True

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_max_pages_explicit_param_applied(self, mock_get_session, _mock_detail):
        """An explicit max_pages arg caps pagination.

        Mirrors how run_ats_scan / reconcile_all_companies thread
        config.ats.workday_max_pages down to the registry's slug->list scanner
        (threaded as an explicit parameter, replacing the
        thread-unsafe ``set_max_pages`` ContextVar this test used to cover).
        """
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "total": 500,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_post.side_effect = _page

        # Pass max_pages explicitly
        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal", max_pages=2)

        assert complete is False
        assert len(postings) == 2 * _PAGE_SIZE
        assert mock_post.call_count == 2

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_empty_board_is_complete(self, mock_get_session, _mock_detail):
        """total=0, no postings → complete=True (genuine empty board)."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"total": 0, "jobPostings": []}
        mock_post.return_value = mock_resp

        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal")
        assert complete is True
        assert postings == []

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_first_page_error_is_incomplete(self, mock_get_session, _mock_detail):
        """Network exception on first page → complete=False."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        mock_post.side_effect = Exception("connection refused")

        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal")
        assert complete is False
        assert postings == []

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_early_pagination_stop_is_incomplete(self, mock_get_session, _mock_detail):
        """Server error on page 2 before total_fetched >= total → complete=False."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        page1_resp = MagicMock(status_code=200)
        page1_resp.json.return_value = {
            "total": 25,
            "jobPostings": [
                {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"} for i in range(20)
            ],
        }
        page2_resp = MagicMock(status_code=500)
        mock_post.side_effect = [page1_resp, page2_resp]

        postings, complete = _fetch_postings_with_completeness("acme.wd5/AcmeExternal")
        assert complete is False
        assert len(postings) == 20  # page 1 landed; page 2 failed

    def test_invalid_slug_is_incomplete(self, _mock_detail):
        """Slug without '/' → complete=False, empty list."""
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        postings, complete = _fetch_postings_with_completeness("no-slash")
        assert complete is False
        assert postings == []

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_first_page_410_raises_board_gone(self, mock_get_session, _mock_detail):
        """First-page HTTP 410 → BoardGoneError (the tenant/slug no longer
        resolves, e.g. Walmart). The scan path catches this to demote the stale
        hit rather than logging '0 fetched' against a dead board forever."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )
        from jobcannon.engine.ats_platforms._registry import BoardGoneError

        mock_post.return_value = MagicMock(status_code=410)
        with pytest.raises(BoardGoneError) as exc_info:
            _fetch_postings_with_completeness("walmart.wd5/WalmartExternal")
        assert exc_info.value.status == 410

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_first_page_404_raises_board_gone(self, mock_get_session, _mock_detail):
        """First-page HTTP 404 → BoardGoneError (slug doesn't resolve)."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )
        from jobcannon.engine.ats_platforms._registry import BoardGoneError

        mock_post.return_value = MagicMock(status_code=404)
        with pytest.raises(BoardGoneError):
            _fetch_postings_with_completeness("acme.wd5/Gone")

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_first_page_403_does_not_raise_board_gone(self, mock_get_session, _mock_detail):
        """First-page HTTP 403 (blocked/rate-limited, NOT gone) → incomplete, no
        raise: a transient block must never demote a real board."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        mock_post.return_value = MagicMock(status_code=403)
        postings, complete = _fetch_postings_with_completeness("acme.wd5/Blocked")
        assert postings == []
        assert complete is False

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_mid_pagination_410_does_not_raise_board_gone(self, mock_get_session, _mock_detail):
        """A 410 AFTER page 1 (postings already collected) is a partial break, NOT
        board-gone — we never demote a board that just served real postings."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _fetch_postings_with_completeness,
        )

        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "total": 100,
            "jobPostings": [
                {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"} for i in range(20)
            ],
        }
        page2 = MagicMock(status_code=410)
        mock_post.side_effect = [page1, page2]
        postings, complete = _fetch_postings_with_completeness("acme.wd5/Partial")
        assert len(postings) == 20
        assert complete is False

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_fetch_postings_thin_wrapper_returns_list(self, mock_get_session, _mock_detail):
        """_fetch_postings is a thin wrapper that discards the completeness flag."""
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import _fetch_postings

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "total": 1,
            "jobPostings": [{"title": "Data Scientist", "externalPath": "/job/DS_R-1"}],
        }
        mock_post.return_value = mock_resp

        result = _fetch_postings("acme.wd5/AcmeExternal")
        assert isinstance(result, list)
        assert len(result) == 1

    # -----------------------------------------------------------------------
    # Tests: parallel page-fetch concurrency
    # -----------------------------------------------------------------------

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_page_fetch_respects_concurrency_bound_with_overlap(
        self, mock_get_session, _mock_detail
    ):
        """Recorded-concurrency test: bound is respected AND overlap is proven.

        total=200 (10 pages of 20); page 1 is serial, leaving 9 pages for the
        parallel pool. With page_fetch_concurrency=3, max concurrent page
        fetches must be exactly 3 — not 1 (which would mean the pool never
        actually overlapped, i.e. accidentally serial) and not >3 (which
        would mean the bound was ignored).
        """
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        runtime_config.set_config_provider(lambda: {"ats": {"page_fetch_concurrency": 3}})

        tracker = {"max_concurrent": 0, "active": 0, "lock": threading.Lock()}

        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            with tracker["lock"]:
                tracker["active"] += 1
                tracker["max_concurrent"] = max(tracker["max_concurrent"], tracker["active"])
            if offset > 0:
                time.sleep(0.05)  # only the parallel pages need to overlap
            with tracker["lock"]:
                tracker["active"] -= 1
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "total": 200,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_post.side_effect = _page

        postings, complete = _fetch_postings_with_completeness(
            "acme.wd5/AcmeExternal", max_pages=100
        )

        assert complete is True
        assert len(postings) == 200
        # Proves the pool actually overlapped (not accidentally serial) AND
        # respected the configured ceiling (not unbounded).
        assert tracker["max_concurrent"] == 3

    def test_page_fetch_concurrency_clamps_to_range(self, _mock_detail):
        """page_fetch_concurrency config values outside 1-6 are clamped."""
        from jobcannon.engine.ats_platforms._concurrency import get_page_fetch_concurrency

        test_cases = [
            (0, 1),  # Below floor → 1
            (-5, 1),  # Negative → 1
            (1, 1),  # Minimum valid → 1
            (4, 4),  # Default → 4
            (6, 6),  # Maximum valid → 6
            (10, 6),  # Above ceiling → 6
            (100, 6),  # Way above ceiling → 6
        ]

        for config_value, expected_concurrency in test_cases:
            runtime_config.set_config_provider(
                lambda config_value=config_value: {"ats": {"page_fetch_concurrency": config_value}}
            )

            actual = get_page_fetch_concurrency()

            assert actual == expected_concurrency, (
                f"config={config_value}, expected={expected_concurrency}, got={actual}"
            )

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_page_fetch_failure_isolated_to_that_page(self, mock_get_session, _mock_detail):
        """One page failing in the parallel pool degrades only that page.

        total=100 (5 pages of 20); page 1 (offset=0) serial, pages at offsets
        20/40/60/80 fetched in parallel. The page at offset=40 returns HTTP
        500; the other three succeed. The failure must not raise, must not
        drop the other pages' postings, and must not crash the fetch —
        completeness correctly reflects the resulting partial total.
        """
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        runtime_config.set_config_provider(lambda: {"ats": {"page_fetch_concurrency": 4}})

        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            if offset == 40:
                return MagicMock(status_code=500)
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "total": 100,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_post.side_effect = _page

        postings, complete = _fetch_postings_with_completeness(
            "acme.wd5/AcmeExternal", max_pages=100
        )

        # 4 of 5 pages landed (80 postings); the failed page contributed 0,
        # not a crash, and did not suppress the pages that succeeded.
        assert len(postings) == 80
        assert complete is False
        fetched_offsets = {int(p["title"].split()[1]) for p in postings}
        assert 40 not in fetched_offsets
        assert {0, 20, 60, 80}.issubset(fetched_offsets)

    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_parallel_pages_assembled_in_offset_order_despite_completion_order(
        self, mock_get_session, _mock_detail
    ):
        """Output order is deterministic (offset order) regardless of which
        parallel page's HTTP response comes back first.

        Pages complete in REVERSE offset order (highest offset sleeps least,
        finishes first) — this proves the offset-sort-before-extend step
        (not thread-completion order) determines the final list order.
        """
        mock_post = ats_session_method(mock_get_session, "post")
        from jobcannon.engine.ats_platforms._platforms_workday import (
            _PAGE_SIZE,
            _fetch_postings_with_completeness,
        )

        runtime_config.set_config_provider(lambda: {"ats": {"page_fetch_concurrency": 4}})
        total = 100  # page 1 (offset 0) serial + 4 parallel pages (20/40/60/80)

        def _page(_url, **_kwargs):
            offset = _kwargs["json"]["offset"]
            if offset > 0:
                # Higher offset -> shorter sleep -> completes first.
                time.sleep(0.08 - (offset / 100.0) * 0.06)
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "total": total,
                "jobPostings": [
                    {"title": f"Job {i}", "externalPath": f"/job/Job-{i}_R-{i}"}
                    for i in range(offset, offset + _PAGE_SIZE)
                ],
            }
            return resp

        mock_post.side_effect = _page

        postings, complete = _fetch_postings_with_completeness(
            "acme.wd5/AcmeExternal", max_pages=100
        )

        assert complete is True
        assert len(postings) == total
        # The postings must appear in ascending offset order (0, 20, 40, 60,
        # 80) despite completing in the opposite order under the hood.
        job_indices = [int(p["title"].split()[1]) for p in postings]
        assert job_indices == sorted(job_indices)


# ---------------------------------------------------------------------------
# Tests: _fetch_workday_description (per-job detail fetch)
# ---------------------------------------------------------------------------


class TestFetchWorkdayDescription:
    """Tests for the Workday per-job detail fetcher.

    The Workday CXS list endpoint returns titles only; the full HTML
    description lives at a separate per-job URL. These tests cover the
    detail-fetch behavior directly.
    """

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_fetches_and_strips_html_description(self, mock_get_session):
        """_fetch_workday_description returns plain-text JD from HTML."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import _fetch_workday_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobPostingInfo": {
                "jobDescription": "<p>Design and build <b>scalable</b> data pipelines.</p>"
            }
        }
        mock_get.return_value = mock_resp

        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert "Design and build" in text
        assert "scalable" in text
        assert "<b>" not in text  # HTML was stripped

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_fetches_plain_text_description_unchanged(self, mock_get_session):
        """Non-HTML descriptions pass through without stripping."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import _fetch_workday_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobPostingInfo": {"jobDescription": "Plain text description here."}
        }
        mock_get.return_value = mock_resp

        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert text == "Plain text description here."

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_404_returns_empty_string(self, mock_get_session):
        """Detail endpoint 404 returns empty string, no exception."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import _fetch_workday_description

        mock_get.return_value = MagicMock(status_code=404)
        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DNE")
        assert text == ""

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_network_exception_returns_empty_string(self, mock_get_session):
        """Network error returns empty string, no exception."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import _fetch_workday_description

        mock_get.side_effect = Exception("timeout")
        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert text == ""

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    def test_missing_jobPostingInfo_returns_empty_string(self, mock_get_session):
        """Response without jobPostingInfo key returns empty string."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import _fetch_workday_description

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"other": "shape"}
        mock_get.return_value = mock_resp

        text = _fetch_workday_description("walmart.wd5", "walmart", "WalmartExternal", "/job/DS-1")
        assert text == ""

    @patch("jobcannon.engine.ats_platforms._detail_fetchers.get_session")
    @patch("jobcannon.engine.ats_platforms._platforms_workday.get_session")
    def test_scan_workday_populates_description_from_detail(
        self, mock_workday_session, mock_detail_session
    ):
        """End-to-end: scan_workday calls detail endpoint and populates description."""
        mock_post = ats_session_method(mock_workday_session, "post")
        mock_get = ats_session_method(mock_detail_session, "get")
        from jobcannon.engine.ats_platforms import scan_workday

        # List endpoint returns one matching job
        list_resp = MagicMock(status_code=200)
        list_resp.json.return_value = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Senior Data Scientist",
                    "locationsText": "Remote",
                    "externalPath": "/job/Senior-DS_R-1",
                }
            ],
        }
        mock_post.return_value = list_resp

        # Detail endpoint returns the JD
        detail_resp = MagicMock(status_code=200)
        detail_resp.json.return_value = {
            "jobPostingInfo": {
                "jobDescription": "Full job description with details about the role."
            }
        }
        mock_get.return_value = detail_resp

        results = scan_workday(
            "walmart.wd5/WalmartExternal",
            target_titles=["data scientist"],
            exclusions=[],
        )
        assert len(results) == 1
        assert "Full job description" in results[0]["description"]
        # Detail URL hit correctly
        args, _ = mock_get.call_args
        assert args[0] == (
            "https://walmart.wd5.myworkdayjobs.com/wday/cxs/walmart/"
            "WalmartExternal/job/Senior-DS_R-1"
        )

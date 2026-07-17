"""Tests for the Amazon Jobs (global search.json board) ATS scanner."""

from unittest.mock import MagicMock, patch

from tests.engine.helpers.ats_session import ats_session_method


def _job(
    title,
    *,
    icims="1234567",
    path=None,
    loc="Seattle, Washington, USA",
    posted="June 22, 2026",
    schedule="Full Time",
    category="Data Science",
    desc="<p>Build.</p>",
):
    return {
        "id_icims": icims,
        "title": title,
        "job_path": path if path is not None else f"/en/jobs/{icims}/role",
        "normalized_location": loc,
        # Real field is ``posted_date`` (verified live 2026-06-22) — NOT
        # ``posting_date``, which silently returned None on every Amazon job.
        "posted_date": posted,
        "job_schedule_type": schedule,
        "job_category": category,
        "description": desc,
    }


class TestScanAmazon:
    @patch("jobcannon.engine.ats_platforms._platforms_amazon.get_session")
    def test_returns_matched_jobs_with_field_mapping(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_amazon

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "hits": 2,
            "jobs": [
                _job("Senior Data Scientist", icims="111"),
                _job("Warehouse Associate", icims="222"),
            ],
        }
        mock_get.return_value = resp

        results = scan_amazon("data scientist", ["data scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Amazon"
        assert job["source_url"] == "https://www.amazon.jobs/en/jobs/111/role"
        assert job["source_id"] == "111"
        assert job["location"] == "Seattle, Washington, USA"
        assert job["posted_date"] == "2026-06-22"
        assert job["employment_type"] == "Full Time"
        assert job["department"] == "Data Science"
        assert "Build" in job["description"]

    @patch("jobcannon.engine.ats_platforms._platforms_amazon.get_session")
    def test_posted_date_parsing_variants(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_amazon

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "hits": 3,
            "jobs": [
                _job("Data Scientist Full", icims="1", posted="June 22, 2026"),
                _job("Data Scientist Abbr", icims="2", posted="Jun 22, 2026"),
                _job("Data Scientist Bad", icims="3", posted="sometime soon"),
            ],
        }
        mock_get.return_value = resp

        by_id = {j["source_id"]: j for j in scan_amazon("", ["data scientist"], [])}
        assert by_id["1"]["posted_date"] == "2026-06-22"
        assert by_id["2"]["posted_date"] == "2026-06-22"
        assert by_id["3"]["posted_date"] is None  # unparseable → None (D-08)

    @patch("jobcannon.engine.ats_platforms._platforms_amazon.get_session")
    def test_is_remote_from_virtual_text(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_amazon

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "hits": 2,
            "jobs": [
                _job("Data Scientist Virtual", icims="1", loc="Virtual Location, USA"),
                _job("Data Scientist Onsite", icims="2", loc="Seattle, Washington, USA"),
            ],
        }
        mock_get.return_value = resp

        by_id = {j["source_id"]: j for j in scan_amazon("", ["data scientist"], [])}
        assert by_id["1"]["is_remote"] is True
        assert by_id["2"]["is_remote"] is None

    @patch("jobcannon.engine.ats_platforms._platforms_amazon.get_session")
    def test_short_page_stops_pagination(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_amazon

        # One page of 5 jobs (< _PAGE_SIZE) → loop stops after one request.
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "hits": 9999,
            "jobs": [_job(f"Data Scientist {i}", icims=str(i)) for i in range(5)],
        }
        mock_get.return_value = resp

        results = scan_amazon("", ["data scientist"], [])
        assert len(results) == 5
        assert mock_get.call_count == 1

    @patch("jobcannon.engine.ats_platforms._platforms_amazon.get_session")
    def test_base_query_param_set_from_slug(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_amazon

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"hits": 0, "jobs": []}
        mock_get.return_value = resp

        scan_amazon("data scientist", ["data scientist"], [])
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["base_query"] == "data scientist"
        assert kwargs["params"]["sort"] == "recent"

    @patch("jobcannon.engine.ats_platforms._platforms_amazon.get_session")
    def test_http_404_returns_empty_no_raise(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        # Amazon is a global board — a 404 is NOT BoardGoneError; just an empty result.
        from jobcannon.engine.ats_platforms import scan_amazon

        mock_get.return_value = MagicMock(status_code=404)
        assert scan_amazon("data scientist", ["data scientist"], []) == []

    @patch("jobcannon.engine.ats_platforms._platforms_amazon.get_session")
    def test_request_exception_returns_empty(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_amazon

        mock_get.side_effect = Exception("network error")
        assert scan_amazon("data scientist", ["data scientist"], []) == []

    @patch("jobcannon.engine.ats_platforms._platforms_amazon.get_session")
    def test_multi_query_slug_unions_and_dedups(self, mock_get_session):
        """A '|'-delimited slug runs each focused query and merges by id_icims.

        Amazon's global board + 2000-cap means a single broad keyword drowns
        genuine matches; the focused-query union is the fix. The same posting
        surfacing under two queries must appear once.
        """
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_amazon

        def _resp(jobs):
            r = MagicMock(status_code=200)
            r.json.return_value = {"hits": len(jobs), "jobs": jobs}
            return r

        def _side_effect(url, params=None, **kwargs):
            q = (params or {}).get("base_query", "")
            if q == "data scientist":
                return _resp([_job("Data Scientist", icims="1")])
            if q == "data analyst":
                # id 1 is shared with the 'data scientist' query -> must dedup.
                return _resp([_job("Data Analyst", icims="2"), _job("Data Scientist", icims="1")])
            return _resp([])

        mock_get.side_effect = _side_effect

        results = scan_amazon("data scientist|data analyst", ["data scientist", "data analyst"], [])
        assert sorted(j["source_id"] for j in results) == ["1", "2"]
        # both focused queries were actually issued
        issued = {c.kwargs["params"].get("base_query") for c in mock_get.call_args_list}
        assert {"data scientist", "data analyst"} <= issued

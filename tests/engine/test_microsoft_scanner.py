"""Tests for the Microsoft Careers (Phenom pcsx) ATS scanner."""

from unittest.mock import MagicMock, patch

import pytest

from tests.engine.helpers.ats_session import ats_session_method


def _position(name, *, pid="111", locations=None, work="remote", posted_ts=1700000000, dept="Data"):
    return {
        "id": pid,
        "name": name,
        "locations": locations if locations is not None else ["Redmond, WA, US"],
        "positionUrl": f"/careers/job/{pid}",
        "postedTs": posted_ts,
        "workLocationOption": work,
        "department": dept,
    }


class TestScanMicrosoft:
    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_returns_matched_jobs_with_field_mapping(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_microsoft

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            # The API ALWAYS returns an empty error object on success (verified
            # live 2026-06-22). A truthy-dict check here aborted every page.
            "error": {"message": "", "body": ""},
            "data": {
                "count": 2,
                "positions": [
                    _position("Senior Data Scientist", pid="111"),
                    _position("Retail Associate", pid="222"),
                ],
            },
        }
        mock_get.return_value = resp

        results = scan_microsoft("microsoft.com", ["data scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Microsoft Careers"
        assert job["source_url"] == "https://apply.careers.microsoft.com/careers/job/111"
        assert job["source_id"] == "111"
        assert job["location"] == "Redmond, WA, US"
        assert job["posted_date"] == "2023-11-14"
        assert job["is_remote"] is True
        assert job["department"] == "Data"

    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_is_remote_tri_state(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_microsoft

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "data": {
                "count": 3,
                "positions": [
                    _position("Data Scientist A", pid="1", work="onsite"),
                    _position("Data Scientist B", pid="2", work="Remote"),
                    {"id": "3", "name": "Data Scientist C", "locations": ["NYC"]},
                ],
            }
        }
        mock_get.return_value = resp

        by_id = {j["source_id"]: j for j in scan_microsoft("microsoft.com", ["data scientist"], [])}
        assert by_id["1"]["is_remote"] is False
        assert by_id["2"]["is_remote"] is True
        assert by_id["3"]["is_remote"] is None  # workLocationOption absent

    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_domain_param_from_slug(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_microsoft

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"count": 0, "positions": []}}
        mock_get.return_value = resp

        scan_microsoft("microsoft", ["data scientist"], [])  # bare slug → default domain
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["domain"] == "microsoft.com"

    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_http_error_returns_empty(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_microsoft

        mock_get.return_value = MagicMock(status_code=500)
        assert scan_microsoft("microsoft.com", ["data scientist"], []) == []


class TestMicrosoftCompleteness:
    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_paginates_and_is_complete(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms._platforms_microsoft import (
            _fetch_postings_with_completeness,
        )

        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "data": {"count": 12, "positions": [_position(f"J{i}", pid=str(i)) for i in range(10)]}
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {
            "data": {
                "count": 12,
                "positions": [_position(f"J{i}", pid=str(i)) for i in range(10, 12)],
            }
        }
        mock_get.side_effect = [page1, page2]

        postings, complete = _fetch_postings_with_completeness("microsoft.com")
        assert len(postings) == 12
        assert complete is True
        assert mock_get.call_count == 2

    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_empty_board_is_complete(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms._platforms_microsoft import (
            _fetch_postings_with_completeness,
        )

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": {"count": 0, "positions": []}}
        mock_get.return_value = resp

        postings, complete = _fetch_postings_with_completeness("microsoft.com")
        assert postings == []
        assert complete is True

    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_first_page_404_raises_board_gone(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms._platforms_microsoft import (
            _fetch_postings_with_completeness,
        )
        from jobcannon.engine.ats_platforms._registry import BoardGoneError

        mock_get.return_value = MagicMock(status_code=404)
        with pytest.raises(BoardGoneError) as exc_info:
            _fetch_postings_with_completeness("defunct.com")
        assert exc_info.value.status == 404

    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_first_page_403_does_not_raise(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms._platforms_microsoft import (
            _fetch_postings_with_completeness,
        )

        mock_get.return_value = MagicMock(status_code=403)
        postings, complete = _fetch_postings_with_completeness("blocked.com")
        assert postings == []
        assert complete is False

    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_empty_error_object_does_not_abort(self, mock_get_session):
        """Regression: the always-present empty ``error`` object is not an error.

        The live API returns ``error: {"message": "", "body": ""}`` on every
        successful response; a truthy-dict check aborted the fetch and returned
        zero jobs against a board with 1,382 live positions.
        """
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms._platforms_microsoft import (
            _fetch_postings_with_completeness,
        )

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "error": {"message": "", "body": ""},
            "data": {"count": 1, "positions": [_position("Data Scientist", pid="9")]},
        }
        mock_get.return_value = resp

        postings, complete = _fetch_postings_with_completeness("microsoft.com")
        assert len(postings) == 1
        assert complete is True

    @patch("jobcannon.engine.ats_platforms._platforms_microsoft.get_session")
    def test_nonempty_error_message_aborts(self, mock_get_session):
        """A real (non-empty) error message stops pagination."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms._platforms_microsoft import (
            _fetch_postings_with_completeness,
        )

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "error": {"message": "rate limited", "body": ""},
            "data": {"count": 5, "positions": [_position("Data Scientist", pid="9")]},
        }
        mock_get.return_value = resp

        postings, complete = _fetch_postings_with_completeness("microsoft.com")
        assert postings == []
        assert complete is False

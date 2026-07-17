"""Tests for the Eightfold (SmartApply) ATS scanner — Netflix and generic tenants."""

from unittest.mock import MagicMock, patch

import pytest

from tests.engine.helpers.ats_session import ats_session_method

_NETFLIX_SLUG = "explore.jobs.netflix.net|netflix.com"


def _position(
    name, *, pid="40875", location="USA - Remote", dept="Data & Insights", canonical=None
):
    return {
        "id": pid,
        "name": name,
        "location": location,
        "locations": [location],
        "canonicalPositionUrl": canonical
        if canonical is not None
        else f"https://explore.jobs.netflix.net/careers/job/{pid}",
        "department": dept,
        "t_create": 1700000000,  # must be ignored (repost-reset → posted_date None)
    }


class TestScanEightfold:
    @patch("jobcannon.engine.ats_platforms._platforms_eightfold.get_session")
    def test_returns_matched_jobs_with_field_mapping(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_eightfold

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "count": 2,
            "positions": [
                _position("Senior Data Scientist", pid="40875"),
                _position("Office Manager", pid="40999"),
            ],
        }
        mock_get.return_value = resp

        results = scan_eightfold(_NETFLIX_SLUG, ["data scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Eightfold"
        assert job["source_url"] == "https://explore.jobs.netflix.net/careers/job/40875"
        assert job["source_id"] == "40875"
        assert job["is_remote"] is True  # "USA - Remote"
        assert job["posted_date"] is None  # t_create unreliable → never stored
        assert job["department"] == "Data & Insights"

    @patch("jobcannon.engine.ats_platforms._platforms_eightfold.get_session")
    def test_host_and_domain_parsed_from_slug(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_eightfold

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"count": 0, "positions": []}
        mock_get.return_value = resp

        scan_eightfold(_NETFLIX_SLUG, ["data scientist"], [])
        args, kwargs = mock_get.call_args
        assert args[0] == "https://explore.jobs.netflix.net/api/apply/v2/jobs"
        assert kwargs["params"]["domain"] == "netflix.com"

    @patch("jobcannon.engine.ats_platforms._platforms_eightfold.get_session")
    def test_bare_slug_defaults_to_eightfold_ai_host(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_eightfold

        resp = MagicMock(status_code=200)
        resp.json.return_value = {"count": 0, "positions": []}
        mock_get.return_value = resp

        scan_eightfold("acme", ["data scientist"], [])
        args, kwargs = mock_get.call_args
        assert args[0] == "https://acme.eightfold.ai/api/apply/v2/jobs"
        assert kwargs["params"]["domain"] == "acme"

    @patch("jobcannon.engine.ats_platforms._platforms_eightfold.get_session")
    def test_is_remote_none_for_onsite_text(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_eightfold

        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "count": 1,
            "positions": [_position("Data Scientist", location="New York, NY")],
        }
        mock_get.return_value = resp

        results = scan_eightfold(_NETFLIX_SLUG, ["data scientist"], [])
        assert results[0]["is_remote"] is None

    @patch("jobcannon.engine.ats_platforms._platforms_eightfold.get_session")
    def test_request_exception_returns_empty(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_eightfold

        mock_get.side_effect = Exception("network error")
        assert scan_eightfold(_NETFLIX_SLUG, ["data scientist"], []) == []


class TestEightfoldCompleteness:
    @patch("jobcannon.engine.ats_platforms._platforms_eightfold.get_session")
    def test_paginates_top_level_positions(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms._platforms_eightfold import (
            _fetch_postings_with_completeness,
        )

        page1 = MagicMock(status_code=200)
        page1.json.return_value = {
            "count": 12,
            "positions": [_position(f"J{i}", pid=str(i)) for i in range(10)],
        }
        page2 = MagicMock(status_code=200)
        page2.json.return_value = {
            "count": 12,
            "positions": [_position(f"J{i}", pid=str(i)) for i in range(10, 12)],
        }
        mock_get.side_effect = [page1, page2]

        postings, complete = _fetch_postings_with_completeness(_NETFLIX_SLUG)
        assert len(postings) == 12
        assert complete is True

    @patch("jobcannon.engine.ats_platforms._platforms_eightfold.get_session")
    def test_first_page_410_raises_board_gone(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms._platforms_eightfold import (
            _fetch_postings_with_completeness,
        )
        from jobcannon.engine.ats_platforms._registry import BoardGoneError

        mock_get.return_value = MagicMock(status_code=410)
        with pytest.raises(BoardGoneError) as exc_info:
            _fetch_postings_with_completeness(_NETFLIX_SLUG)
        assert exc_info.value.status == 410

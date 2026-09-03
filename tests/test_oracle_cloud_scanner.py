"""Tests for the Oracle Recruiting Cloud (Fusion CE) platform scanner.

Covers URL detection (host + site → "{host}|{site}" slug, region variants,
negatives), the scanner's requisitionList parsing + offset pagination +
BoardGoneError, the canonical job-dict mapping, and dispatch registration.
"""

import re
from unittest.mock import MagicMock, patch

import pytest

from jobcannon.engine.ats_detection import extract_ats_from_url_best
from jobcannon.engine.ats_platforms import SCANNERS_BY_NAME, scan_oracle_cloud
from jobcannon.engine.ats_platforms import _platforms_oracle_cloud as orc
from jobcannon.engine.ats_platforms._registry import BoardGoneError
from tests.helpers.ats_session import ats_session_method

_HOST = "ibtcjb.fa.ocs.oraclecloud.com"


# ── URL detection ────────────────────────────────────────────────────────────


def test_ce_url_with_site_returns_host_and_site():
    url = f"https://{_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/requisitions"
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{_HOST}|CX_1", 5)


def test_ce_url_without_site_defaults_cx1():
    url = f"https://{_HOST}/hcmUI/CandidateExperience/en/"
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{_HOST}|CX_1", 5)


def test_rest_api_url_extracts_site_number():
    url = (
        f"https://{_HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        "?finder=findReqs;siteNumber=CX_2,limit=25"
    )
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{_HOST}|CX_2", 5)


def test_region_variant_host_preserved():
    host = "evfc.fa.us2.oraclecloud.com"
    url = f"https://{host}/hcmUI/CandidateExperience/en/sites/CX_3/jobs"
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{host}|CX_3", 5)


def test_host_lowercased():
    url = "https://IBTCJB.FA.OCS.ORACLECLOUD.COM/hcmUI/CandidateExperience/en/sites/CX_1/"
    assert extract_ats_from_url_best(url) == ("oracle_cloud", f"{_HOST}|CX_1", 5)


def test_oracle_marketing_host_returns_none():
    # Oracle's own product/marketing host is not a Fusion pod -> not an ATS.
    assert extract_ats_from_url_best("https://www.oracle.com/careers/") is None


def test_non_oracle_url_returns_none():
    assert extract_ats_from_url_best("https://boards.greenhouse.io/acme") == (
        "greenhouse",
        "acme",
        5,
    )
    assert extract_ats_from_url_best("https://acme.com/careers") is None


# ── Scanner behavior ─────────────────────────────────────────────────────────


def _resp(status: int, payload: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    return r


def _page(reqs: list[dict], total: int) -> dict:
    return {"items": [{"TotalJobsCount": total, "requisitionList": reqs}]}


def _req(rid: str, title: str, **extra) -> dict:
    base = {
        "Id": rid,
        "Title": title,
        "PostedDate": "2026-06-23",
        "PrimaryLocation": "Austin, TX, United States",
        "WorkplaceTypeCode": "ORA_ON_SITE",
        "ShortDescriptionStr": "A short blurb.",
    }
    base.update(extra)
    return base


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_fetch_postings_single_page(mock_get_session):
    mock_get = ats_session_method(mock_get_session, "get")
    mock_get.return_value = _resp(200, _page([_req("1", "Data Analyst")], total=1))
    out = orc._fetch_postings(f"{_HOST}|CX_1")
    assert [r["Id"] for r in out] == ["1"]


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_fetch_postings_paginates(mock_get_session, monkeypatch):
    mock_get = ats_session_method(mock_get_session, "get")
    # Shrink the page size so two small pages exercise the offset loop.
    monkeypatch.setattr(orc, "_PAGE_SIZE", 2)
    mock_get.side_effect = [
        _resp(200, _page([_req("1", "A"), _req("2", "B")], total=3)),
        _resp(200, _page([_req("3", "C")], total=3)),
    ]
    out = orc._fetch_postings(f"{_HOST}|CX_1")
    assert [r["Id"] for r in out] == ["1", "2", "3"]
    assert mock_get.call_count == 2


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_first_page_404_raises_board_gone(mock_get_session):
    mock_get = ats_session_method(mock_get_session, "get")
    mock_get.return_value = _resp(404)
    with pytest.raises(BoardGoneError):
        orc._fetch_postings(f"{_HOST}|CX_1")


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_empty_board_is_clean_miss(mock_get_session):
    mock_get = ats_session_method(mock_get_session, "get")
    mock_get.return_value = _resp(200, _page([], total=0))
    assert orc._fetch_postings(f"{_HOST}|CX_1") == []


def test_posting_to_job_builds_canonical_dict():
    posting = _req("42251", "Junior Intelligence Analyst", WorkplaceTypeCode="ORA_REMOTE")
    job = orc._posting_to_job(posting, f"{_HOST}|CX_1")
    assert job["title"] == "Junior Intelligence Analyst"
    assert job["company_source"] == "Oracle Cloud"
    assert job["location"] == "Austin, TX, United States"
    assert job["posted_date"] == "2026-06-23"
    assert job["is_remote"] is True
    assert job["source_id"] == "42251"
    assert job["source_url"] == (
        f"https://{_HOST}/hcmUI/CandidateExperience/en/sites/CX_1/job/42251"
    )
    assert job["salary_min"] is None and job["salary_max"] is None


def test_posting_to_job_onsite_is_not_remote():
    job = orc._posting_to_job(_req("9", "Engineer"), f"{_HOST}|CX_1")
    assert job["is_remote"] is False


def test_posting_missing_id_is_skipped():
    assert orc._posting_to_job({"Title": "x"}, f"{_HOST}|CX_1") is None


# ── Dispatch registration ────────────────────────────────────────────────────


def test_oracle_cloud_registered_for_dispatch():
    assert "oracle_cloud" in SCANNERS_BY_NAME
    from jobcannon.engine.ats_scanner._run import _PLATFORM_SCANNERS

    assert "oracle_cloud" in _PLATFORM_SCANNERS


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_scan_oracle_cloud_title_gate(mock_get_session):
    mock_get = ats_session_method(mock_get_session, "get")
    mock_get.return_value = _resp(
        200,
        _page(
            [_req("1", "Senior Data Analyst"), _req("2", "Line Cook")],
            total=2,
        ),
    )
    jobs = scan_oracle_cloud(f"{_HOST}|CX_1", ["Data Analyst"], [])
    assert [j["title"] for j in jobs] == ["Senior Data Analyst"]


# ── Parallel page-fetch concurrency (issue #1029) ────────────────────────────


def _offset_of(url: str) -> int:
    """Extract the ``offset=`` query value embedded in an ORC finder URL."""
    m = re.search(r"offset=(\d+)", url)
    assert m is not None, f"no offset= in URL: {url}"
    return int(m.group(1))


# DROPPED test (port L-group jobcannon/engine) [test_page_fetch_respects_concurrency_bound_with_overlap]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


# DROPPED test (port L-group jobcannon/engine) [test_page_fetch_concurrency_clamps_to_range]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


# DROPPED test (port L-group jobcannon/engine) [test_page_fetch_failure_isolated_to_that_page]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


# DROPPED test (port L-group jobcannon/engine) [test_parallel_pages_assembled_in_offset_order_despite_completion_order]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


# ── Completeness signal (issue #1092) ─────────────────────────────────────────


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_completeness_single_page_complete(mock_get_session):
    """Single-page fetch with total matching fetched count returns complete=True."""
    mock_get = ats_session_method(mock_get_session, "get")
    mock_get.return_value = _resp(200, _page([_req("1", "Data Analyst")], total=1))
    postings, complete = orc._fetch_postings_with_completeness(f"{_HOST}|CX_1")
    assert len(postings) == 1
    assert complete is True


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_completeness_empty_board_complete(mock_get_session):
    """Genuinely empty board (total=0) returns complete=True."""
    mock_get = ats_session_method(mock_get_session, "get")
    mock_get.return_value = _resp(200, _page([], total=0))
    postings, complete = orc._fetch_postings_with_completeness(f"{_HOST}|CX_1")
    assert postings == []
    assert complete is True


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_completeness_first_page_error_incomplete(mock_get_session):
    """First-page error (network/HTTP/JSON) returns complete=False."""
    mock_get = ats_session_method(mock_get_session, "get")
    mock_get.return_value = _resp(500)
    postings, complete = orc._fetch_postings_with_completeness(f"{_HOST}|CX_1")
    assert postings == []
    assert complete is False


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_completeness_paginated_all_pages_complete(mock_get_session, monkeypatch):
    """Multi-page fetch where all pages land returns complete=True."""
    mock_get = ats_session_method(mock_get_session, "get")
    monkeypatch.setattr(orc, "_PAGE_SIZE", 2)
    mock_get.side_effect = [
        _resp(200, _page([_req("1", "A"), _req("2", "B")], total=3)),
        _resp(200, _page([_req("3", "C")], total=3)),
    ]
    postings, complete = orc._fetch_postings_with_completeness(f"{_HOST}|CX_1")
    assert [r["Id"] for r in postings] == ["1", "2", "3"]
    assert complete is True


# DROPPED test (port L-group jobcannon/engine) [test_completeness_parallel_page_failure_incomplete]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


@patch("jobcannon.engine.ats_platforms._platforms_oracle_cloud.get_session")
def test_completeness_exceeds_max_results_incomplete(mock_get_session, monkeypatch):
    """Board larger than _MAX_RESULTS returns complete=False."""
    mock_get = ats_session_method(mock_get_session, "get")
    monkeypatch.setattr(orc, "_PAGE_SIZE", 50)
    monkeypatch.setattr(orc, "_MAX_RESULTS", 100)
    # total=200 but we cap at 100 (2 pages)
    mock_get.side_effect = [
        _resp(200, _page([_req(str(i), f"Job {i}") for i in range(50)], total=200)),
        _resp(200, _page([_req(str(i), f"Job {i}") for i in range(50, 100)], total=200)),
    ]
    postings, complete = orc._fetch_postings_with_completeness(f"{_HOST}|CX_1")
    assert len(postings) == 100
    assert complete is False

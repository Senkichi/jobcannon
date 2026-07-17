"""Tests for the UKG Pro Recruiting (UltiPro) platform scanner.

Covers URL detection (host/tenant/board GUID → "{host}/{tenant}/{board}" slug,
tenant-case preservation, recruiting vs recruiting2, negatives), the scanner's
opportunities parsing + Top/Skip pagination + BoardGoneError, and the canonical
job-dict mapping (including the numeric-JobLocationType regression).
"""

from unittest.mock import MagicMock, patch

import pytest

from jobcannon.engine.ats_detection import extract_ats_from_url_best
from jobcannon.engine.ats_platforms import scan_ultipro
from jobcannon.engine.ats_platforms import _platforms_ultipro as ukg
from jobcannon.engine.ats_platforms._registry import BoardGoneError
from tests.engine.helpers.ats_session import ats_session_method

_HOST = "recruiting2.ultipro.com"
_TENANT = "JAN1000JANI"
_BOARD = "693b35f4-c147-4487-97c3-600a31f4816b"
_SLUG = f"{_HOST}/{_TENANT}/{_BOARD}"


# ── URL detection ────────────────────────────────────────────────────────────


def test_board_url_returns_host_tenant_board():
    url = f"https://{_HOST}/{_TENANT}/JobBoard/{_BOARD}"
    assert extract_ats_from_url_best(url) == ("ultipro", _SLUG, 5)


def test_opportunity_detail_url_matches():
    url = f"https://{_HOST}/{_TENANT}/JobBoard/{_BOARD}/OpportunityDetail?opportunityId=abc"
    assert extract_ats_from_url_best(url) == ("ultipro", _SLUG, 5)


def test_tenant_case_preserved_host_lowercased():
    url = f"https://RECRUITING2.ULTIPRO.COM/{_TENANT}/JobBoard/{_BOARD.upper()}"
    # host lowercased, GUID lowercased, tenant case preserved
    assert extract_ats_from_url_best(url) == ("ultipro", _SLUG, 5)


def test_legacy_recruiting_subdomain_matches():
    url = f"https://recruiting.ultipro.com/{_TENANT}/JobBoard/{_BOARD}"
    assert extract_ats_from_url_best(url) == (
        "ultipro",
        f"recruiting.ultipro.com/{_TENANT}/{_BOARD}",
        5,
    )


def test_tenant_only_url_without_board_returns_none():
    # No board GUID -> not enough to scan; detection abstains.
    assert extract_ats_from_url_best(f"https://{_HOST}/{_TENANT}/JobBoard") is None


def test_non_ultipro_url_returns_none():
    assert extract_ats_from_url_best("https://ultipro.com/about") is None


# ── Scanner behavior ─────────────────────────────────────────────────────────


def _resp(status: int, payload: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    return r


def _page(ops: list[dict], total: int) -> dict:
    return {"opportunities": ops, "totalCount": total, "locations": []}


def _op(oid: str, title: str, **extra) -> dict:
    base = {
        "Id": oid,
        "Title": title,
        "RequisitionNumber": "REQ001",
        "FullTime": "true",
        "JobCategoryName": "Engineering",
        "Locations": [{"LocalizedName": "Seattle, WA"}],
        "PostedDate": "2026-06-23T15:24:52.749Z",
        "BriefDescription": "A short blurb.",
        "JobLocationType": None,
    }
    base.update(extra)
    return base


@patch("jobcannon.engine.ats_platforms._platforms_ultipro.get_session")
def test_fetch_postings_single_page(mock_get_session):
    mock_post = ats_session_method(mock_get_session, "post")
    mock_post.return_value = _resp(200, _page([_op("1", "Data Analyst")], total=1))
    out = ukg._fetch_postings(_SLUG)
    assert [o["Id"] for o in out] == ["1"]


@patch("jobcannon.engine.ats_platforms._platforms_ultipro.get_session")
def test_fetch_postings_paginates_via_skip(mock_get_session, monkeypatch):
    mock_post = ats_session_method(mock_get_session, "post")
    monkeypatch.setattr(ukg, "_PAGE_SIZE", 2)
    mock_post.side_effect = [
        _resp(200, _page([_op("1", "A"), _op("2", "B")], total=3)),
        _resp(200, _page([_op("3", "C")], total=3)),
    ]
    out = ukg._fetch_postings(_SLUG)
    assert [o["Id"] for o in out] == ["1", "2", "3"]
    assert mock_post.call_count == 2
    # Second request advances Skip by the page size.
    assert mock_post.call_args_list[1].kwargs["json"]["opportunitySearch"]["Skip"] == 2


@patch("jobcannon.engine.ats_platforms._platforms_ultipro.get_session")
def test_first_page_404_raises_board_gone(mock_get_session):
    mock_post = ats_session_method(mock_get_session, "post")
    mock_post.return_value = _resp(404)
    with pytest.raises(BoardGoneError):
        ukg._fetch_postings(_SLUG)


@patch("jobcannon.engine.ats_platforms._platforms_ultipro.get_session")
def test_empty_board_is_clean_miss(mock_get_session):
    mock_post = ats_session_method(mock_get_session, "post")
    mock_post.return_value = _resp(200, _page([], total=0))
    assert ukg._fetch_postings(_SLUG) == []


def test_malformed_slug_no_fetch():
    assert ukg._fetch_postings("recruiting2.ultipro.com/onlytenant") == []


def test_posting_to_job_builds_canonical_dict():
    job = ukg._posting_to_job(_op("508dd542", "Lathe Machinist"), _SLUG)
    assert job["title"] == "Lathe Machinist"
    assert job["company_source"] == "UltiPro"
    assert job["location"] == "Seattle, WA"
    assert job["posted_date"] == "2026-06-23"  # ISO datetime truncated to date
    assert job["employment_type"] == "Full Time"
    assert job["department"] == "Engineering"
    assert job["source_id"] == "508dd542"
    assert job["source_url"] == (
        f"https://{_HOST}/{_TENANT}/JobBoard/{_BOARD}/OpportunityDetail?opportunityId=508dd542"
    )


def test_location_falls_back_to_address():
    op = _op("9", "Engineer", Locations=[{"Address": {"City": "Austin", "State": {"Code": "TX"}}}])
    assert ukg._posting_to_job(op, _SLUG)["location"] == "Austin, TX"


def test_numeric_job_location_type_does_not_crash():
    # Regression: some tenants emit JobLocationType as a non-zero int -> str()
    # coercion must keep _is_remote from raising (live-discovered).
    job = ukg._posting_to_job(_op("1", "x", JobLocationType=2), _SLUG)
    assert job["is_remote"] is None


def test_remote_string_detected():
    assert ukg._posting_to_job(_op("1", "x", JobLocationType="Remote"), _SLUG)["is_remote"] is True


def test_posting_missing_id_skipped():
    assert ukg._posting_to_job({"Title": "x"}, _SLUG) is None


# test_ultipro_registered_for_dispatch deliberately not ported: it imports
# jobcannon.engine.ats_scanner._run, which is Task 3 scope (not ported by
# this PR). Registry-side coverage lives in test_ats_registry_completeness.py.


@patch("jobcannon.engine.ats_platforms._platforms_ultipro.get_session")
def test_scan_ultipro_title_gate(mock_get_session):
    mock_post = ats_session_method(mock_get_session, "post")
    mock_post.return_value = _resp(
        200, _page([_op("1", "Senior Data Analyst"), _op("2", "Line Cook")], total=2)
    )
    jobs = scan_ultipro(_SLUG, ["Data Analyst"], [])
    assert [j["title"] for j in jobs] == ["Senior Data Analyst"]

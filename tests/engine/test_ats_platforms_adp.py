"""Tests for ADP Workforce Now ATS platform scanner."""

from urllib.parse import urlparse

from jobcannon.engine.ats_platforms._platforms_adp import (
    SCANNER,
    _employment_type,
    _extract_external_job_id,
    _is_remote,
    _job_url,
    _location,
)


def test_extract_external_job_id():
    """Test ExternalJobID extraction from customFieldGroup.stringFields."""
    posting = {
        "customFieldGroup": {
            "stringFields": [
                {"nameCode": {"codeValue": "ExternalJobID"}, "stringValue": "634946"},
                {"nameCode": {"codeValue": "OtherField"}, "stringValue": "value"},
            ]
        }
    }
    assert _extract_external_job_id(posting) == "634946"

    # Missing ExternalJobID
    posting = {
        "customFieldGroup": {
            "stringFields": [{"nameCode": {"codeValue": "OtherField"}, "stringValue": "value"}]
        }
    }
    assert _extract_external_job_id(posting) is None

    # No customFieldGroup
    posting = {}
    assert _extract_external_job_id(posting) is None


def test_location():
    """Test location extraction from requisitionLocations."""
    posting = {
        "requisitionLocations": [
            {"nameCode": {"shortName": "San Francisco, CA"}, "itemID": "12345"}
        ]
    }
    assert _location(posting) == "San Francisco, CA"

    # Fallback to itemID
    posting = {"requisitionLocations": [{"itemID": "12345"}]}
    assert _location(posting) == "12345"

    # No locations
    posting = {}
    assert _location(posting) == ""


def test_is_remote():
    """Test remote indicator extraction."""
    posting = {
        "customFieldGroup": {
            "indicatorFields": [{"nameCode": {"codeValue": "Remote"}, "indicatorValue": True}]
        }
    }
    assert _is_remote(posting) is True

    posting = {
        "customFieldGroup": {
            "indicatorFields": [{"nameCode": {"codeValue": "Remote"}, "indicatorValue": False}]
        }
    }
    assert _is_remote(posting) is False

    # No remote field
    posting = {"customFieldGroup": {"indicatorFields": []}}
    assert _is_remote(posting) is None


def test_employment_type():
    """Test employment type extraction."""
    posting = {"workLevelCode": {"shortName": "Full Time"}}
    assert _employment_type(posting) == "Full Time"

    # Fallback to custom field
    posting = {
        "customFieldGroup": {
            "codeFields": [{"nameCode": {"codeValue": "SalaryType"}, "shortName": "Hourly"}]
        }
    }
    assert _employment_type(posting) == "Hourly"

    # No employment type
    posting = {}
    assert _employment_type(posting) is None


def test_job_url():
    """Test job URL construction."""
    assert (
        _job_url("a6717ebc-f6a8-4a51-856b-f7ebd573645e", "634946")
        == "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=a6717ebc-f6a8-4a51-856b-f7ebd573645e&ccId=19000101_000001&job=634946"
    )


def test_scanner_contract():
    """Test that the scanner has the required contract."""
    assert SCANNER.name == "adp"
    assert SCANNER.company_source == "ADP"
    assert callable(SCANNER.fetch_postings)
    assert callable(SCANNER.title_of)
    assert callable(SCANNER.posting_to_job)


def test_scanner_title_of():
    """Test title extraction from posting dict."""
    posting = {"requisitionTitle": "Software Engineer"}
    assert SCANNER.title_of(posting) == "Software Engineer"


def test_scanner_posting_to_job():
    """Test conversion to canonical job dict."""
    posting = {
        "itemID": "9201405840307_1",
        "requisitionTitle": "CNC Machinist",
        "postDate": "2026-06-25T14:50:00.000-04:00",
        "customFieldGroup": {
            "stringFields": [{"nameCode": {"codeValue": "ExternalJobID"}, "stringValue": "634946"}]
        },
        "requisitionLocations": [
            {"nameCode": {"shortName": "San Francisco, CA"}, "itemID": "12345"}
        ],
    }
    job = SCANNER.posting_to_job(posting, "a6717ebc-f6a8-4a51-856b-f7ebd573645e")
    assert job["title"] == "CNC Machinist"
    assert job["company_source"] == "ADP"
    assert job["location"] == "San Francisco, CA"
    assert job["source_id"] == "9201405840307_1"
    assert job["posted_date"] == "2026-06-25"
    assert urlparse(job["source_url"]).hostname == "workforcenow.adp.com"
    assert "634946" in job["source_url"]


def test_scanner_posting_to_job_missing_external_id():
    """Test posting_to_job when ExternalJobID is missing (falls back to itemID)."""
    posting = {
        "itemID": "9201405840307_1",
        "requisitionTitle": "CNC Machinist",
        "postDate": "2026-06-25T14:50:00.000-04:00",
        "customFieldGroup": {"stringFields": []},
        "requisitionLocations": [],
    }
    job = SCANNER.posting_to_job(posting, "a6717ebc-f6a8-4a51-856b-f7ebd573645e")
    assert job["title"] == "CNC Machinist"
    assert job["source_id"] == "9201405840307_1"
    assert "9201405840307_1" in job["source_url"]


def test_scanner_posting_to_job_missing_item_id():
    """Test posting_to_job when itemID is missing (returns None)."""
    posting = {
        "requisitionTitle": "CNC Machinist",
        "postDate": "2026-06-25T14:50:00.000-04:00",
    }
    job = SCANNER.posting_to_job(posting, "a6717ebc-f6a8-4a51-856b-f7ebd573645e")
    assert job is None

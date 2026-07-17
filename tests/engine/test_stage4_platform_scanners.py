"""Happy-path scan tests for the 7 Stage-4 platforms.

Prior to the polish-review F1 refactor, only Lever / Greenhouse / Ashby /
Workday / SmartRecruiters had direct unit tests; the Stage-4 additions
(Recruitee, Breezy, JazzHR, Pinpoint, Personio, BambooHR, Teamtailor)
relied on the integration path through ``_scan_one_company_via_ats_api``.

These tests assert each platform's posting-shape → canonical job dict
transformation in isolation, mocking ``requests.get`` at the
``ats_platforms`` module surface so the registry's
``run_platform_scan`` driver picks up the mock via the
``requests`` singleton.

Each platform has one or two tests covering: matched-job extraction,
keyword filter, HTTP-error path. Edge cases that already get exercised
through the registry driver tests (empty postings, exclusion AND-NOT
semantics) are not duplicated here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.engine.helpers.ats_session import ats_session_method

# ---------------------------------------------------------------------------
# Recruitee
# ---------------------------------------------------------------------------


class TestScanRecruitee:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_recruitee

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "offers": [
                {
                    "title": "Senior Data Scientist",
                    "city": "Berlin",
                    "country_code": "DE",
                    "description": "<p>Build models.</p>",
                    "careers_url": "https://acme.recruitee.com/o/sds",
                    "slug": "sds",
                },
                {
                    "title": "Marketing Manager",
                    "city": "Berlin",
                    "country_code": "DE",
                    "description": "Run campaigns.",
                    "careers_url": "https://acme.recruitee.com/o/mm",
                    "slug": "mm",
                },
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_recruitee("acme", ["data scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Recruitee"
        assert job["location"] == "Berlin, DE"
        assert "Build models" in job["description"]
        assert "<p>" not in job["description"]
        assert job["source_url"] == "https://acme.recruitee.com/o/sds"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_on_http_error(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_recruitee

        mock_get.return_value = MagicMock(status_code=500)
        assert scan_recruitee("acme", ["data scientist"], []) == []

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_location_falls_back_to_locations_list(self, mock_get_session):
        """When flat city/country are missing, derive from locations[0]."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_recruitee

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "offers": [
                {
                    "title": "Data Engineer",
                    "locations": [{"city": "London", "country": "UK"}],
                    "description": "Pipelines.",
                    "careers_url": "https://acme.recruitee.com/o/de",
                    "slug": "de",
                }
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_recruitee("acme", ["data engineer"], [])
        assert results[0]["location"] == "London, UK"


# ---------------------------------------------------------------------------
# Breezy
# ---------------------------------------------------------------------------


class TestScanBreezy:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_matched_jobs_from_bare_list(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "name": "Staff Software Engineer",
                "location": {"city": "Austin", "state": "TX", "country": "US"},
                "url": "https://acme.breezy.hr/p/abc",
            },
            {
                "name": "Recruiter",
                "location": {"city": "NYC"},
                "url": "https://acme.breezy.hr/p/def",
            },
        ]
        mock_get.return_value = mock_resp

        results = scan_breezy("acme", ["software engineer"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Staff Software Engineer"
        assert job["company_source"] == "Breezy"
        assert job["location"] == "Austin, TX, US"
        assert job["source_url"] == "https://acme.breezy.hr/p/abc"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_accepts_positions_dict_form(self, mock_get_session):
        """Some tenants return {'positions': [...]} instead of a bare list."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "positions": [
                {
                    "name": "Data Engineer",
                    "location": {"is_remote": True},
                    "url": "https://acme.breezy.hr/p/de",
                }
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_breezy("acme", ["data engineer"], [])
        assert len(results) == 1
        assert results[0]["location"] == "Remote"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_on_http_error(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_breezy

        mock_get.return_value = MagicMock(status_code=404)
        assert scan_breezy("acme", ["data engineer"], []) == []

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_handles_nested_dict_in_location_field(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        # Regression: Maleda Tech returned location.state as a nested dict,
        # which crashed the scanner with "sequence item N: expected str
        # instance, dict found" inside the parts join. Scanner must skip
        # non-string sub-fields instead of crashing.
        from jobcannon.engine.ats_platforms import scan_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "name": "Staff Engineer",
                "location": {
                    "city": "Austin",
                    "state": {"name": "Texas", "code": "TX"},  # malformed nested dict
                    "country": "US",
                },
                "url": "https://acme.breezy.hr/p/abc",
            },
        ]
        mock_get.return_value = mock_resp

        results = scan_breezy("acme", ["engineer"], [])
        assert len(results) == 1
        # Non-string state is dropped; city and country survive.
        assert results[0]["location"] == "Austin, US"


# ---------------------------------------------------------------------------
# JazzHR
# ---------------------------------------------------------------------------


class TestScanJazzhr:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Senior Data Scientist",
                    "city": "Boston",
                    "state": "MA",
                    "country": "US",
                    "description": "<p>Build ML systems.</p>",
                    "apply_url": "https://acme.applytojob.com/apply/abc",
                },
                {
                    "title": "Customer Success Manager",
                    "city": "Boston",
                    "state": "MA",
                    "country": "US",
                    "description": "Renewals.",
                    "apply_url": "https://acme.applytojob.com/apply/def",
                },
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_jazzhr("acme", ["data scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "JazzHR"
        assert job["location"] == "Boston, MA, US"
        assert "Build ML systems" in job["description"]
        assert "<p>" not in job["description"]
        assert job["source_url"] == "https://acme.applytojob.com/apply/abc"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_constructs_source_url_from_board_code(self, mock_get_session):
        """When apply_url/link are absent, build one from {slug}/{board_code}."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Data Scientist",
                    "city": "Remote",
                    "description": "ML.",
                    "board_code": "BC123",
                }
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_jazzhr("acme", ["data scientist"], [])
        assert results[0]["source_url"] == "https://acme.applytojob.com/apply/BC123"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_on_http_error(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_jazzhr

        mock_get.return_value = MagicMock(status_code=403)
        assert scan_jazzhr("acme", ["data scientist"], []) == []


# ---------------------------------------------------------------------------
# Pinpoint
# ---------------------------------------------------------------------------


class TestScanPinpoint:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_pinpoint

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "data": [
                {
                    "title": "Senior Data Scientist",
                    "location": {"city": "Seattle", "province": "WA"},
                    "url": "https://acme.pinpointhq.com/postings/1",
                    "compensation_minimum": 150000,
                    "compensation_maximum": 200000,
                    "description": "<p>Build ML models.</p>",
                },
                {
                    "title": "Office Manager",
                    "location": {"city": "Seattle"},
                    "url": "https://acme.pinpointhq.com/postings/2",
                },
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_pinpoint("acme", ["data scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Pinpoint"
        assert job["location"] == "Seattle, WA"
        assert job["salary_min"] == 150000
        assert job["salary_max"] == 200000
        assert "Build ML models" in job["description"]
        assert "<p>" not in job["description"]
        assert job["source_url"] == "https://acme.pinpointhq.com/postings/1"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_on_http_error(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_pinpoint

        mock_get.return_value = MagicMock(status_code=502)
        assert scan_pinpoint("acme", ["data scientist"], []) == []

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_handles_non_numeric_salary(self, mock_get_session):
        """Salary fields that are not int/float become None."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_pinpoint

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "data": [
                {
                    "title": "Data Scientist",
                    "location": {"city": "Remote"},
                    "url": "https://acme.pinpointhq.com/postings/1",
                    "compensation_minimum": "150k",
                    "compensation_maximum": None,
                }
            ]
        }
        mock_get.return_value = mock_resp

        job = scan_pinpoint("acme", ["data scientist"], [])[0]
        assert job["salary_min"] is None
        assert job["salary_max"] is None


# ---------------------------------------------------------------------------
# Personio
# ---------------------------------------------------------------------------


class TestScanPersonio:
    _XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>42</id>
    <name>Senior Data Scientist</name>
    <office>Munich</office>
    <jobDescriptions>
      <jobDescription><name>About</name><value>Build models.</value></jobDescription>
      <jobDescription><name>Reqs</name><value>5+ years.</value></jobDescription>
    </jobDescriptions>
  </position>
  <position>
    <id>43</id>
    <name>Office Manager</name>
    <office>Munich</office>
    <jobDescriptions>
      <jobDescription><name>About</name><value>Run office.</value></jobDescription>
    </jobDescriptions>
  </position>
</workzag-jobs>
"""

    @patch("jobcannon.engine.ats_platforms._platforms_personio.get_session")
    def test_returns_matched_jobs_from_de_feed(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_personio

        mock_resp = MagicMock(status_code=200)
        mock_resp.content = self._XML_TEMPLATE.encode("utf-8")
        mock_get.return_value = mock_resp

        results = scan_personio("acme", ["data scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Personio"
        assert job["location"] == "Munich"
        assert "Build models" in job["description"]
        assert "5+ years" in job["description"]
        assert job["source_url"] == "https://acme.jobs.personio.de/job/42"

    @patch("jobcannon.engine.ats_platforms._platforms_personio.get_session")
    def test_falls_back_to_com_tld_on_de_404(self, mock_get_session):
        """If .de returns 404, retry on .com TLD."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_personio

        de_resp = MagicMock(status_code=404)
        com_resp = MagicMock(status_code=200)
        com_resp.content = self._XML_TEMPLATE.encode("utf-8")
        mock_get.side_effect = [de_resp, com_resp]

        results = scan_personio("acme", ["data scientist"], [])
        assert len(results) == 1
        assert mock_get.call_count == 2

    @patch("jobcannon.engine.ats_platforms._platforms_personio.get_session")
    def test_returns_empty_when_both_tlds_404(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_personio

        mock_get.return_value = MagicMock(status_code=404)
        assert scan_personio("acme", ["data scientist"], []) == []


# ---------------------------------------------------------------------------
# BambooHR
# ---------------------------------------------------------------------------


class TestScanBamboohr:
    _HTML_TEMPLATE = """
<html><body>
<ul>
  <li class="BambooHR-ATS-Jobs-Item">
    <a href="/careers/123">Senior Data Scientist</a>
    <span class="BambooHR-ATS-Location">Chicago, IL</span>
  </li>
  <li class="BambooHR-ATS-Jobs-Item">
    <a href="/careers/124">Office Manager</a>
    <span class="BambooHR-ATS-Location">Chicago, IL</span>
  </li>
</ul>
</body></html>
"""

    @patch("jobcannon.engine.ats_platforms._platforms_bamboohr.get_session")
    def test_returns_matched_jobs_from_html_widget(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_bamboohr

        mock_resp = MagicMock(status_code=200)
        mock_resp.text = self._HTML_TEMPLATE
        mock_get.return_value = mock_resp

        results = scan_bamboohr("acme", ["data scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "BambooHR"
        assert job["location"] == "Chicago, IL"
        assert job["source_url"] == "https://acme.bamboohr.com/careers/123"
        # Description is deliberately empty — listing has none, jd_full
        # is filled later by enrichment.
        assert job["description"] == ""

    @patch("jobcannon.engine.ats_platforms._platforms_bamboohr.get_session")
    def test_protocol_relative_href_gets_https(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_bamboohr

        mock_resp = MagicMock(status_code=200)
        mock_resp.text = """
<ul>
  <li class="BambooHR-ATS-Jobs-Item">
    <a href="//other.bamboohr.com/careers/9">Data Scientist</a>
    <span class="BambooHR-ATS-Location">Remote</span>
  </li>
</ul>
"""
        mock_get.return_value = mock_resp

        results = scan_bamboohr("acme", ["data scientist"], [])
        assert results[0]["source_url"] == "https://other.bamboohr.com/careers/9"

    @patch("jobcannon.engine.ats_platforms._platforms_bamboohr.get_session")
    def test_returns_empty_on_http_error(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_bamboohr

        mock_get.return_value = MagicMock(status_code=500)
        assert scan_bamboohr("acme", ["data scientist"], []) == []


# ---------------------------------------------------------------------------
# Teamtailor
# ---------------------------------------------------------------------------


class TestScanTeamtailor:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_teamtailor

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "data": [
                {
                    "attributes": {
                        "title": "Senior Data Scientist",
                        "body": "<p>Build models.</p>",
                        "city": "Stockholm",
                        "country": "SE",
                    },
                    "links": {
                        "careersite-job-url": "https://acme.teamtailor.com/jobs/1",
                    },
                },
                {
                    "attributes": {
                        "title": "HR Coordinator",
                        "body": "Coordinate.",
                        "city": "Stockholm",
                        "country": "SE",
                    },
                    "links": {
                        "careersite-job-url": "https://acme.teamtailor.com/jobs/2",
                    },
                },
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_teamtailor("acme", ["data scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Teamtailor"
        assert job["location"] == "Stockholm, SE"
        assert "Build models" in job["description"]
        assert "<p>" not in job["description"]
        assert job["source_url"] == "https://acme.teamtailor.com/jobs/1"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_on_http_error(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_teamtailor

        mock_get.return_value = MagicMock(status_code=500)
        assert scan_teamtailor("acme", ["data scientist"], []) == []

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_skips_items_without_attributes(self, mock_get_session):
        """Malformed items (no attributes dict) are silently skipped."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_teamtailor

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "data": [
                {"links": {"careersite-job-url": "https://x/1"}},  # no attributes
                {
                    "attributes": {
                        "title": "Data Scientist",
                        "body": "ML.",
                    },
                    "links": {"careersite-job-url": "https://acme.teamtailor.com/jobs/3"},
                },
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_teamtailor("acme", ["data scientist"], [])
        assert len(results) == 1
        assert results[0]["source_url"] == "https://acme.teamtailor.com/jobs/3"

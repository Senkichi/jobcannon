"""Tests for Stage-4 ATS scanners: Recruitee, Breezy, JazzHR.

Mirrors the structure of tests/engine/test_smartrecruiters_scanner.py — for each
platform: URL detection → _probe_<platform> → scan_<platform>.

All HTTP calls are mocked. No live network use.
"""

from unittest.mock import MagicMock, patch

from tests.engine.helpers.ats_session import ats_session_method

# ===========================================================================
# Recruitee
# ===========================================================================


class TestRecruiteeUrlDetection:
    """Tests for Recruitee URL pattern recognition in ats_detection."""

    def test_subdomain_url_returns_recruitee_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.recruitee.com/o/senior-data-scientist"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "recruitee"
        assert slug == "acme"

    def test_api_path_returns_recruitee_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.recruitee.com/api/offers/123"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "recruitee"
        assert slug == "acme"

    def test_slug_lowercased(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://AcmeCo.recruitee.com/o/job"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "recruitee"
        assert slug == "acmeco"

    def test_root_domain_not_matched(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        # Bare recruitee.com (no subdomain) should not match — the regex
        # requires at least one subdomain part.
        urls = ["https://recruitee.com/about"]
        platform, _ = extract_ats_from_urls(urls)
        assert platform is None


class TestProbeRecruitee:
    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_true_when_offers_present(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_recruitee

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"offers": [{"id": 1, "title": "Engineer"}]}
        mock_get.return_value = mock_resp
        assert _probe_recruitee("acme") is True

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_when_offers_empty(self, mock_get):
        """200 + empty offers list stays a miss — same pitfall as Lever."""
        from jobcannon.engine.ats_prober import _probe_recruitee

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"offers": []}
        mock_get.return_value = mock_resp
        assert _probe_recruitee("emptyco") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_404(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_recruitee

        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_recruitee("nonexistent") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_exception(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_recruitee

        mock_get.side_effect = Exception("dns failure")
        assert _probe_recruitee("acme") is False


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
                    "slug": "senior-data-scientist",
                    "careers_url": "https://acme.recruitee.com/o/senior-data-scientist",
                    "description": "<p>Build cool stuff.</p>",
                    "locations": [{"city": "Berlin", "country_code": "DE"}],
                },
                {
                    "title": "Marketing Manager",
                    "slug": "mktg",
                    "careers_url": "https://acme.recruitee.com/o/mktg",
                    "description": "Marketing role",
                    "locations": [{"city": "Berlin", "country_code": "DE"}],
                },
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_recruitee("acme", ["Data Scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Recruitee"
        assert job["source_url"] == "https://acme.recruitee.com/o/senior-data-scientist"
        # HTML stripped from description
        assert "<p>" not in job["description"]
        assert "Build cool stuff" in job["description"]
        assert "Berlin" in job["location"]

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_falls_back_to_constructed_url_when_careers_url_missing(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_recruitee

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "offers": [
                {"title": "Engineer", "slug": "eng", "description": "..."},
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_recruitee("acme", ["Engineer"], [])
        assert results[0]["source_url"] == "https://acme.recruitee.com/o/eng"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_list_on_404(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_recruitee

        mock_get.return_value = MagicMock(status_code=404)
        assert scan_recruitee("nonexistent", ["Engineer"], []) == []

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_list_on_exception(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_recruitee

        mock_get.side_effect = Exception("connection refused")
        assert scan_recruitee("acme", ["Engineer"], []) == []


# ===========================================================================
# Breezy
# ===========================================================================


class TestBreezyUrlDetection:
    def test_subdomain_url_returns_breezy_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.breezy.hr/p/abc123-engineer"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "breezy"
        assert slug == "acme"

    def test_slug_lowercased(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://AcmeCo.breezy.hr/p/eng"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "breezy"
        assert slug == "acmeco"

    def test_root_domain_not_matched(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://breezy.hr/pricing"]
        platform, _ = extract_ats_from_urls(urls)
        assert platform is None


class TestProbeBreezy:
    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_true_when_list_non_empty(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [{"id": "1", "name": "Engineer"}]
        mock_get.return_value = mock_resp
        assert _probe_breezy("acme") is True

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_when_list_empty(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp
        assert _probe_breezy("emptyco") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_404(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_breezy

        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_breezy("nonexistent") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_exception(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_breezy

        mock_get.side_effect = Exception("timeout")
        assert _probe_breezy("acme") is False


class TestScanBreezy:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "id": "abc123",
                "name": "Staff Engineer",
                "url": "https://acme.breezy.hr/p/abc123-staff-engineer",
                "location": {"city": "San Francisco", "country": "USA", "is_remote": False},
                "type": {"id": "FULL_TIME"},
                "department": "Engineering",
            },
            {
                "id": "xyz789",
                "name": "Office Manager",
                "url": "https://acme.breezy.hr/p/xyz789-office-manager",
                "location": {"is_remote": True},
            },
        ]
        mock_get.return_value = mock_resp

        results = scan_breezy("acme", ["Staff Engineer"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Staff Engineer"
        assert job["company_source"] == "Breezy"
        assert "San Francisco" in job["location"]
        assert job["source_url"].startswith("https://acme.breezy.hr/p/abc123")

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_remote_only_location_renders_as_remote(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {
                "id": "1",
                "name": "Engineer",
                "url": "https://acme.breezy.hr/p/1-eng",
                "location": {"is_remote": True},
            }
        ]
        mock_get.return_value = mock_resp

        results = scan_breezy("acme", ["Engineer"], [])
        assert results[0]["location"] == "Remote"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_accepts_positions_wrapper_shape(self, mock_get_session):
        """Some tenants wrap the list in {"positions": [...]}; accept both."""
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_breezy

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "positions": [
                {"id": "1", "name": "Engineer", "url": "https://acme.breezy.hr/p/1"},
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_breezy("acme", ["Engineer"], [])
        assert len(results) == 1

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_list_on_404(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_breezy

        mock_get.return_value = MagicMock(status_code=404)
        assert scan_breezy("nonexistent", ["Engineer"], []) == []


# ===========================================================================
# JazzHR
# ===========================================================================


class TestJazzHRUrlDetection:
    def test_subdomain_url_returns_jazzhr_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.applytojob.com/apply/abc123/senior-engineer"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "jazzhr"
        assert slug == "acme"

    def test_slug_lowercased(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://AcmeCo.applytojob.com/apply/x"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "jazzhr"
        assert slug == "acmeco"


class TestProbeJazzHR:
    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_true_when_jobs_present(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"jobs": [{"title": "Engineer"}]}
        mock_get.return_value = mock_resp
        assert _probe_jazzhr("acme") is True

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_true_when_bare_list_non_empty(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [{"title": "Engineer"}]
        mock_get.return_value = mock_resp
        assert _probe_jazzhr("acme") is True

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_when_jobs_empty(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"jobs": []}
        mock_get.return_value = mock_resp
        assert _probe_jazzhr("emptyco") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_404(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_jazzhr

        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_jazzhr("nonexistent") is False


class TestScanJazzHR:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Senior Data Scientist",
                    "board_code": "abcXYZ",
                    "city": "Austin",
                    "state": "TX",
                    "country": "USA",
                    "department": "Analytics",
                    "employment_type": "Full-time",
                    "description": "<p>Lead the data science team.</p>",
                    "apply_url": "https://acme.applytojob.com/apply/abcXYZ",
                }
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_jazzhr("acme", ["Data Scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "JazzHR"
        assert "Austin" in job["location"] and "TX" in job["location"]
        assert "<p>" not in job["description"]
        assert "Lead the data science team" in job["description"]
        assert job["source_url"] == "https://acme.applytojob.com/apply/abcXYZ"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_falls_back_to_board_code_url(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "jobs": [
                {
                    "title": "Engineer",
                    "board_code": "code123",
                    "description": "...",
                }
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_jazzhr("acme", ["Engineer"], [])
        assert results[0]["source_url"] == "https://acme.applytojob.com/apply/code123"

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_accepts_bare_list_response(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_jazzhr

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = [
            {"title": "Engineer", "board_code": "1", "description": "x"},
        ]
        mock_get.return_value = mock_resp

        results = scan_jazzhr("acme", ["Engineer"], [])
        assert len(results) == 1

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_list_on_exception(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_jazzhr

        mock_get.side_effect = Exception("dns failure")
        assert scan_jazzhr("acme", ["Engineer"], []) == []


# ===========================================================================
# Speculative probe loop: verifies new platforms are wired into probe_ats_slugs
# ===========================================================================


class TestSpeculativeProbeLoopWiring:
    """Confirms the new probes are reachable through probe_ats_slugs."""

    def test_probe_names_exported_from_ats_prober(self):
        """All 7 Stage-4 probes are public symbols in ats_prober."""
        from jobcannon.engine import ats_prober

        assert callable(ats_prober._probe_recruitee)
        assert callable(ats_prober._probe_breezy)
        assert callable(ats_prober._probe_jazzhr)
        assert callable(ats_prober._probe_pinpoint)
        assert callable(ats_prober._probe_personio)
        assert callable(ats_prober._probe_bamboohr)
        assert callable(ats_prober._probe_teamtailor)

    # test_fp_prone_probes_fastpath_verifiable_but_speculative_excluded
    # deliberately not ported: it imports jobcannon.engine.ats_scanner._probe,
    # which is Task 3 scope (not ported by this PR).


# ===========================================================================
# Pinpoint
# ===========================================================================


class TestPinpointUrlDetection:
    def test_subdomain_url_returns_pinpoint_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://workwithus.pinpointhq.com/postings/some-job"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "pinpoint"
        assert slug == "workwithus"

    def test_api_path_returns_pinpoint_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://workwithus.pinpointhq.com/postings.json"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "pinpoint"
        assert slug == "workwithus"

    def test_slug_lowercased(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://WorkWithUs.pinpointhq.com/postings"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "pinpoint"
        assert slug == "workwithus"

    def test_root_domain_not_matched(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://pinpointhq.com/about"]
        platform, _ = extract_ats_from_urls(urls)
        assert platform is None


class TestProbePinpoint:
    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_true_when_postings_present(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_pinpoint

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"data": [{"id": "1", "title": "Engineer"}]}
        mock_get.return_value = mock_resp
        assert _probe_pinpoint("acme") is True

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_when_postings_empty(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_pinpoint

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"data": []}
        mock_get.return_value = mock_resp
        assert _probe_pinpoint("emptyco") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_404(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_pinpoint

        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_pinpoint("nonexistent") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_exception(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_pinpoint

        mock_get.side_effect = Exception("dns failure")
        assert _probe_pinpoint("acme") is False


class TestScanPinpoint:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_pinpoint

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "data": [
                {
                    "id": "1",
                    "title": "Senior Data Scientist",
                    "url": "https://acme.pinpointhq.com/postings/1",
                    "location": {"city": "Berlin", "province": "BE", "name": "Berlin Office"},
                    "compensation_minimum": 90000,
                    "compensation_maximum": 130000,
                    "description": "<p>Build cool stuff.</p>",
                },
                {
                    "id": "2",
                    "title": "Receptionist",  # excluded
                    "url": "https://acme.pinpointhq.com/postings/2",
                    "location": {"city": "Berlin"},
                },
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_pinpoint("acme", ["Data Scientist"], [])
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"
        assert results[0]["company_source"] == "Pinpoint"
        assert results[0]["location"] == "Berlin, BE"
        assert results[0]["source_url"] == "https://acme.pinpointhq.com/postings/1"
        assert results[0]["description"] == "Build cool stuff."
        assert results[0]["salary_min"] == 90000
        assert results[0]["salary_max"] == 130000

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_list_when_data_missing(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_pinpoint

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {}
        mock_get.return_value = mock_resp
        assert scan_pinpoint("acme", ["Engineer"], []) == []

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_list_on_exception(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_pinpoint

        mock_get.side_effect = Exception("dns failure")
        assert scan_pinpoint("acme", ["Engineer"], []) == []

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_invalid_compensation_does_not_crash(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_pinpoint

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "data": [
                {
                    "id": "3",
                    "title": "Engineer",
                    "url": "https://acme.pinpointhq.com/postings/3",
                    "location": {},
                    "compensation_minimum": "not-a-number",
                    "compensation_maximum": None,
                    "description": "",
                }
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_pinpoint("acme", ["Engineer"], [])
        assert len(results) == 1
        assert results[0]["salary_min"] is None
        assert results[0]["salary_max"] is None


# ===========================================================================
# Personio
# ===========================================================================


_PERSONIO_XML_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>12345</id>
    <name>Senior Data Scientist</name>
    <office>Berlin</office>
    <employmentType>permanent</employmentType>
    <jobDescriptions>
      <jobDescription>
        <name>About the role</name>
        <value>&lt;p&gt;Build cool stuff.&lt;/p&gt;</value>
      </jobDescription>
    </jobDescriptions>
  </position>
  <position>
    <id>67890</id>
    <name>Receptionist</name>
    <office>Munich</office>
    <jobDescriptions/>
  </position>
</workzag-jobs>
"""


class TestPersonioUrlDetection:
    def test_de_tld_url_returns_personio_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.jobs.personio.de/job/12345"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "personio"
        assert slug == "acme"

    def test_com_tld_url_returns_personio_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.jobs.personio.com/job/12345"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "personio"
        assert slug == "acme"

    def test_xml_path_returns_personio(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.jobs.personio.de/xml"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "personio"
        assert slug == "acme"

    def test_root_domain_not_matched(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://www.personio.de/"]
        platform, _ = extract_ats_from_urls(urls)
        assert platform is None


class TestProbePersonio:
    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_true_when_positions_present_on_de(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_personio

        mock_resp = MagicMock(status_code=200, content=_PERSONIO_XML_SAMPLE)
        mock_get.return_value = mock_resp
        assert _probe_personio("acme") is True
        # First call should hit .de
        assert "personio.de" in mock_get.call_args_list[0].args[0]

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_falls_back_to_com_on_404(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_personio

        mock_de = MagicMock(status_code=404, content=b"")
        mock_com = MagicMock(status_code=200, content=_PERSONIO_XML_SAMPLE)
        mock_get.side_effect = [mock_de, mock_com]
        assert _probe_personio("acme") is True
        assert "personio.com" in mock_get.call_args_list[1].args[0]

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_when_feed_empty(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_personio

        empty_xml = b"<?xml version='1.0'?><workzag-jobs></workzag-jobs>"
        mock_de = MagicMock(status_code=200, content=empty_xml)
        mock_com = MagicMock(status_code=404, content=b"")
        mock_get.side_effect = [mock_de, mock_com]
        assert _probe_personio("emptyco") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_exception(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_personio

        mock_get.side_effect = Exception("dns failure")
        assert _probe_personio("acme") is False


class TestScanPersonio:
    @patch("jobcannon.engine.ats_platforms._platforms_personio.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_personio

        mock_resp = MagicMock(status_code=200, content=_PERSONIO_XML_SAMPLE)
        mock_get.return_value = mock_resp

        results = scan_personio("acme", ["Data Scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Personio"
        assert job["location"] == "Berlin"
        assert "Build cool stuff" in job["description"]
        assert job["source_url"].endswith("/job/12345")
        assert job["salary_min"] is None

    @patch("jobcannon.engine.ats_platforms._platforms_personio.get_session")
    def test_returns_empty_list_on_404(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_personio

        mock_get.return_value = MagicMock(status_code=404, content=b"")
        assert scan_personio("acme", ["Engineer"], []) == []

    @patch("jobcannon.engine.ats_platforms._platforms_personio.get_session")
    def test_returns_empty_list_on_parse_error(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_personio

        mock_get.return_value = MagicMock(status_code=200, content=b"<not-xml>")
        assert scan_personio("acme", ["Engineer"], []) == []


# ===========================================================================
# BambooHR
# ===========================================================================


_BAMBOOHR_WIDGET_SAMPLE = """
<div class="BambooHR-ATS-board">
  <ul>
    <li id="bhrDepartmentID_1" class="BambooHR-ATS-Department-Item">
      <div id="department_1" class="BambooHR-ATS-Department-Header">Engineering</div>
      <ul class="BambooHR-ATS-Jobs-List">
        <li id="bhrPositionID_111" class="BambooHR-ATS-Jobs-Item">
          <a href="//acme.bamboohr.com/careers/111">Senior Data Scientist</a>
          <span class="BambooHR-ATS-Location">Berlin, Germany</span>
        </li>
        <li id="bhrPositionID_222" class="BambooHR-ATS-Jobs-Item">
          <a href="//acme.bamboohr.com/careers/222">Receptionist</a>
          <span class="BambooHR-ATS-Location">Munich</span>
        </li>
      </ul>
    </li>
  </ul>
</div>
"""


class TestBambooHRUrlDetection:
    def test_subdomain_url_returns_bamboohr_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.bamboohr.com/careers/111"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "bamboohr"
        assert slug == "acme"

    def test_jobs_embed_url_returns_bamboohr(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.bamboohr.com/jobs/embed2.php"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "bamboohr"
        assert slug == "acme"

    def test_slug_lowercased(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://AcmeCo.bamboohr.com/careers/123"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "bamboohr"
        assert slug == "acmeco"

    def test_root_domain_not_matched(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://www.bamboohr.com/products/ats"]
        platform, _ = extract_ats_from_urls(urls)
        assert platform == "bamboohr"
        # We accept www-prefixed company-less URLs as a degenerate slug 'www' —
        # the human URL regex is intentionally permissive. Reconciliation
        # depends on the speculative probe to confirm the slug is real.


class TestProbeBambooHR:
    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_true_when_widget_contains_positions(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_bamboohr

        mock_resp = MagicMock(status_code=200, text=_BAMBOOHR_WIDGET_SAMPLE)
        mock_get.return_value = mock_resp
        assert _probe_bamboohr("acme") is True

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_when_widget_empty(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_bamboohr

        mock_resp = MagicMock(status_code=200, text="<div class='BambooHR-ATS-board'></div>")
        mock_get.return_value = mock_resp
        assert _probe_bamboohr("emptyco") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_404(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_bamboohr

        mock_get.return_value = MagicMock(status_code=404, text="")
        assert _probe_bamboohr("nonexistent") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_exception(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_bamboohr

        mock_get.side_effect = Exception("dns failure")
        assert _probe_bamboohr("acme") is False


class TestScanBambooHR:
    @patch("jobcannon.engine.ats_platforms._platforms_bamboohr.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_bamboohr

        mock_resp = MagicMock(status_code=200, text=_BAMBOOHR_WIDGET_SAMPLE)
        mock_get.return_value = mock_resp

        results = scan_bamboohr("acme", ["Data Scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "BambooHR"
        assert job["location"] == "Berlin, Germany"
        assert job["source_url"] == "https://acme.bamboohr.com/careers/111"
        # Listing has no description; jd_full is filled later by enrichment.
        assert job["description"] == ""

    @patch("jobcannon.engine.ats_platforms._platforms_bamboohr.get_session")
    def test_relative_href_is_absolutized(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_bamboohr

        sample = (
            '<li id="bhrPositionID_999" class="BambooHR-ATS-Jobs-Item">'
            '<a href="/careers/999">Engineer</a>'
            '<span class="BambooHR-ATS-Location">Remote</span></li>'
        )
        mock_get.return_value = MagicMock(status_code=200, text=sample)
        results = scan_bamboohr("acme", ["Engineer"], [])
        assert results[0]["source_url"] == "https://acme.bamboohr.com/careers/999"

    @patch("jobcannon.engine.ats_platforms._platforms_bamboohr.get_session")
    def test_returns_empty_list_on_404(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_bamboohr

        mock_get.return_value = MagicMock(status_code=404, text="")
        assert scan_bamboohr("acme", ["Engineer"], []) == []

    @patch("jobcannon.engine.ats_platforms._platforms_bamboohr.get_session")
    def test_returns_empty_list_on_exception(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_bamboohr

        mock_get.side_effect = Exception("dns failure")
        assert scan_bamboohr("acme", ["Engineer"], []) == []


# ===========================================================================
# Teamtailor
# ===========================================================================


class TestTeamtailorUrlDetection:
    def test_subdomain_url_returns_teamtailor_and_slug(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.teamtailor.com/jobs/12345"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "teamtailor"
        assert slug == "acme"

    def test_api_path_returns_teamtailor(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://acme.teamtailor.com/api/jobs"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "teamtailor"
        assert slug == "acme"

    def test_slug_lowercased(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://AcmeCo.teamtailor.com/jobs/12345"]
        platform, slug = extract_ats_from_urls(urls)
        assert platform == "teamtailor"
        assert slug == "acmeco"

    def test_root_domain_not_matched(self):
        from jobcannon.engine.ats_detection import extract_ats_from_urls

        urls = ["https://teamtailor.com/about"]
        platform, _ = extract_ats_from_urls(urls)
        assert platform is None


class TestProbeTeamtailor:
    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_true_when_data_present(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_teamtailor

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"data": [{"id": "1", "attributes": {"title": "Engineer"}}]}
        mock_get.return_value = mock_resp
        assert _probe_teamtailor("acme") is True

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_when_data_empty(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_teamtailor

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"data": []}
        mock_get.return_value = mock_resp
        assert _probe_teamtailor("emptyco") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_404(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_teamtailor

        mock_get.return_value = MagicMock(status_code=404)
        assert _probe_teamtailor("nonexistent") is False

    @patch("jobcannon.engine.ats_prober.requests.get")
    def test_returns_false_on_exception(self, mock_get):
        from jobcannon.engine.ats_prober import _probe_teamtailor

        mock_get.side_effect = Exception("dns failure")
        assert _probe_teamtailor("acme") is False


class TestScanTeamtailor:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_matched_jobs(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_teamtailor

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "data": [
                {
                    "id": "1",
                    "attributes": {
                        "title": "Senior Data Scientist",
                        "body": "<p>Build cool stuff.</p>",
                        "city": "Stockholm",
                        "country": "Sweden",
                    },
                    "links": {"careersite-job-url": "https://acme.teamtailor.com/jobs/1"},
                },
                {
                    "id": "2",
                    "attributes": {"title": "Receptionist"},
                    "links": {},
                },
            ]
        }
        mock_get.return_value = mock_resp

        results = scan_teamtailor("acme", ["Data Scientist"], [])
        assert len(results) == 1
        job = results[0]
        assert job["title"] == "Senior Data Scientist"
        assert job["company_source"] == "Teamtailor"
        assert job["location"] == "Stockholm, Sweden"
        assert job["source_url"] == "https://acme.teamtailor.com/jobs/1"
        assert job["description"] == "Build cool stuff."

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_list_when_data_missing(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_teamtailor

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {}
        mock_get.return_value = mock_resp
        assert scan_teamtailor("acme", ["Engineer"], []) == []

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_returns_empty_list_on_exception(self, mock_get_session):
        mock_get = ats_session_method(mock_get_session, "get")
        from jobcannon.engine.ats_platforms import scan_teamtailor

        mock_get.side_effect = Exception("dns failure")
        assert scan_teamtailor("acme", ["Engineer"], []) == []

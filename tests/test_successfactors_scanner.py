"""Tests for SuccessFactors ATS platform scanner.

Covers:
- URL detection: extract_ats_from_url_best returns the expected
  (platform, slug, specificity) for SuccessFactors URLs.
- Probe: _probe_successfactors returns True on a 200 response with
  job-bearing XML, False on empty / non-200 / exception.
- Scanner: SCANNER's fetch_postings returns expected shape from
  the real fixture; posting_to_job builds the canonical job dict.
- Facet resolution: location/department/employment_type resolved by
  label text, not element index.
- Empty feed handling: <Job-Listing></Job-Listing> returns [].
- 404/410 handling: BoardGoneError raised.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from jobcannon.engine.ats_detection import (
    ATS_EXTRACTOR_VERSION,
    extract_ats_from_url_best,
)
from jobcannon.engine.ats_platforms._platforms_successfactors import (
    SCANNER,
    _parse_posted_date,
    _posting_to_job,
    _resolve_facet,
)

# ---------------------------------------------------------------------------
# Extractor version bump
# ---------------------------------------------------------------------------


def test_extractor_version_bumped_for_successfactors():
    """SuccessFactors added URL pattern -- the version string must be bumped."""
    assert ATS_EXTRACTOR_VERSION == "m050-v1"  # Updated to m050-v1 after PR-4 registry migration


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_successfactors_eu_url_returns_successfactors_and_slug(self):
        url = "https://career2.successfactors.eu/career?company=SwissRe&career_ns=job_listing_summary&resultType=XML"
        platform, slug, _ = extract_ats_from_url_best(url) or ("", "", 0)
        assert platform == "successfactors"
        assert slug == "career2.successfactors.eu|SwissRe"

    def test_successfactors_com_url_returns_successfactors_and_slug(self):
        url = "https://career4.successfactors.com/career?company=AcmeCorp&career_ns=job_listing_summary&resultType=XML"
        platform, slug, _ = extract_ats_from_url_best(url) or ("", "", 0)
        assert platform == "successfactors"
        assert slug == "career4.successfactors.com|AcmeCorp"

    def test_successfactors_career1_url(self):
        url = "https://career1.successfactors.com/career?company=TestCo"
        platform, slug, _ = extract_ats_from_url_best(url) or ("", "", 0)
        assert platform == "successfactors"
        assert slug == "career1.successfactors.com|TestCo"

    def test_successfactors_sap_demo_url_returns_none(self):
        """Alternative SAP domains don't expose the public feed format."""
        url = "https://sapsfdemojobs.com/career?company=Demo"
        assert extract_ats_from_url_best(url) is None

    def test_successfactors_sap_internal_url_returns_none(self):
        url = "https://jobs.hr.cloud.sap.com/career?company=SAP"
        assert extract_ats_from_url_best(url) is None


# ---------------------------------------------------------------------------
# Fixture-based parsing
# ---------------------------------------------------------------------------


class TestFixtureParsing:
    def test_parse_real_fixture_extracts_at_least_two_jobs(self):
        """The real Swiss Re fixture has 2 jobs (truncated from 309)."""
        fixture_path = (
            Path(__file__).parent / "fixtures" / "successfactors_job_listing_summary.xml"
        )
        content = fixture_path.read_bytes()

        import defusedxml.ElementTree as ET

        root = ET.fromstring(content)
        jobs = list(root.iter("Job"))
        assert len(jobs) >= 2

    def test_first_job_title_and_req_id_extracted(self):
        """First job in fixture: (Senior) Aktuar/in & Client Manager/in L&H, ReqId 137248."""
        fixture_path = (
            Path(__file__).parent / "fixtures" / "successfactors_job_listing_summary.xml"
        )
        content = fixture_path.read_bytes()

        import defusedxml.ElementTree as ET

        root = ET.fromstring(content)
        first_job = next(root.iter("Job"))

        title_elem = first_job.find("JobTitle")
        title = (title_elem.text if title_elem is not None else "").strip()
        assert "(Senior) Aktuar/in" in title

        req_id_elem = first_job.find("ReqId")
        req_id = (req_id_elem.text if req_id_elem is not None else "").strip()
        assert req_id == "137248"

    def test_posted_date_converted_to_iso(self):
        """Posted-Date 04/03/2026 → 2026-04-03."""
        assert _parse_posted_date("04/03/2026") == "2026-04-03"
        assert _parse_posted_date("12/31/2025") == "2025-12-31"
        assert _parse_posted_date("invalid") is None
        assert _parse_posted_date("") is None

    def test_facet_resolution_by_label(self):
        """Facets resolved by label text, not element index."""
        fixture_path = (
            Path(__file__).parent / "fixtures" / "successfactors_job_listing_summary.xml"
        )
        content = fixture_path.read_bytes()

        import defusedxml.ElementTree as ET

        root = ET.fromstring(content)
        first_job = next(root.iter("Job"))

        # Posting Location facet (empty in fixture, but label exists)
        location = _resolve_facet(first_job, "Posting Location")
        assert location is None  # Empty value returns None

        # Country facet fallback
        country = _resolve_facet(first_job, "Country")
        assert country == "Germany"

        # Job Family / Functional Area
        department = _resolve_facet(first_job, "Job Family / Functional Area")
        assert "Client Management" in department

        # Type of Employment
        employment_type = _resolve_facet(first_job, "Type of Employment")
        assert employment_type == "Regular Employment"


# ---------------------------------------------------------------------------
# Scanner shape
# ---------------------------------------------------------------------------


class TestSuccessFactorsScanner:
    def test_fetch_postings_from_fixture(self):
        """Parse the real fixture and extract jobs."""
        fixture_path = (
            Path(__file__).parent / "fixtures" / "successfactors_job_listing_summary.xml"
        )
        content = fixture_path.read_bytes()

        with patch(
            "jobcannon.engine.ats_platforms._platforms_successfactors._fetch_xml",
            return_value=content,
        ):
            postings = SCANNER.fetch_postings("career2.successfactors.eu|SwissRe")

        assert len(postings) >= 2
        first = postings[0]
        assert "(Senior) Aktuar/in" in first["title"]
        assert "& Client Manager/in L&H" in first["title"]
        assert "&amp;" not in first["title"]
        assert first["req_id"] == "137248"
        assert first["posted_date"] == "2026-04-03"
        assert first["location"] == "Germany"  # Country fallback
        assert "Client Management" in first["department"]
        assert first["employment_type"] == "Regular Employment"

    def test_posting_to_job_builds_canonical_dict(self):
        posting = {
            "title": "Test Engineer",
            "__description_raw": "<p>Build things</p>",
            "req_id": "12345",
            "posted_date": "2026-01-15",
            "location": "Remote",
            "department": "Engineering",
            "employment_type": "Full-time",
        }

        job = _posting_to_job(posting, "career2.successfactors.eu|TestCo")

        assert job["title"] == "Test Engineer"
        assert job["company_source"] == "SuccessFactors"
        assert job["jd_full"] == "<p>Build things</p>"  # Raw HTML kept
        assert job["source_id"] == "12345"
        assert job["posted_date"] == "2026-01-15"
        assert job["location"] == "Remote"
        assert job["department"] == "Engineering"
        assert job["employment_type"] == "Full-time"
        assert job["source_url"] == "https://career2.successfactors.eu/career?company=TestCo"
        assert job["salary_min"] is None
        assert job["salary_max"] is None
        assert job["comp_json"] is None

    def test_empty_job_listing_returns_empty_list(self):
        """Empty <Job-Listing></Job-Listing> returns []."""
        empty_xml = b'<?xml version="1.0" encoding="UTF-8"?><Job-Listing></Job-Listing>'

        with patch(
            "jobcannon.engine.ats_platforms._platforms_successfactors._fetch_xml",
            return_value=empty_xml,
        ):
            postings = SCANNER.fetch_postings("career2.successfactors.eu|SwissRe")

        assert postings == []

    def test_invalid_slug_format_returns_empty_list(self):
        """Slug without | separator returns []."""
        with patch(
            "jobcannon.engine.ats_platforms._platforms_successfactors._fetch_xml",
            return_value=None,
        ):
            postings = SCANNER.fetch_postings("invalid-slug")
        assert postings == []

    def test_404_raises_board_gone(self):
        """404/410 from the feed URL raises BoardGoneError (enables stale-hit demotion)."""
        from jobcannon.engine.ats_platforms._registry import BoardGoneError
        from tests.helpers.ats_session import ats_session_method

        fake = Mock(status_code=404, content=b"")
        with patch(
            "jobcannon.engine.ats_platforms._platforms_successfactors.get_session"
        ) as mock_get_session:
            ats_session_method(mock_get_session, "get").return_value = fake
            with pytest.raises(BoardGoneError):
                SCANNER.fetch_postings("career2.successfactors.eu|NonExistent")


# ---------------------------------------------------------------------------
# Probe behavior
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class TestProbeSuccessFactors:
    def test_probe_hit_with_job_listing(self):
        from jobcannon.engine.ats_prober import _probe_successfactors

        xml_with_jobs = '<?xml version="1.0"?><Job-Listing><Job><JobTitle>Engineer</JobTitle></Job></Job-Listing>'
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, xml_with_jobs),
        ):
            assert _probe_successfactors("career2.successfactors.eu|SwissRe") is True

    def test_probe_miss_on_empty_job_listing(self):
        from jobcannon.engine.ats_prober import _probe_successfactors

        empty_xml = '<?xml version="1.0"?><Job-Listing></Job-Listing>'
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, empty_xml),
        ):
            assert _probe_successfactors("career2.successfactors.eu|SwissRe") is False

    def test_probe_miss_on_404(self):
        from jobcannon.engine.ats_prober import _probe_successfactors

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(404),
        ):
            assert _probe_successfactors("career2.successfactors.eu|NonExistent") is False

    def test_probe_miss_on_invalid_slug_format(self):
        from jobcannon.engine.ats_prober import _probe_successfactors

        assert _probe_successfactors("invalid-slug-no-pipe") is False


# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


class TestRegistryCompleteness:
    def test_successfactors_in_scanners_by_name(self):
        from jobcannon.engine.ats_platforms import SCANNERS_BY_NAME

        assert "successfactors" in SCANNERS_BY_NAME

    def test_successfactors_in_registry(self):
        from jobcannon.engine.ats_registry import PLATFORMS

        assert "successfactors" in PLATFORMS
        spec = PLATFORMS["successfactors"]
        assert spec.probe_attr == "_probe_successfactors"
        assert spec.url_fastpath is True
        assert spec.reconcilable is True

    def test_successfactors_scanner_callable(self):
        from jobcannon.engine.ats_platforms import scan_successfactors

        # "invalid|slug" splits into a real (host, company_id) pair, so
        # _fetch_xml would otherwise issue a real HTTP GET to
        # https://invalid/career?...; mock it at the same seam
        # test_invalid_slug_format_returns_empty_list uses.
        with patch(
            "jobcannon.engine.ats_platforms._platforms_successfactors._fetch_xml",
            return_value=None,
        ):
            # Should not raise; returns [] on empty/invalid slug
            result = scan_successfactors("invalid|slug", [], [])
        assert result == []

"""Tests for round-6 ATS platform additions (audit B2-roadmap):
Workable, Jobvite, Paylocity, Rippling, IBM.

Covers:
- URL detection: extract_ats_from_url_best returns the expected
  (platform, slug, specificity) for each platform's canonical URL.
- Probe: each _probe_X returns True on a 200 response with non-empty
  jobs, False on empty / non-200 / exception.
- Scanner: each SCANNER's fetch_postings returns expected shape from
  a stub HTTP response; posting_to_job builds the canonical job dict.
- NON_SCANNABLE_PLATFORMS invariants (frozenset, jobvite membership,
  subset-of-registered-scanners).

Jobvite is intentionally a stub (no public unauthenticated JSON API);
its scanner returns []. Tests reflect that contract.

The private original's Dispatcher/Reconcile-path coverage
(``_PLATFORM_SCANNERS``, ``_verify_fastpath_live``, ``ats_identity_reconcile.
_verify_live``) is Task 3 scope and not ported here — see the module body's
inline notes for the exact tests left behind.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jobcannon.engine.ats_detection import (
    ATS_EXTRACTOR_VERSION,
    extract_ats_from_url_best,
)
from tests.engine.helpers.ats_session import ats_session_method

# ---------------------------------------------------------------------------
# Extractor version bump
# ---------------------------------------------------------------------------


def test_extractor_version_bumped_for_round6_patterns():
    """Round-6 added 4 URL patterns -- the version string must be bumped.

    Tracks the current extractor version (bumped to m049-v5 when the iCIMS URL
    pattern was added in PR-A2, m049-v6 for Oracle Recruiting Cloud, m049-v7 for
    UKG Pro Recruiting / UltiPro, m049-v8 for SuccessFactors, m049-v9 for Phenom,
    m049-v10 for Phenom www.* exclusion, m049-v11 for ADP Workforce Now); every
    material regex change bumps it.

    Updated to m050-v1 after PR-4: URL patterns migrated to ats_registry.
    """
    assert ATS_EXTRACTOR_VERSION == "m050-v1"


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------


class TestUrlDetection:
    def test_workable_url_returns_workable_and_slug(self):
        url = "https://apply.workable.com/datadog"
        assert extract_ats_from_url_best(url) == ("workable", "datadog", 5)

    def test_workable_job_detail_url_returns_workable(self):
        url = "https://apply.workable.com/canonical/j/A1B2C3D4"
        platform, slug, _ = extract_ats_from_url_best(url) or ("", "", 0)
        assert platform == "workable"
        assert slug == "canonical"

    def test_jobvite_url_returns_jobvite_and_slug(self):
        url = "https://jobs.jobvite.com/victaulic/jobs/alljobs"
        assert extract_ats_from_url_best(url) == ("jobvite", "victaulic", 5)

    def test_jobvite_root_url_returns_jobvite(self):
        url = "https://jobs.jobvite.com/the-institutes"
        assert extract_ats_from_url_best(url) == ("jobvite", "the-institutes", 5)

    def test_paylocity_guid_url_returns_paylocity_and_guid(self):
        url = (
            "https://recruiting.paylocity.com/recruiting/jobs/All/"
            "b181f77f-0432-453f-b229-869d786bb46c/Available-Positions"
        )
        platform, slug, _ = extract_ats_from_url_best(url) or ("", "", 0)
        assert platform == "paylocity"
        assert slug == "b181f77f-0432-453f-b229-869d786bb46c"

    def test_paylocity_subdomain_with_titlecase_path(self):
        """Audit observed `2000recruiting.paylocity.com/Recruiting/Jobs/All/{guid}`."""
        url = (
            "https://2000recruiting.paylocity.com/Recruiting/Jobs/All/"
            "e2bcef5a-b6e5-4c5a-8fdd-c4da179dd98c"
        )
        platform, slug, _ = extract_ats_from_url_best(url) or ("", "", 0)
        assert platform == "paylocity"
        assert slug == "e2bcef5a-b6e5-4c5a-8fdd-c4da179dd98c"

    def test_rippling_url_returns_rippling_and_slug(self):
        url = "https://ats.rippling.com/joinroot/jobs"
        assert extract_ats_from_url_best(url) == ("rippling", "joinroot", 5)

    def test_rippling_root_url_returns_rippling(self):
        url = "https://ats.rippling.com/just-appraised-jobs"
        assert extract_ats_from_url_best(url) == ("rippling", "just-appraised-jobs", 5)

    def test_unknown_workable_lookalike_returns_none(self):
        """Don't match `workable.com` directly; only the apply.workable.com tenant URL."""
        assert extract_ats_from_url_best("https://www.workable.com/careers") is None

    def test_icims_careers_host_returns_icims_and_tenant(self):
        url = "https://careers-acme.icims.com/jobs/search?ss=1"
        assert extract_ats_from_url_best(url) == ("icims", "acme", 5)

    def test_icims_jobs_host_returns_icims_and_tenant(self):
        url = "https://jobs-acme.icims.com/jobs/12345/data-scientist/job"
        assert extract_ats_from_url_best(url) == ("icims", "acme", 5)

    def test_icims_tenant_lowercased_with_hyphen(self):
        url = "https://careers-Big-Co.icims.com/jobs/search"
        assert extract_ats_from_url_best(url) == ("icims", "big-co", 5)

    def test_icims_vendor_host_returns_none(self):
        """The vendor's own www.icims.com marketing host is not a tenant board."""
        assert extract_ats_from_url_best("https://www.icims.com/products") is None

    def test_icims_bare_subdomain_without_prefix_returns_none(self):
        """Require the careers-/jobs- prefix; a bare {sub}.icims.com isn't matched."""
        assert extract_ats_from_url_best("https://acme.icims.com/jobs/search") is None


# ---------------------------------------------------------------------------
# Probe behavior
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, body: dict | list | None = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class TestProbeWorkable:
    def test_workable_hit_with_jobs(self):
        from jobcannon.engine.ats_prober import _probe_workable

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, {"name": "Acme", "jobs": [{"title": "Engineer"}]}),
        ):
            assert _probe_workable("acme") is True

    def test_workable_empty_jobs_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_workable

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, {"name": "Acme", "jobs": []}),
        ):
            assert _probe_workable("acme") is False

    def test_workable_404_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_workable

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(404),
        ):
            assert _probe_workable("acme") is False


class TestProbeJobvite:
    def test_jobvite_200_is_hit(self):
        from jobcannon.engine.ats_prober import _probe_jobvite

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200),
        ):
            assert _probe_jobvite("victaulic") is True

    def test_jobvite_404_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_jobvite

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(404),
        ):
            assert _probe_jobvite("nope") is False


class TestProbePaylocity:
    def test_paylocity_hit_with_jobs(self):
        from jobcannon.engine.ats_prober import _probe_paylocity

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(
                200,
                {"organization": "Acme", "jobs": [{"jobId": 1, "title": "Engineer"}]},
            ),
        ):
            assert _probe_paylocity("00000000-0000-0000-0000-000000000000") is True

    def test_paylocity_empty_jobs_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_paylocity

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, {"organization": "Acme", "jobs": []}),
        ):
            assert _probe_paylocity("00000000-0000-0000-0000-000000000000") is False


class TestProbeRippling:
    def test_rippling_hit_with_items(self):
        from jobcannon.engine.ats_prober import _probe_rippling

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(
                200,
                {"items": [{"id": "x", "name": "Engineer"}], "page": 1, "pageSize": 1},
            ),
        ):
            assert _probe_rippling("joinroot") is True

    def test_rippling_empty_items_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_rippling

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, {"items": [], "page": 1, "pageSize": 1}),
        ):
            assert _probe_rippling("joinroot") is False


class TestProbeIbm:
    def test_ibm_200_with_hits_is_hit(self):
        from jobcannon.engine.ats_prober import _probe_ibm

        response_body = {
            "hits": {"hits": [{"_source": {"field_text_01": 123, "title": "Engineer"}}]}
        }
        with patch(
            "jobcannon.engine.ats_prober.requests.post",
            return_value=_FakeResp(200, response_body),
        ):
            assert _probe_ibm("ibm") is True

    def test_ibm_200_with_empty_hits_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_ibm

        response_body = {"hits": {"hits": []}}
        with patch(
            "jobcannon.engine.ats_prober.requests.post",
            return_value=_FakeResp(200, response_body),
        ):
            assert _probe_ibm("ibm") is False

    def test_ibm_404_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_ibm

        with patch(
            "jobcannon.engine.ats_prober.requests.post",
            return_value=_FakeResp(404),
        ):
            assert _probe_ibm("ibm") is False


# ---------------------------------------------------------------------------
# Scanner shape
# ---------------------------------------------------------------------------


class TestWorkableScanner:
    def test_fetch_postings_returns_jobs_array(self):
        from jobcannon.engine.ats_platforms._platforms_workable import (
            _fetch_postings,
        )

        sample = {
            "name": "Acme",
            "jobs": [
                {
                    "title": "Senior Engineer",
                    "location": "Remote",
                    "description": "<p>Build things</p>",
                    "shortcode": "ABC123",
                },
                "not-a-dict",  # filtered out defensively
            ],
        }
        with patch("jobcannon.engine.ats_platforms._registry.get_session") as mock_get_session:
            ats_session_method(mock_get_session, "get").return_value = _FakeResp(200, sample)
            postings = _fetch_postings("acme")
        assert len(postings) == 1
        assert postings[0]["title"] == "Senior Engineer"

    def test_posting_to_job_strips_html_and_falls_back_to_shortcode_url(self):
        from jobcannon.engine.ats_platforms._platforms_workable import (
            _posting_to_job,
        )

        posting = {
            "title": "Engineer",
            "location": "Remote",
            "description": "<p>Build <b>things</b></p>",
            "shortcode": "ABC123",
        }
        job = _posting_to_job(posting, "acme")
        assert job["title"] == "Engineer"
        assert job["company_source"] == "Workable"
        assert "<" not in job["description"]
        assert "build" in job["description"].lower()


class TestPaylocityScanner:
    def test_fetch_postings_extracts_jobs(self):
        from jobcannon.engine.ats_platforms._platforms_paylocity import (
            _fetch_postings,
        )

        sample = {
            "organization": "Acme",
            "jobCount": 1,
            "jobs": [
                {"jobId": 42, "title": "Engineer", "location": "NYC"},
            ],
        }
        with patch("jobcannon.engine.ats_platforms._registry.get_session") as mock_get_session:
            ats_session_method(mock_get_session, "get").return_value = _FakeResp(200, sample)
            postings = _fetch_postings("00000000-0000-0000-0000-000000000000")
        assert len(postings) == 1

    def test_posting_to_job_stitches_multi_section_description(self):
        from jobcannon.engine.ats_platforms._platforms_paylocity import (
            _posting_to_job,
        )

        posting = {
            "title": "Engineer",
            "location": "NYC",
            "summary": "Brief role overview",
            "keyResponsibilities": ["Do thing A", "Do thing B"],
            "requirements": ["Skill X"],
            "salaryRange": "$100k-$120k",
            "applyUrl": "https://recruiting.paylocity.com/recruiting/jobs/Apply/42",
        }
        job = _posting_to_job(posting, "guid")
        assert job["title"] == "Engineer"
        assert job["company_source"] == "Paylocity"
        assert "Brief role overview" in job["description"]
        assert "Key Responsibilities:" in job["description"]
        assert "- Do thing A" in job["description"]
        assert "Requirements:" in job["description"]
        assert "Salary: $100k-$120k" in job["description"]
        assert job["source_url"].endswith("/Apply/42")


class TestRipplingScanner:
    def test_fetch_postings_paginates(self):
        """Walks pages until totalPages reached. Two-page sample collapses to
        a flat list of items from both pages."""
        from jobcannon.engine.ats_platforms._platforms_rippling import (
            _fetch_postings,
        )

        page1 = {
            "items": [{"id": "a", "name": "Job A"}],
            "page": 1,
            "pageSize": 1,
            "totalItems": 2,
            "totalPages": 2,
        }
        page2 = {
            "items": [{"id": "b", "name": "Job B"}],
            "page": 2,
            "pageSize": 1,
            "totalItems": 2,
            "totalPages": 2,
        }
        responses = [_FakeResp(200, page1), _FakeResp(200, page2)]
        with patch("jobcannon.engine.ats_platforms._registry.get_session") as mock_get_session:
            ats_session_method(mock_get_session, "get").side_effect = responses
            postings = _fetch_postings("joinroot")
        assert [p["id"] for p in postings] == ["a", "b"]

    def test_posting_to_job_builds_canonical_dict(self):
        from jobcannon.engine.ats_platforms._platforms_rippling import (
            _posting_to_job,
        )

        posting = {
            "id": "1dc592e2",
            "name": "Director, Investor Relations",
            "url": "https://ats.rippling.com/joinroot/jobs/1dc592e2",
            "department": {"name": "CFO Org"},
            "locations": [{"name": "Remote (United States)", "workplaceType": "REMOTE"}],
        }
        job = _posting_to_job(posting, "joinroot")
        assert job["title"] == "Director, Investor Relations"
        assert job["company_source"] == "Rippling"
        assert job["location"] == "Remote (United States)"
        assert job["source_url"] == "https://ats.rippling.com/joinroot/jobs/1dc592e2"
        assert job["description"] == ""  # list endpoint omits description


class TestIbmScanner:
    def test_fetch_postings_extracts_from_real_fixture(self):
        """Full fetch+map from real captured response."""
        import json

        from jobcannon.engine.ats_platforms._platforms_ibm import (
            _fetch_postings,
        )

        with open("tests/engine/fixtures/ibm_search_api_response.json") as f:
            fixture = json.load(f)

        with patch("jobcannon.engine.ats_platforms._platforms_ibm.get_session") as mock_get_session:
            ats_session_method(mock_get_session, "post").return_value = _FakeResp(200, fixture)
            postings = _fetch_postings("ibm")
        assert len(postings) == 5
        # First posting from fixture
        assert postings[0]["title"] == "Quantum Hardware Design Engineer"
        assert postings[0]["field_text_01"] == 108263
        assert postings[0]["field_keyword_19"] == "Yorktown Heights, US"

    def test_posting_to_job_maps_all_canonical_keys(self):
        """Full field-mapping assertion — all canonical keys emitted correctly."""
        from jobcannon.engine.ats_platforms._platforms_ibm import (
            _posting_to_job,
        )

        posting = {
            "field_text_01": 108263,
            "title": "Quantum Hardware Design Engineer",
            "field_keyword_05": "United States",
            "field_keyword_08": "Infrastructure & Technology",
            "field_keyword_19": "Yorktown Heights, US",
        }
        job = _posting_to_job(posting, "ibm")
        # Assert all canonical keys are present and correctly mapped
        assert job["title"] == "Quantum Hardware Design Engineer"
        assert job["company_source"] == "IBM"
        assert job["location"] == "Yorktown Heights, US, United States"
        assert job["source_url"] == "https://careers.ibm.com/careers/JobDetail?jobId=108263"
        assert job["source_id"] == "108263"  # Coerced from int to str
        assert job["department"] == "Infrastructure & Technology"
        assert job["description"] == ""  # Not exposed in list response
        assert job["salary_min"] is None
        assert job["salary_max"] is None
        assert job["comp_json"] is None
        assert job["posted_date"] is None
        assert job["is_remote"] is None
        assert job["employment_type"] is None
        assert "locations_structured" in job

    def test_posting_to_job_returns_none_for_missing_job_id(self):
        """None-guard: posting missing field_text_01 returns None."""
        from jobcannon.engine.ats_platforms._platforms_ibm import (
            _posting_to_job,
        )

        posting = {
            "title": "Engineer",
            "field_keyword_05": "us",
            "field_keyword_08": "Engineering",
            "field_keyword_19": "Armonk",
        }
        assert _posting_to_job(posting, "ibm") is None

    def test_fetch_postings_paginates_correctly(self):
        """Pagination: two mocked pages — full page then short page, offset advances."""
        from jobcannon.engine.ats_platforms._platforms_ibm import (
            _PAGE_SIZE,
            _fetch_postings,
        )

        page1_hits = [
            {"_source": {"field_text_01": i, "title": f"Job {i}"}} for i in range(_PAGE_SIZE)
        ]
        page2_hits = [{"_source": {"field_text_01": _PAGE_SIZE, "title": "Last Job"}}]
        page1 = {"hits": {"hits": page1_hits}}
        page2 = {"hits": {"hits": page2_hits}}

        responses = [_FakeResp(200, page1), _FakeResp(200, page2)]
        with patch("jobcannon.engine.ats_platforms._platforms_ibm.get_session") as mock_get_session:
            ats_session_method(mock_get_session, "post").side_effect = responses
            postings = _fetch_postings("ibm")
        assert len(postings) == _PAGE_SIZE + 1
        # Verify all _source dicts were extracted
        assert all(isinstance(p, dict) and "field_text_01" in p for p in postings)

    def test_fetch_postings_raises_board_gone_on_first_page_404(self):
        """Board-gone: first-page 404 raises BoardGoneError."""
        from jobcannon.engine.ats_platforms._platforms_ibm import (
            _fetch_postings,
        )
        from jobcannon.engine.ats_platforms._registry import BoardGoneError

        with patch("jobcannon.engine.ats_platforms._platforms_ibm.get_session") as mock_get_session:
            ats_session_method(mock_get_session, "post").return_value = _FakeResp(404)
            with pytest.raises(BoardGoneError):
                _fetch_postings("ibm")

    def test_integration_via_run_platform_scan(self):
        """Integration via run_platform_scan: exercises title_of lambda + title-gate + None-skip."""
        import json

        from jobcannon.engine.ats_platforms._platforms_ibm import SCANNER
        from jobcannon.engine.ats_platforms._registry import run_platform_scan

        with open("tests/engine/fixtures/ibm_search_api_response.json") as f:
            fixture = json.load(f)

        with patch("jobcannon.engine.ats_platforms._platforms_ibm.get_session") as mock_get_session:
            ats_session_method(mock_get_session, "post").return_value = _FakeResp(200, fixture)
            jobs, skipped = run_platform_scan(SCANNER, "ibm", [], [])
        # All 5 postings should be converted to jobs
        assert len(jobs) == 5
        # First job should have correct mapping
        assert jobs[0]["title"] == "Quantum Hardware Design Engineer"
        assert jobs[0]["company_source"] == "IBM"
        assert jobs[0]["source_id"] == "108263"


class TestJobviteScannerIsStub:
    def test_fetch_postings_always_returns_empty(self):
        from jobcannon.engine.ats_platforms._platforms_jobvite import (
            _fetch_postings,
        )

        assert _fetch_postings("any-slug") == []

    # test_scanner_is_registered deliberately not ported: it imports
    # jobcannon.engine.ats_scanner._run, which is Task 3 scope (not ported
    # by this PR).


# TestDispatcherWiring deliberately not ported in full: every test imports
# jobcannon.engine.ats_scanner._run / _probe, or
# jobcannon.engine.ats_identity_reconcile — all Task 3 scope (not ported by
# this PR). See the PR body's "tests not ported" list.


# ---------------------------------------------------------------------------
# NON_SCANNABLE_PLATFORMS invariants
# ---------------------------------------------------------------------------


class TestNonScannablePlatformsConstant:
    """NON_SCANNABLE_PLATFORMS is a frozenset, contains jobvite, and is a
    subset of registered scanner names (no phantom entries)."""

    def test_non_scannable_platforms_is_frozenset(self):
        from jobcannon.engine.ats_registry import NON_SCANNABLE_PLATFORMS

        assert isinstance(NON_SCANNABLE_PLATFORMS, frozenset)

    def test_jobvite_in_non_scannable_platforms(self):
        from jobcannon.engine.ats_registry import NON_SCANNABLE_PLATFORMS

        assert "jobvite" in NON_SCANNABLE_PLATFORMS

    def test_non_scannable_platforms_subset_of_registered_scanners(self):
        """Every entry in NON_SCANNABLE_PLATFORMS must be a registered platform name.
        Some non-scannable platforms have no scanner (domain-only entries for email matching)."""
        from jobcannon.engine.ats_registry import NON_SCANNABLE_PLATFORMS, PLATFORMS

        assert set(PLATFORMS.keys()) >= NON_SCANNABLE_PLATFORMS

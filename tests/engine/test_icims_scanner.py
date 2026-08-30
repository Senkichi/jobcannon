"""Tests for the iCIMS Playwright scanner.

Covers two of the three architectural seams from the private-repo original
(the third, Orchestrator / ``ats_scanner._run_playwright``, is Task 3 scope
and is not ported here — see the module-level note below):

1. Scanner (``_platforms_icims``): a fake Playwright Browser/Page rendering a
   saved iCIMS board HTML fixture → raw postings → canonical job dicts with
   all required keys; a render exception yields ``[]`` and never raises.
2. Probe (``ats_prober._probe_icims``): ``True`` for an iCIMS-marker body,
   ``False`` for 404 / non-marker — mocking ``requests.get`` per the existing
   ``_probe_*`` test patterns.
"""

from __future__ import annotations

from unittest.mock import patch

# Canonical-dict keys the upsert path and downstream consumers rely on.
_REQUIRED_KEYS = {
    "title",
    "company_source",
    "location",
    "locations_structured",
    "description",
    "source_url",
    "source_id",
    "posted_date",
    "posted_date_precision",
    "salary_min",
    "salary_max",
    "comp_json",
}

# A saved iCIMS search-results board (classic ``iCIMS_JobsTable`` markup).
# Row 1 uses a relative href; row 2 an absolute href; row 3 a relative href.
_BOARD_HTML = """
<html><body>
<div class="iCIMS_JobsTable">
  <div class="iCIMS_JobListingRow">
    <h3 class="title">
      <a class="iCIMS_Anchor" href="/jobs/12345/senior-data-scientist/job">Senior Data Scientist</a>
    </h3>
    <span class="iCIMS_JobHeaderTag iCIMS_JobLocation">US-CA-San Francisco</span>
  </div>
  <div class="iCIMS_JobListingRow">
    <h3 class="title">
      <a class="iCIMS_Anchor"
         href="https://careers-acme.icims.com/jobs/67890/marketing-coordinator/job">Marketing Coordinator</a>
    </h3>
    <span class="iCIMS_JobLocation">US-NY-New York</span>
  </div>
  <div class="iCIMS_JobListingRow">
    <h3 class="title">
      <a class="iCIMS_Anchor" href="/jobs/24680/machine-learning-engineer/job">Machine Learning Engineer</a>
    </h3>
    <span class="iCIMS_JobLocation">Remote</span>
  </div>
</div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Fake Playwright Browser / Page
# ---------------------------------------------------------------------------


class _FakePage:
    """Minimal stand-in for a Playwright Page returning fixture HTML."""

    def __init__(self, html: str, *, fail: bool = False) -> None:
        self._html = html
        self._fail = fail
        self.closed = False

    def goto(self, url: str, **kwargs) -> None:
        if self._fail:
            raise RuntimeError("simulated render failure")

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def content(self) -> str:
        return self._html

    def query_selector(self, selector: str):
        # No "load more" control — terminates the pagination loop after the
        # initial render.
        return None

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    def new_page(self) -> _FakePage:
        return self._page

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Scanner: render + extraction
# ---------------------------------------------------------------------------


class TestIcimsFetchPostings:
    def test_extracts_raw_postings_from_rendered_html(self):
        from jobcannon.engine.ats_platforms._platforms_icims import _fetch_postings

        page = _FakePage(_BOARD_HTML)
        postings = _fetch_postings(_FakeBrowser(page), "acme")

        assert len(postings) == 3
        titles = {p["title"] for p in postings}
        assert titles == {
            "Senior Data Scientist",
            "Marketing Coordinator",
            "Machine Learning Engineer",
        }
        first = next(p for p in postings if p["title"] == "Senior Data Scientist")
        assert first["source_id"] == "12345"
        assert first["source_url"].endswith("/jobs/12345/senior-data-scientist/job")
        assert first["location"] == "US-CA-San Francisco"
        # The page is closed in the finally block.
        assert page.closed is True

    def test_absolute_href_preserved(self):
        from jobcannon.engine.ats_platforms._platforms_icims import _fetch_postings

        postings = _fetch_postings(_FakeBrowser(_FakePage(_BOARD_HTML)), "acme")
        coord = next(p for p in postings if p["title"] == "Marketing Coordinator")
        assert coord["source_url"] == (
            "https://careers-acme.icims.com/jobs/67890/marketing-coordinator/job"
        )

    def test_render_exception_yields_empty_never_raises(self):
        from jobcannon.engine.ats_platforms._platforms_icims import _fetch_postings

        page = _FakePage(_BOARD_HTML, fail=True)
        # Must swallow the exception and return [].
        assert _fetch_postings(_FakeBrowser(page), "acme") == []
        # The page is still closed despite the failure.
        assert page.closed is True

    def test_new_page_failure_yields_empty(self):
        from jobcannon.engine.ats_platforms._platforms_icims import _fetch_postings

        class _BoomBrowser:
            def new_page(self):
                raise RuntimeError("no page")

        assert _fetch_postings(_BoomBrowser(), "acme") == []


class TestIcimsPostingToJob:
    def test_canonical_dict_has_all_required_keys(self):
        from jobcannon.engine.ats_platforms._platforms_icims import _posting_to_job

        posting = {
            "title": "Senior Data Scientist",
            "source_url": "https://careers-acme.icims.com/jobs/12345/x/job",
            "source_id": "12345",
            "location": "US-CA-San Francisco",
        }
        job = _posting_to_job(posting, "acme")
        assert set(job.keys()) == _REQUIRED_KEYS
        assert job["company_source"] == "iCIMS"
        assert job["title"] == "Senior Data Scientist"
        assert job["location"] == "US-CA-San Francisco"
        assert job["source_id"] == "12345"
        assert job["locations_structured"] == []
        # Description deferred to enrichment; date intentionally absent (D-08).
        assert job["description"] == ""
        assert job["posted_date"] is None


class TestBoardUrl:
    def test_bare_slug_wraps_in_careers_prefix(self):
        from jobcannon.engine.ats_platforms._platforms_icims import _board_url

        assert _board_url("acme") == "https://careers-acme.icims.com/jobs/search?ss=1"

    def test_explicit_icims_host_slug_used_verbatim(self):
        from jobcannon.engine.ats_platforms._platforms_icims import _board_url

        assert _board_url("jobs-acme.icims.com") == "https://jobs-acme.icims.com/jobs/search?ss=1"

    def test_scheme_prefixed_slug_with_path_extracts_host_only(self):
        from jobcannon.engine.ats_platforms._platforms_icims import _board_url

        assert (
            _board_url("https://careers-acme.icims.com/some/path")
            == "https://careers-acme.icims.com/jobs/search?ss=1"
        )

    def test_lookalike_domain_is_not_mistaken_for_icims_host(self):
        # Regression pin for CodeQL py/incomplete-url-substring-sanitization:
        # the old substring check (`"icims.com" in s`) matched "noticims.com"
        # and treated it as an explicit iCIMS host; hostname parsing must not.
        from jobcannon.engine.ats_platforms._platforms_icims import _board_url

        assert (
            _board_url("noticims.com") == "https://careers-noticims.com.icims.com/jobs/search?ss=1"
        )

    def test_whitespace_padded_bare_slug_matches_bare_slug(self):
        from jobcannon.engine.ats_platforms._platforms_icims import _board_url

        assert _board_url("  acme  ") == _board_url("acme")


# TestIcimsDriverTitleGate deliberately not ported: both tests import
# jobcannon.engine.ats_scanner._run_playwright, which is Task 3 scope (not
# ported by this PR). See the PR body's "tests not ported" list.

# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class TestProbeIcims:
    def test_marker_body_is_hit(self):
        from jobcannon.engine.ats_prober import _probe_icims

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, "<html>Powered by iCIMS</html>"),
        ):
            assert _probe_icims("acme") is True

    def test_non_marker_body_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_icims

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, "<html>some other portal</html>"),
        ):
            assert _probe_icims("acme") is False

    def test_404_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_icims

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(404, "iCIMS not found"),
        ):
            assert _probe_icims("acme") is False

    def test_request_exception_is_miss(self):
        from jobcannon.engine.ats_prober import _probe_icims

        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            side_effect=RuntimeError("boom"),
        ):
            assert _probe_icims("acme") is False


# TestIcimsOrchestrator deliberately not ported: all three tests import
# jobcannon.engine.ats_scanner._run_playwright / _run (Task 3 scope, not
# ported by this PR), and two use the unported migrated_db fixture. See the
# PR body's "tests not ported" list.

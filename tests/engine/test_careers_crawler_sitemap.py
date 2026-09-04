# PORTED from tests/test_careers_crawler_sitemap.py @ a7f0f38a85dfa0af4d305c04da833785f723d649 (private job-cannon). Ledger L-0337.
"""Unit tests for the sitemap/RSS crawler tier (Stage 5).

Covers the acceptance criteria in NO-KEY-COMPENSATION-PLAN.md Stage 5:
  (a) valid sitemap with job URLs
  (b) sitemap index with child sitemaps
  (c) 404 sitemap → fallthrough
  (d) malformed XML → fallthrough without crash

Plus the helper functions (`_title_from_url`, `_is_job_url`,
`_extract_urls_from_sitemap`, `_local_name`, `_root_url`) and the RSS/Atom
fallback path.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import requests
from defusedxml import ElementTree as DefusedET
from requests import Response
from requests.adapters import BaseAdapter

from jobcannon.engine import http_fetch
from jobcannon.engine.careers_crawler._sitemap_tier import (
    _extract_urls_from_sitemap,
    _is_job_url,
    _local_name,
    _root_url,
    _title_from_url,
    _try_rss,
    _try_sitemap_extract,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_URLSET_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://co.com/jobs/senior-software-engineer-12345</loc></url>
  <url><loc>https://co.com/jobs/data-scientist-67890</loc></url>
  <url><loc>https://co.com/about</loc></url>
  <url><loc>https://co.com/careers/qa-engineer-99</loc></url>
</urlset>"""

_SITEMAPINDEX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://co.com/sitemap-jobs.xml</loc></sitemap>
  <sitemap><loc>https://co.com/sitemap-blog.xml</loc></sitemap>
</sitemapindex>"""

_CHILD_URLSET_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://co.com/jobs/backend-engineer-555</loc></url>
</urlset>"""

_RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Careers Feed</title>
    <item>
      <title>Senior Engineer</title>
      <link>https://co.com/jobs/senior-engineer-201</link>
    </item>
    <item>
      <title>Designer</title>
      <link>https://co.com/jobs/designer-202</link>
    </item>
  </channel>
</rss>"""

_ATOM_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Backend Engineer</title>
    <link href="https://co.com/jobs/backend-engineer-303"/>
  </entry>
</feed>"""

_NOT_XML = b"<<<not actually xml at all>>>"


def _mk_resp(status: int, body: bytes) -> MagicMock:
    """Build a MagicMock response object with status_code + content attrs."""
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    return resp


def _404(_url: str, **_kwargs) -> MagicMock:
    return _mk_resp(404, b"")


# ---------------------------------------------------------------------------
# Unit: _local_name
# ---------------------------------------------------------------------------


class TestLocalName:
    def test_strips_namespace(self):
        root = DefusedET.fromstring(_URLSET_XML)
        assert _local_name(root) == "urlset"

    def test_returns_tag_when_no_namespace(self):
        root = DefusedET.fromstring(b"<rss/>")
        assert _local_name(root) == "rss"


# ---------------------------------------------------------------------------
# Unit: _root_url
# ---------------------------------------------------------------------------


class TestRootUrl:
    def test_https_with_path(self):
        assert _root_url("https://company.com/careers") == "https://company.com"

    def test_subdomain(self):
        assert _root_url("https://careers.company.com/") == "https://careers.company.com"

    def test_unparseable(self):
        assert _root_url("not-a-url") == ""

    def test_empty(self):
        assert _root_url("") == ""


# ---------------------------------------------------------------------------
# Unit: _is_job_url
# ---------------------------------------------------------------------------


class TestIsJobUrl:
    def test_jobs_path(self):
        assert _is_job_url("https://co.com/jobs/role-123")

    def test_careers_path(self):
        assert _is_job_url("https://co.com/careers/role")

    def test_positions_path(self):
        assert _is_job_url("https://co.com/positions/foo")

    def test_openings_path(self):
        assert _is_job_url("https://co.com/openings/bar")

    def test_uppercase_path_matches(self):
        # Path is lowercased before substring check
        assert _is_job_url("https://co.com/Jobs/Role")

    def test_non_job_path(self):
        assert not _is_job_url("https://co.com/about")
        assert not _is_job_url("https://co.com/blog/post")


# ---------------------------------------------------------------------------
# Unit: _title_from_url
# ---------------------------------------------------------------------------


class TestTitleFromUrl:
    def test_simple_slug(self):
        assert (
            _title_from_url("https://co.com/jobs/senior-software-engineer")
            == "Senior Software Engineer"
        )

    def test_strips_trailing_numeric_id(self):
        assert (
            _title_from_url("https://co.com/jobs/senior-software-engineer-12345")
            == "Senior Software Engineer"
        )

    def test_strips_trailing_hex_id(self):
        # 8+ hex chars are treated as IDs
        assert _title_from_url("https://co.com/jobs/data-scientist-a1b2c3d4e5") == "Data Scientist"

    def test_preserves_single_digit_trailing(self):
        # 2+ digits get stripped as IDs; a single digit is preserved in
        # case it encodes a role level (rare but harmless).
        assert _title_from_url("https://co.com/jobs/engineer-3") == "Engineer 3"

    def test_strips_two_digit_trailing_id(self):
        # 2-digit trailing numerics are typical job IDs and get stripped.
        assert _title_from_url("https://co.com/jobs/qa-tester-99") == "Qa Tester"

    def test_underscore_separator(self):
        assert _title_from_url("https://co.com/jobs/back_end_engineer") == "Back End Engineer"

    def test_preserves_all_caps_tokens(self):
        # "QA" should stay uppercase, not "Qa"
        assert _title_from_url("https://co.com/jobs/QA-tester") == "QA Tester"

    def test_root_returns_empty(self):
        assert _title_from_url("https://co.com/") == ""

    def test_empty_returns_empty(self):
        assert _title_from_url("") == ""


# ---------------------------------------------------------------------------
# Unit: _extract_urls_from_sitemap
# ---------------------------------------------------------------------------


class TestExtractUrlsFromSitemap:
    def test_urlset_returns_loc_urls(self):
        root = DefusedET.fromstring(_URLSET_XML)
        urls = _extract_urls_from_sitemap(root)
        assert "https://co.com/jobs/senior-software-engineer-12345" in urls
        assert "https://co.com/jobs/data-scientist-67890" in urls
        # Note: extracts all loc URLs, including non-job ones — filtering
        # happens in _try_sitemap_extract.
        assert "https://co.com/about" in urls

    def test_sitemapindex_recurses_into_children(self):
        """sitemapindex root should fetch each child sitemap and merge URLs."""
        with patch("jobcannon.engine.careers_crawler._sitemap_tier._fetch_xml") as mock_fetch:
            # _fetch_xml is called once per <loc> in the index; return
            # the same child urlset for both children.
            mock_fetch.return_value = DefusedET.fromstring(_CHILD_URLSET_XML)
            index_root = DefusedET.fromstring(_SITEMAPINDEX_XML)
            urls = _extract_urls_from_sitemap(index_root)
            assert "https://co.com/jobs/backend-engineer-555" in urls
            # 2 children in index → 2 fetches (under the _MAX_CHILD_SITEMAPS=3 cap)
            assert mock_fetch.call_count == 2

    def test_sitemapindex_recursion_capped(self):
        """A sitemapindex returned BY a child sitemap (depth=1) must not
        trigger another round of fetches — depth guard kicks in."""
        index_xml = b"""<?xml version="1.0"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://co.com/inner-index.xml</loc></sitemap>
        </sitemapindex>"""
        # Simulate: outer index points at another index — child fetch returns
        # a sitemapindex; the depth=1 guard should treat that as empty (return []).
        with patch("jobcannon.engine.careers_crawler._sitemap_tier._fetch_xml") as mock_fetch:
            mock_fetch.return_value = DefusedET.fromstring(index_xml)  # child is another index
            urls = _extract_urls_from_sitemap(DefusedET.fromstring(index_xml))
            # Depth=1 sitemapindex returns []; only the outer fetch fires
            assert urls == []
            assert mock_fetch.call_count == 1

    def test_sitemapindex_caps_child_fetches(self):
        """Sitemapindex with > _MAX_CHILD_SITEMAPS children fetches at most cap."""
        many_children = (
            b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + b"".join(
                f"<sitemap><loc>https://co.com/sm-{i}.xml</loc></sitemap>".encode()
                for i in range(10)
            )
            + b"</sitemapindex>"
        )
        with patch("jobcannon.engine.careers_crawler._sitemap_tier._fetch_xml") as mock_fetch:
            mock_fetch.return_value = DefusedET.fromstring(_CHILD_URLSET_XML)
            _extract_urls_from_sitemap(DefusedET.fromstring(many_children))
            # _MAX_CHILD_SITEMAPS = 3
            assert mock_fetch.call_count == 3

    def test_unknown_root_returns_empty(self):
        """A root that isn't urlset / sitemapindex returns []."""
        unrelated = DefusedET.fromstring(b"<foo><bar/></foo>")
        assert _extract_urls_from_sitemap(unrelated) == []


# ---------------------------------------------------------------------------
# Integration: _try_sitemap_extract
# ---------------------------------------------------------------------------


class TestTrySitemapExtractAcceptanceCriteria:
    """PLAN.md Stage 5 (d) sitemap fetcher must handle these without raising:
    valid sitemap, sitemap index, 404, malformed XML."""

    def test_a_valid_sitemap_with_job_urls(self):
        def fake_get(url, **_kwargs):
            if url.endswith("/sitemap.xml"):
                return _mk_resp(200, _URLSET_XML)
            return _404(url)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer", "scientist"], [])
            # Engineer + scientist jobs should match; about-page filtered out
            titles = {j["title"] for j in jobs}
            assert "Senior Software Engineer" in titles
            assert "Data Scientist" in titles
            # qa-engineer in URL → "Qa Engineer" (URL slugs are lowercase; the
            # all-caps preservation only triggers when the input already has
            # an uppercase token). Functional matching via _title_matches is
            # case-insensitive so this is cosmetic only.
            assert "Qa Engineer" in titles
            # /about doesn't match _is_job_url → filtered
            for job in jobs:
                assert "/about" not in job["url"]
                assert job["description"] == ""

    def test_b_sitemap_index_with_child_sitemaps(self):
        def fake_get(url, **_kwargs):
            if url.endswith("/sitemap.xml"):
                return _mk_resp(200, _SITEMAPINDEX_XML)
            if "sitemap-jobs.xml" in url or "sitemap-blog.xml" in url:
                return _mk_resp(200, _CHILD_URLSET_XML)
            return _404(url)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer"], [])
            assert len(jobs) == 1
            assert jobs[0]["title"] == "Backend Engineer"
            assert jobs[0]["url"] == "https://co.com/jobs/backend-engineer-555"

    def test_c_404_sitemap_returns_empty(self):
        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = _404
            # All sitemap + RSS candidates return 404 → empty result
            assert _try_sitemap_extract("https://co.com/careers", ["engineer"], []) == []

    def test_d_malformed_xml_returns_empty(self):
        def fake_get(url, **_kwargs):
            # Every candidate URL returns garbage that won't parse as XML
            return _mk_resp(200, _NOT_XML)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            # Must not raise — defusedxml's parse exception is caught
            assert _try_sitemap_extract("https://co.com/careers", ["engineer"], []) == []


class TestTrySitemapExtractFiltering:
    def test_title_match_filter_excludes_non_matching(self):
        def fake_get(url, **_kwargs):
            if url.endswith("/sitemap.xml"):
                return _mk_resp(200, _URLSET_XML)
            return _404(url)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            # Only "engineer" — "Data Scientist" should be filtered out
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer"], [])
            for job in jobs:
                assert "engineer" in job["title"].lower()
            assert not any("scientist" in j["title"].lower() for j in jobs)

    def test_exclusion_filter(self):
        # "senior" is excluded → "Senior Software Engineer" should be dropped
        def fake_get(url, **_kwargs):
            if url.endswith("/sitemap.xml"):
                return _mk_resp(200, _URLSET_XML)
            return _404(url)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer"], ["senior"])
            for job in jobs:
                assert "senior" not in job["title"].lower()

    def test_deduplicates_urls(self):
        """Duplicate <loc> entries collapse to a single result row."""
        dup_xml = b"""<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://co.com/jobs/engineer-1</loc></url>
          <url><loc>https://co.com/jobs/engineer-1</loc></url>
        </urlset>"""

        def fake_get(url, **_kwargs):
            if url.endswith("/sitemap.xml"):
                return _mk_resp(200, dup_xml)
            return _404(url)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer"], [])
            urls = [j["url"] for j in jobs]
            assert len(urls) == len(set(urls))


# ---------------------------------------------------------------------------
# RSS / Atom fallback
# ---------------------------------------------------------------------------


class TestRssFallback:
    def test_rss_used_when_sitemap_returns_nothing(self):
        """All sitemap candidates 404 → fall through to RSS."""

        def fake_get(url, **_kwargs):
            if "/jobs.rss" in url or "/careers.rss" in url or url.endswith("/rss"):
                return _mk_resp(200, _RSS_XML)
            return _404(url)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer"], [])
            assert any(j["title"] == "Senior Engineer" for j in jobs)

    def test_atom_feed_at_careers_url_extension(self):
        """`<careers_url>.atom` is in the RSS candidate list."""

        def fake_get(url, **_kwargs):
            if url.endswith("/careers.atom"):
                return _mk_resp(200, _ATOM_XML)
            return _404(url)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer"], [])
            assert any(j["title"] == "Backend Engineer" for j in jobs)

    def test_rss_not_called_if_sitemap_has_candidates(self):
        """A successful sitemap pre-empts the RSS pass — RSS must not be fetched."""
        rss_call_count = 0

        def fake_get(url, **_kwargs):
            nonlocal rss_call_count
            if "/rss" in url or url.endswith(".atom"):
                rss_call_count += 1
                return _mk_resp(200, _RSS_XML)
            if url.endswith("/sitemap.xml"):
                return _mk_resp(200, _URLSET_XML)
            return _404(url)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            _try_sitemap_extract("https://co.com/careers", ["engineer"], [])
            assert rss_call_count == 0

    def test_try_rss_returns_empty_when_no_feeds(self):
        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.get",
            side_effect=_404,
        ):
            assert _try_rss("https://co.com", "https://co.com/careers") == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_careers_url_returns_empty(self):
        assert _try_sitemap_extract("", ["engineer"], []) == []

    def test_unparseable_careers_url_returns_empty(self):
        assert _try_sitemap_extract("not-a-url", ["engineer"], []) == []

    def test_connection_error_returns_empty(self):
        """A networking exception inside session.get must not crash the tier."""
        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = ConnectionError("DNS failed")
            assert _try_sitemap_extract("https://co.com/careers", ["engineer"], []) == []

    def test_non_200_status_returns_empty(self):
        """Server-error HTTP codes are treated as 'no sitemap'."""

        def fake_get(url, **_kwargs):
            return _mk_resp(503, b"<html>service unavailable</html>")

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            assert _try_sitemap_extract("https://co.com/careers", ["engineer"], []) == []


# ---------------------------------------------------------------------------
# Cohort-legitimacy gate wiring (#1144 follow-up)
#
# The gate's own scoring logic (sampling, hiring-org affinity, location
# chrome detection) is covered in tests/test_cohort_legitimacy.py. These
# tests only cover the sitemap tier's WIRING to it: does a large cohort get
# evaluated, does a positive verdict withhold the cohort + populate
# flag_sink, and — critically — do existing callers that don't pass
# company_name/config keep their original unconditional-passthrough
# behavior (backward compatibility for callers with no company context).
# ---------------------------------------------------------------------------


def _big_urlset_xml(n: int) -> bytes:
    """Build a <urlset> with `n` distinct /jobs/engineer-<i> URLs."""
    urls = "".join(f"<url><loc>https://co.com/jobs/engineer-{i}</loc></url>" for i in range(n))
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + urls.encode()
        + b"</urlset>"
    )


class TestCohortLegitimacyWiring:
    def test_flagged_cohort_withholds_import_and_populates_sink(self):
        big_xml = _big_urlset_xml(90)

        def fake_get(url, **_kwargs):
            if url.endswith("/sitemap.xml"):
                return _mk_resp(200, big_xml)
            return _404(url)

        fake_verdict = MagicMock(flagged=True, reason="aggregator_suspected:test")
        with (
            patch(
                "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
            ) as mock_session,
            patch(
                "jobcannon.engine.careers_crawler._sitemap_tier.evaluate_cohort_legitimacy",
                return_value=fake_verdict,
            ) as mock_gate,
        ):
            mock_session.return_value.get.side_effect = fake_get
            sink: list[str] = []
            jobs = _try_sitemap_extract(
                "https://co.com/careers",
                ["engineer"],
                [],
                company_name="AggregatorCo",
                config={"careers_crawl": {}},
                flag_sink=sink,
            )

        assert jobs == []
        assert sink == ["aggregator_suspected:test"]
        # The gate is evaluated once, against the full filtered cohort.
        mock_gate.assert_called_once()
        call_args = mock_gate.call_args[0]
        assert call_args[0] == "AggregatorCo"
        assert len(call_args[1]) == 90

    def test_clean_cohort_passes_through_unaffected(self):
        big_xml = _big_urlset_xml(90)

        def fake_get(url, **_kwargs):
            if url.endswith("/sitemap.xml"):
                return _mk_resp(200, big_xml)
            return _404(url)

        fake_verdict = MagicMock(flagged=False, reason=None)
        with (
            patch(
                "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
            ) as mock_session,
            patch(
                "jobcannon.engine.careers_crawler._sitemap_tier.evaluate_cohort_legitimacy",
                return_value=fake_verdict,
            ),
        ):
            mock_session.return_value.get.side_effect = fake_get
            sink: list[str] = []
            jobs = _try_sitemap_extract(
                "https://co.com/careers",
                ["engineer"],
                [],
                company_name="CleanCo",
                config={"careers_crawl": {}},
                flag_sink=sink,
            )

        assert len(jobs) == 90
        assert sink == []

    def test_backward_compatible_callers_unaffected_by_gate(self):
        """Existing callers that omit company_name/config/flag_sink (the
        acceptance-criteria tests above, and any pre-#1144 caller) must see
        the tier's original unconditional-passthrough behavior — the real
        (unmocked) gate no-ops without a company name to check against, even
        on a cohort well above the size trip-wire, and never issues the
        sample-fetch network calls."""
        big_xml = _big_urlset_xml(90)
        fetch_urls: list[str] = []

        def fake_get(url, **_kwargs):
            fetch_urls.append(url)
            if url.endswith("/sitemap.xml"):
                return _mk_resp(200, big_xml)
            return _404(url)

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer"], [])

        assert len(jobs) == 90
        # Only the sitemap-discovery fetches happened — no per-posting
        # sample fetches from the gate (it no-op'd on empty company_name).
        assert all("/jobs/" not in u for u in fetch_urls)


class TestSessionReuse:
    """Regression test for issue #1127: a single requests.Session should be
    created per `_try_sitemap_extract` call and reused for every sitemap/RSS
    GET issued for that company.
    """

    def test_session_created_once_and_reused_for_all_candidates(self):
        """All 3 sitemap + 4 RSS candidates are fetched through the same
        session.get when nothing succeeds, proving connection reuse."""

        def fake_get(url, **_kwargs):
            return _mk_resp(404, b"")

        with patch(
            "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session"
        ) as mock_session:
            mock_session.return_value.get.side_effect = fake_get
            _try_sitemap_extract("https://co.com/careers", ["engineer"], [])

        assert mock_session.call_count == 1
        get_calls = mock_session.return_value.get.call_args_list
        assert len(get_calls) == 7
        urls = [call.args[0] for call in get_calls]
        assert any(url.endswith("/sitemap.xml") for url in urls)
        assert any(url.endswith("/rss") for url in urls)
        assert any(url.endswith(".atom") for url in urls)


class TestSessionCloseAbandon:
    """fetch_with_deadline abandon semantics vs Session.close() (issue #1163).

    A straggler thread can still hold a connection from the per-company
    session when ``session.close()`` runs in the ``finally`` block. This
    test verifies the close is safe and that a fresh session afterward is
    unaffected.
    """

    def test_close_while_straggler_outstanding_then_fresh_session(self):
        release = threading.Event()

        class _SlowAdapter(BaseAdapter):
            def __init__(self, release: threading.Event):
                self.release = release

            def send(self, request, **kwargs):
                self.release.wait()
                resp = Response()
                resp.status_code = 200
                resp._content = b""
                resp.request = request
                return resp

            def close(self):
                self.release.set()

        class _NormalAdapter(BaseAdapter):
            def send(self, request, **kwargs):
                resp = Response()
                resp.status_code = 200
                resp._content = _URLSET_XML
                resp.request = request
                return resp

            def close(self):
                pass

        class _FakeSession(requests.Session):
            _call_count = 0
            close_count = 0

            def __init__(self):
                super().__init__()
                _FakeSession._call_count += 1
                if _FakeSession._call_count == 1:
                    self.mount("https://", _SlowAdapter(release))
                    self.mount("http://", _SlowAdapter(release))
                else:
                    self.mount("https://", _NormalAdapter())
                    self.mount("http://", _NormalAdapter())

            def close(self):
                _FakeSession.close_count += 1
                super().close()

        _FakeSession._call_count = 0
        _FakeSession.close_count = 0

        def _deadline_getter(url, getter, **kwargs):
            return http_fetch.fetch_with_deadline(
                url, total_deadline_s=0.1, getter=getter, **kwargs
            )

        with (
            patch(
                "jobcannon.engine.careers_crawler._sitemap_tier.requests.Session",
                new=_FakeSession,
            ),
            patch(
                "jobcannon.engine.careers_crawler._sitemap_tier.fetch_with_deadline",
                new=_deadline_getter,
            ),
        ):
            # Every probe blocks on the release event; the finally-closed
            # session must not raise and must leave the worker cleanly.
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer"], [])
            assert jobs == []
            assert _FakeSession.close_count == 1

            # A fresh session with a normal adapter must produce jobs normally,
            # proving the close/timeout interaction did not corrupt anything.
            jobs = _try_sitemap_extract("https://co.com/careers", ["engineer"], [])
            assert len(jobs) > 0
            assert _FakeSession.close_count == 2
            assert any("Engineer" in j["title"] for j in jobs)

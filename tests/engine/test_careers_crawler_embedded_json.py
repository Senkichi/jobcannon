# PORTED from tests/test_careers_crawler_embedded_json.py @ a5d91a0912589576458f3732aab6189034cab6fc (private job-cannon). Ledger L-0599.
"""Tests for the embedded JSON extraction tier (issue #562)."""

from jobcannon.engine.careers_crawler._embedded_json_tier import (
    _try_embedded_json_extract_from_html,
)


class TestEmbeddedJsonTier:
    """Tests for the embedded JSON extraction tier."""

    def test_extracts_from_next_data_fixture(self):
        """Walker extracts jobs from __NEXT_DATA__ fixture."""
        fixture_path = "tests/fixtures/next_data_jobs.html"
        with open(fixture_path, encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["software engineer", "data scientist", "product manager"]
        exclusions = []

        result = _try_embedded_json_extract_from_html(html, base_url, target_titles, exclusions)

        assert result is not None
        assert len(result) == 3
        titles = {job["title"] for job in result}
        assert "Senior Software Engineer" in titles
        assert "Data Scientist" in titles
        assert "Product Manager" in titles

        # URLs should be resolved
        urls = [job["url"] for job in result]
        assert all(url.startswith("https://example.com") for url in urls)

    def test_extracts_from_nuxt_fixture(self):
        """Walker extracts jobs from __NUXT__ fixture."""
        fixture_path = "tests/fixtures/nuxt_jobs.html"
        with open(fixture_path, encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["backend engineer", "frontend developer"]
        exclusions = []

        result = _try_embedded_json_extract_from_html(html, base_url, target_titles, exclusions)

        assert result is not None
        assert len(result) == 2
        titles = {job["title"] for job in result}
        assert "Senior Backend Engineer" in titles
        assert "Frontend Developer" in titles

    def test_decoy_fixture_returns_none(self):
        """Decoy fixture with product list returns None (false-positive guard)."""
        fixture_path = "tests/fixtures/next_data_decoy.html"
        with open(fixture_path, encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["widget", "gadget"]  # Would match if not for title+url gate
        exclusions = []

        result = _try_embedded_json_extract_from_html(html, base_url, target_titles, exclusions)

        # Should return None because the array lacks title+url keys
        assert result is None

    def test_realistic_nav_decoy_yields_no_jobs(self):
        """Nav/category array WITH genuine title+url keys yields zero jobs.

        The products decoy (next_data_decoy.html) fails at the coarse title+url
        KEY gate, so it never exercises the meaningful guard. A real department
        navigation menu carries genuine {title, url} keys, PASSES the key gate,
        and is selected by the walker — so the tier must reject it downstream on
        title SEMANTICS via the role-aware _title_matches() filter. Result is an
        empty list (candidate array found, but no item survives the title match),
        NOT None (which would mean no candidate array was found at all).
        """
        fixture_path = "tests/fixtures/next_data_nav_decoy.html"
        with open(fixture_path, encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        # Realistic job targets whose keywords overlap the nav labels
        # ("Engineering" vs "engineer", "Data Science" vs "data scientist").
        # A naive substring filter would false-positive; _title_matches is
        # word/role-aware and must reject every department label.
        target_titles = ["software engineer", "data scientist", "product manager"]
        exclusions = []

        result = _try_embedded_json_extract_from_html(html, base_url, target_titles, exclusions)

        # Candidate array WAS found (title+url keys present) but every nav label
        # is rejected by the role-aware title filter -> empty list, not None.
        assert result == []

    def test_title_hygiene_applied(self):
        """Extracted titles run through clean_title for hygiene."""
        fixture_path = "tests/fixtures/next_data_jobs.html"
        with open(fixture_path, encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["software engineer"]
        exclusions = []

        result = _try_embedded_json_extract_from_html(html, base_url, target_titles, exclusions)

        assert result is not None
        # Title should be cleaned (no location suffix, etc.)
        job = result[0]
        assert job["title"] == "Senior Software Engineer"

    def test_title_filter_applied(self):
        """User's title filter is applied to extracted jobs."""
        fixture_path = "tests/fixtures/next_data_jobs.html"
        with open(fixture_path, encoding="utf-8") as f:
            html = f.read()

        base_url = "https://example.com"
        target_titles = ["software engineer"]  # Only match one
        exclusions = []

        result = _try_embedded_json_extract_from_html(html, base_url, target_titles, exclusions)

        assert result is not None
        assert len(result) == 1
        assert result[0]["title"] == "Senior Software Engineer"

    def test_empty_html_returns_none(self):
        """Empty HTML returns None (escalate signal)."""
        result = _try_embedded_json_extract_from_html("", "https://example.com", ["engineer"], [])
        assert result is None

    def test_no_embedded_json_returns_none(self):
        """HTML without embedded JSON returns None."""
        html = "<html><body>No JSON here</body></html>"
        result = _try_embedded_json_extract_from_html(html, "https://example.com", ["engineer"], [])
        assert result is None

    def test_malformed_json_returns_none(self):
        """Malformed JSON in script tags returns None (defensive)."""
        html = """
        <html>
        <script id="__NEXT_DATA__" type="application/json">
        { invalid json }
        </script>
        </html>
        """
        result = _try_embedded_json_extract_from_html(html, "https://example.com", ["engineer"], [])
        assert result is None

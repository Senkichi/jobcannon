"""Tests for Phase B ATS posting_to_job field-alias tolerance.

Regression-guards the current Lever (text/hostedUrl) and Greenhouse
(title/absolute_url) canonical keys AND verifies that alias keys still
resolve when the canonical key is absent.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._platforms_greenhouse import SCANNER as GH
from jobcannon.engine.ats_platforms._platforms_lever import SCANNER as LEVER

# ---------------------------------------------------------------------------
# Lever — regression guards (current keys must still work)
# ---------------------------------------------------------------------------


class TestLeverCurrentKeysWork:
    def test_lever_text_is_title(self):
        """REGRESSION GUARD: Lever uses 'text' for the job title."""
        job = LEVER.posting_to_job({"text": "Data Scientist", "hostedUrl": "https://x"}, "acme")
        assert job is not None
        assert job["title"] == "Data Scientist"

    def test_lever_hosted_url_is_source_url(self):
        """REGRESSION GUARD: Lever uses 'hostedUrl' for the apply URL."""
        job = LEVER.posting_to_job(
            {"text": "Engineer", "hostedUrl": "https://jobs.lever.co/acme/1"}, "acme"
        )
        assert job is not None
        assert job["source_url"] == "https://jobs.lever.co/acme/1"

    def test_lever_both_current_keys_round_trip(self):
        """Full posting with both canonical keys produces correct title + url."""
        posting = {
            "text": "Staff Engineer",
            "hostedUrl": "https://jobs.lever.co/stripe/abc-123",
            "categories": {"location": "Remote"},
        }
        job = LEVER.posting_to_job(posting, "stripe")
        assert job["title"] == "Staff Engineer"
        assert job["source_url"] == "https://jobs.lever.co/stripe/abc-123"


# ---------------------------------------------------------------------------
# Lever — alias tolerance (renamed keys still resolve)
# ---------------------------------------------------------------------------


class TestLeverAliasTolerance:
    def test_title_alias_jobTitle(self):
        """If Lever renames 'text' → 'jobTitle', title still resolves."""
        job = LEVER.posting_to_job({"jobTitle": "Data Scientist", "hostedUrl": "https://x"}, "acme")
        assert job is not None
        assert job["title"] == "Data Scientist"

    def test_title_alias_name(self):
        """If Lever renames 'text' → 'name', title still resolves."""
        job = LEVER.posting_to_job({"name": "ML Engineer", "hostedUrl": "https://x"}, "acme")
        assert job is not None
        assert job["title"] == "ML Engineer"

    def test_url_alias_applyUrl(self):
        """If Lever renames 'hostedUrl' → 'applyUrl', url still resolves."""
        job = LEVER.posting_to_job(
            {"text": "Engineer", "applyUrl": "https://example.com/apply"}, "acme"
        )
        assert job is not None
        assert job["source_url"] == "https://example.com/apply"

    def test_missing_title_and_url_coalesce_to_empty_string(self):
        """Neither key present → title and source_url are '' (not None)."""
        job = LEVER.posting_to_job({}, "acme")
        assert job is not None
        assert job["title"] == ""
        assert job["source_url"] == ""


# ---------------------------------------------------------------------------
# Greenhouse — regression guards (current keys must still work)
# ---------------------------------------------------------------------------


class TestGreenhouseCurrentKeysWork:
    def test_greenhouse_title_key(self):
        """REGRESSION GUARD: Greenhouse uses 'title' for the job title."""
        posting = {
            "title": "Senior Data Scientist",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        }
        job = GH.posting_to_job(posting, "acme")
        assert job is not None
        assert job["title"] == "Senior Data Scientist"

    def test_greenhouse_absolute_url_key(self):
        """REGRESSION GUARD: Greenhouse uses 'absolute_url' for the apply URL."""
        posting = {
            "title": "Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/99",
        }
        job = GH.posting_to_job(posting, "acme")
        assert job is not None
        assert job["source_url"] == "https://boards.greenhouse.io/acme/jobs/99"

    def test_greenhouse_both_current_keys_round_trip(self):
        """Full posting with both canonical keys produces correct title + url."""
        posting = {
            "title": "ML Engineer",
            "absolute_url": "https://boards.greenhouse.io/openai/jobs/42",
            "location": {"name": "San Francisco, CA"},
        }
        job = GH.posting_to_job(posting, "openai")
        assert job["title"] == "ML Engineer"
        assert job["source_url"] == "https://boards.greenhouse.io/openai/jobs/42"


# ---------------------------------------------------------------------------
# Greenhouse — alias tolerance (renamed keys still resolve)
# ---------------------------------------------------------------------------


class TestGreenhouseAliasTolerance:
    def test_title_alias_jobTitle(self):
        """If Greenhouse renames 'title' → 'jobTitle', title still resolves."""
        posting = {
            "jobTitle": "Researcher",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/5",
        }
        job = GH.posting_to_job(posting, "acme")
        assert job is not None
        assert job["title"] == "Researcher"

    def test_url_alias_hostedUrl(self):
        """If Greenhouse adopts 'hostedUrl', url still resolves."""
        posting = {"title": "Analyst", "hostedUrl": "https://example.com/job/1"}
        job = GH.posting_to_job(posting, "acme")
        assert job is not None
        assert job["source_url"] == "https://example.com/job/1"

    def test_url_alias_applyUrl(self):
        """If Greenhouse renames 'absolute_url' → 'applyUrl', url still resolves."""
        posting = {"title": "SWE", "applyUrl": "https://example.com/apply"}
        job = GH.posting_to_job(posting, "acme")
        assert job is not None
        assert job["source_url"] == "https://example.com/apply"

    def test_missing_title_and_url_coalesce_to_empty_string(self):
        """Neither key present → title and source_url are '' (not None)."""
        job = GH.posting_to_job({}, "acme")
        assert job is not None
        assert job["title"] == ""
        assert job["source_url"] == ""

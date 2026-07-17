"""Tests for Ashby source_id and posted_date extraction.

Phase 48.06 — Ashby source_id extraction.
F-04: source_id was missing on 98.4% of Ashby rows.

The Ashby posting-api returns ``id`` as the unique posting identifier.
``_posting_to_job`` must emit it as ``source_id`` (string).
``posted_date`` is also extracted from ``publishedAt`` (ISO-8601 string).
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._platforms_ashby import _posting_to_job

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLUG = "AcmeCorp"  # Ashby slugs are case-sensitive


def _minimal_posting(**kwargs) -> dict:
    """Return a minimal Ashby posting fixture; caller may override any field."""
    base: dict = {
        "id": "abc-posting-123",
        "title": "Software Engineer",
        "location": "San Francisco, CA",
        "jobUrl": "https://jobs.ashbyhq.com/AcmeCorp/abc-posting-123",
        "publishedAt": "2024-03-15T09:00:00.000Z",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# source_id extraction (acceptance criteria for Phase 48.06)
# ---------------------------------------------------------------------------


class TestSourceId:
    """source_id must be populated from the ``id`` field."""

    def test_source_id_string_from_id(self):
        """String ``id`` → source_id is that string."""
        result = _posting_to_job(_minimal_posting(id="abc-posting-123"), _SLUG)
        assert result["source_id"] == "abc-posting-123"

    def test_source_id_integer_id_coerced_to_string(self):
        """Integer ``id`` → source_id is str(id)."""
        result = _posting_to_job(_minimal_posting(id=987654321), _SLUG)
        assert result["source_id"] == "987654321"

    def test_source_id_none_when_id_absent(self):
        """Missing ``id`` → source_id is None (not empty string)."""
        posting = _minimal_posting()
        del posting["id"]
        result = _posting_to_job(posting, _SLUG)
        assert result["source_id"] is None

    def test_source_id_none_when_id_is_none(self):
        """Explicit ``id: null`` → source_id is None."""
        result = _posting_to_job(_minimal_posting(id=None), _SLUG)
        assert result["source_id"] is None

    def test_source_id_key_always_present_in_result(self):
        """``source_id`` key must be present in the returned dict regardless."""
        result = _posting_to_job(_minimal_posting(), _SLUG)
        assert "source_id" in result


# ---------------------------------------------------------------------------
# posted_date extraction (cheap incidental fix alongside source_id)
# ---------------------------------------------------------------------------


class TestPostedDate:
    """posted_date extracted from publishedAt (ISO-8601 string)."""

    def test_posted_date_from_published_at(self):
        """publishedAt present → posted_date matches it exactly."""
        result = _posting_to_job(_minimal_posting(publishedAt="2024-03-15T09:00:00.000Z"), _SLUG)
        assert result["posted_date"] == "2024-03-15T09:00:00.000Z"

    def test_posted_date_none_when_published_at_absent(self):
        """Missing publishedAt → posted_date is None."""
        posting = _minimal_posting()
        del posting["publishedAt"]
        result = _posting_to_job(posting, _SLUG)
        assert result["posted_date"] is None

    def test_posted_date_none_when_published_at_is_none(self):
        """Explicit ``publishedAt: null`` → posted_date is None."""
        result = _posting_to_job(_minimal_posting(publishedAt=None), _SLUG)
        assert result["posted_date"] is None

    def test_posted_date_none_when_published_at_is_empty_string(self):
        """Empty publishedAt string → posted_date is None (falsy guard)."""
        result = _posting_to_job(_minimal_posting(publishedAt=""), _SLUG)
        assert result["posted_date"] is None

    def test_posted_date_key_always_present_in_result(self):
        """``posted_date`` key must be present in the returned dict regardless."""
        result = _posting_to_job(_minimal_posting(), _SLUG)
        assert "posted_date" in result


# ---------------------------------------------------------------------------
# Regression: existing fields still intact
# ---------------------------------------------------------------------------


class TestRegressionExistingFields:
    """Existing fields must not regress after adding source_id / posted_date."""

    def test_source_url_still_populated(self):
        """source_url still comes from jobUrl."""
        result = _posting_to_job(
            _minimal_posting(jobUrl="https://jobs.ashbyhq.com/Acme/xyz"), _SLUG
        )
        assert result["source_url"] == "https://jobs.ashbyhq.com/Acme/xyz"

    def test_locations_structured_still_emitted(self):
        """locations_structured is still present and is a list."""
        result = _posting_to_job(_minimal_posting(), _SLUG)
        assert "locations_structured" in result
        assert isinstance(result["locations_structured"], list)

    def test_company_source_unchanged(self):
        """company_source is still 'Ashby'."""
        result = _posting_to_job(_minimal_posting(), _SLUG)
        assert result["company_source"] == "Ashby"

    def test_title_unchanged(self):
        """title is still extracted from the posting."""
        result = _posting_to_job(_minimal_posting(title="Staff Engineer"), _SLUG)
        assert result["title"] == "Staff Engineer"

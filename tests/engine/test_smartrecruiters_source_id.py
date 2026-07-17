"""Tests for SmartRecruiters source_id extraction.

Phase 48.05 — SmartRecruiters source_id extraction.
F-04: source_id was missing on 100% of SmartRecruiters rows.

The SmartRecruiters v1 list-endpoint returns ``id`` as the unique posting
identifier. ``_posting_to_job`` must emit it as ``source_id`` (string).
``posted_date`` is also extracted from ``releasedDate`` (first publication only; #360)
as a cheap incidental fix.
"""

from __future__ import annotations

from unittest.mock import patch

from jobcannon.engine.ats_platforms._platforms_smartrecruiters import _posting_to_job

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_posting(**kwargs) -> dict:
    """Return a minimal SR posting fixture; caller may override any field."""
    base = {
        "id": "744000115714244",
        "name": "Staff Data Scientist",
        "location": {"city": "Austin", "region": "TX", "country": "US"},
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# source_id extraction (acceptance criteria for Phase 48.05)
# ---------------------------------------------------------------------------


@patch("jobcannon.engine.ats_platforms._fetch_smartrecruiters_description", return_value="")
class TestSourceId:
    """source_id must be populated from the ``id`` field."""

    def test_source_id_string_from_id(self, _mock_desc):
        """String ``id`` → source_id is that string."""
        result = _posting_to_job(_minimal_posting(id="744000115714244"), "LinkedIn3")
        assert result["source_id"] == "744000115714244"

    def test_source_id_integer_id_coerced_to_string(self, _mock_desc):
        """Integer ``id`` → source_id is str(id)."""
        result = _posting_to_job(_minimal_posting(id=123456789), "Acme")
        assert result["source_id"] == "123456789"

    def test_source_id_none_when_id_absent(self, _mock_desc):
        """Missing ``id`` → source_id is None (not empty string)."""
        posting = _minimal_posting()
        del posting["id"]
        result = _posting_to_job(posting, "Acme")
        assert result["source_id"] is None

    def test_source_id_none_when_id_is_none(self, _mock_desc):
        """Explicit ``id: null`` → source_id is None."""
        result = _posting_to_job(_minimal_posting(id=None), "Acme")
        assert result["source_id"] is None

    def test_source_id_none_when_id_is_empty_string(self, _mock_desc):
        """Empty-string ``id`` → source_id is None (falsy guard)."""
        result = _posting_to_job(_minimal_posting(id=""), "Acme")
        assert result["source_id"] is None

    def test_source_id_key_always_present_in_result(self, _mock_desc):
        """``source_id`` key must be present in the returned dict regardless."""
        result = _posting_to_job(_minimal_posting(), "Acme")
        assert "source_id" in result


# ---------------------------------------------------------------------------
# posted_date extraction (cheap incidental fix alongside source_id)
# ---------------------------------------------------------------------------


@patch("jobcannon.engine.ats_platforms._fetch_smartrecruiters_description", return_value="")
class TestPostedDate:
    """posted_date extracted from releasedDate only (#360)."""

    def test_posted_date_from_released_date(self, _mock_desc):
        """releasedDate present → posted_date matches it."""
        posting = _minimal_posting(releasedDate="2024-03-15T09:00:00.000Z")
        result = _posting_to_job(posting, "Acme")
        assert result["posted_date"] == "2024-03-15T09:00:00.000Z"

    def test_posting_status_updated_on_is_ignored(self, _mock_desc):
        """postingStatusUpdatedOn is last-status-change, never used (#360)."""
        posting = _minimal_posting(postingStatusUpdatedOn="2024-02-01T12:00:00.000Z")
        result = _posting_to_job(posting, "Acme")
        assert result["posted_date"] is None

    def test_released_date_wins_when_both_present(self, _mock_desc):
        """releasedDate wins over postingStatusUpdatedOn when both present."""
        posting = _minimal_posting(
            releasedDate="2024-03-20T00:00:00.000Z",
            postingStatusUpdatedOn="2024-01-01T00:00:00.000Z",
        )
        result = _posting_to_job(posting, "Acme")
        assert result["posted_date"] == "2024-03-20T00:00:00.000Z"

    def test_posted_date_none_when_released_date_absent(self, _mock_desc):
        """No releasedDate → posted_date is None."""
        result = _posting_to_job(_minimal_posting(), "Acme")
        assert result["posted_date"] is None

    def test_posted_date_key_always_present_in_result(self, _mock_desc):
        """``posted_date`` key is always present in returned dict."""
        result = _posting_to_job(_minimal_posting(), "Acme")
        assert "posted_date" in result


# ---------------------------------------------------------------------------
# Regression: source_url and existing fields still intact
# ---------------------------------------------------------------------------


@patch("jobcannon.engine.ats_platforms._fetch_smartrecruiters_description", return_value="")
class TestRegressionExistingFields:
    """Existing fields must not regress after adding source_id / posted_date."""

    def test_source_url_still_built_from_id(self, _mock_desc):
        """source_url still encodes the posting id."""
        result = _posting_to_job(_minimal_posting(id="abc-999"), "MyCompany")
        assert "MyCompany" in result["source_url"]
        assert "abc-999" in result["source_url"]

    def test_source_url_empty_when_id_absent(self, _mock_desc):
        """source_url is empty string when id is absent."""
        posting = _minimal_posting()
        del posting["id"]
        result = _posting_to_job(posting, "MyCompany")
        assert result["source_url"] == ""

    def test_locations_structured_still_emitted(self, _mock_desc):
        """locations_structured is still present and non-empty for normal postings."""
        result = _posting_to_job(_minimal_posting(), "Acme")
        assert "locations_structured" in result
        assert isinstance(result["locations_structured"], list)
        assert len(result["locations_structured"]) == 1

    def test_company_source_unchanged(self, _mock_desc):
        """company_source is still 'SmartRecruiters'."""
        result = _posting_to_job(_minimal_posting(), "Acme")
        assert result["company_source"] == "SmartRecruiters"

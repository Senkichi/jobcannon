"""Tests for Lever source_id and posted_date extraction.

Phase 48.06 — Lever source_id extraction.
F-04: source_id was missing on 98.6% of Lever rows.

The Lever v0 posting API returns ``id`` as the unique posting identifier.
``_posting_to_job`` must emit it as ``source_id`` (string).
``posted_date`` is extracted from ``createdAt`` (epoch-milliseconds integer)
and converted to an ISO-8601 string.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._platforms_lever import _posting_to_job

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLUG = "acme"

# 1_618_003_200_000 ms = 2021-04-09T21:20:00+00:00 UTC
_EPOCH_MS = 1_618_003_200_000
_EPOCH_ISO = "2021-04-09T21:20:00+00:00"


def _minimal_posting(**kwargs) -> dict:
    """Return a minimal Lever posting fixture; caller may override any field."""
    base: dict = {
        "id": "0396117a-5f97-463e-9d4e-03eddcc4a2e5",
        "text": "Account Executive",
        "categories": {"location": "San Francisco, CA"},
        "hostedUrl": "https://jobs.lever.co/acme/0396117a-5f97-463e-9d4e-03eddcc4a2e5",
        "createdAt": _EPOCH_MS,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# source_id extraction (acceptance criteria for Phase 48.06)
# ---------------------------------------------------------------------------


class TestSourceId:
    """source_id must be populated from the ``id`` field."""

    def test_source_id_uuid_string_from_id(self):
        """UUID string ``id`` → source_id is that string."""
        result = _posting_to_job(_minimal_posting(id="0396117a-5f97-463e-9d4e-03eddcc4a2e5"), _SLUG)
        assert result["source_id"] == "0396117a-5f97-463e-9d4e-03eddcc4a2e5"

    def test_source_id_integer_id_coerced_to_string(self):
        """Integer ``id`` → source_id is str(id)."""
        result = _posting_to_job(_minimal_posting(id=123456789), _SLUG)
        assert result["source_id"] == "123456789"

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
    """posted_date extracted from createdAt (epoch-ms) → ISO-8601 string."""

    def test_posted_date_iso_string_from_epoch_ms(self):
        """Epoch-ms integer → ISO-8601 string with UTC offset."""
        result = _posting_to_job(_minimal_posting(createdAt=_EPOCH_MS), _SLUG)
        assert result["posted_date"] == _EPOCH_ISO

    def test_posted_date_none_when_created_at_absent(self):
        """Missing createdAt → posted_date is None."""
        posting = _minimal_posting()
        del posting["createdAt"]
        result = _posting_to_job(posting, _SLUG)
        assert result["posted_date"] is None

    def test_posted_date_none_when_created_at_is_none(self):
        """Explicit ``createdAt: null`` → posted_date is None."""
        result = _posting_to_job(_minimal_posting(createdAt=None), _SLUG)
        assert result["posted_date"] is None

    def test_posted_date_contains_year(self):
        """Sanity check: the ISO string encodes the correct year."""
        result = _posting_to_job(_minimal_posting(createdAt=_EPOCH_MS), _SLUG)
        assert result["posted_date"] is not None
        assert "2021" in result["posted_date"]

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
        """source_url still comes from hostedUrl."""
        result = _posting_to_job(
            _minimal_posting(hostedUrl="https://jobs.lever.co/acme/abc"), _SLUG
        )
        assert result["source_url"] == "https://jobs.lever.co/acme/abc"

    def test_locations_structured_still_emitted(self):
        """locations_structured is still present and is a list."""
        result = _posting_to_job(_minimal_posting(), _SLUG)
        assert "locations_structured" in result
        assert isinstance(result["locations_structured"], list)

    def test_company_source_unchanged(self):
        """company_source is still 'Lever'."""
        result = _posting_to_job(_minimal_posting(), _SLUG)
        assert result["company_source"] == "Lever"

    def test_title_from_text_field(self):
        """Lever uses 'text' for title — still extracted correctly."""
        result = _posting_to_job(_minimal_posting(text="Senior Backend Engineer"), _SLUG)
        assert result["title"] == "Senior Backend Engineer"

"""Layer-1 emission tests for the Workday scanner (Phase 48.02).

Verifies that ``_posting_to_job`` extracts ``source_id``, ``posted_date``,
and ``locations_structured`` (``JobLocation``) directly from the Workday CXS
API response — not derived via the Layer-2 ``parse_locations`` heuristic.

Test coverage:
  - Happy-path: standard posting with ``externalPath``, ``postedOn``, and a
    parseable ``locationsText`` → all three fields populated.
  - Missing ``postedOn`` → ``posted_date`` is ``None`` (graceful).
  - Remote-only ``locationsText`` → ``JobLocation.workplace_type == "REMOTE"``.
  - Hybrid prefix ``locationsText`` → ``JobLocation.workplace_type == "HYBRID"``.
  - Multi-location ``locationsText`` (``"New York, NY; Remote"``) → two
    ``JobLocation`` entries.
  - Empty ``externalPath`` → ``source_id`` is ``None``.
  - Unknown ``postedOn`` format → ``posted_date`` is ``None``.

The detail-fetch side-effect (``_fetch_workday_description``) is patched to
return an empty string for all tests so they stay hermetic.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_SLUG = "walmart.wd5/WalmartExternal"

# A minimal base posting dict with all the private slug keys that
# _fetch_postings stashes on each item.  Tests extend / override this.
_BASE_POSTING: dict = {
    "title": "Senior Data Scientist",
    "locationsText": "Sunnyvale, CA",
    "externalPath": "/job/Senior-Data-Scientist_R-12345",
    "postedOn": "2024-01-15",
    "__workday_subdomain": "walmart.wd5",
    "__workday_tenant": "walmart",
    "__workday_board": "WalmartExternal",
}


def _call_posting_to_job(posting: dict) -> dict:
    """Invoke ``_posting_to_job`` with the description fetch mocked out."""
    from jobcannon.engine.ats_platforms._platforms_workday import _posting_to_job

    with patch(
        "jobcannon.engine.ats_platforms._fetch_workday_description",
        return_value="",
    ):
        return _posting_to_job(posting, _SLUG)


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


class TestWorkdayLayer1HappyPath:
    """Standard posting → all three Layer-1 fields populated."""

    def test_source_id_is_external_path(self):
        """source_id is the externalPath string (unique per posting per board)."""
        result = _call_posting_to_job(dict(_BASE_POSTING))
        assert result["source_id"] == "/job/Senior-Data-Scientist_R-12345"

    def test_posted_date_iso_format(self):
        """postedOn in YYYY-MM-DD format is parsed to a datetime."""
        result = _call_posting_to_job(dict(_BASE_POSTING))
        assert isinstance(result["posted_date"], datetime)
        assert result["posted_date"] == datetime(2024, 1, 15)

    def test_posted_date_us_format(self):
        """postedOn in MM/DD/YYYY format is parsed to a datetime."""
        posting = {**_BASE_POSTING, "postedOn": "01/15/2024"}
        result = _call_posting_to_job(posting)
        assert isinstance(result["posted_date"], datetime)
        assert result["posted_date"] == datetime(2024, 1, 15)

    def test_locations_structured_non_empty(self):
        """locations_structured is a non-empty list for a parseable locationsText."""
        result = _call_posting_to_job(dict(_BASE_POSTING))
        locs = result["locations_structured"]
        assert isinstance(locs, list)
        assert len(locs) >= 1

    def test_locations_structured_city_and_region(self):
        """Parsed US 'City, ST' location carries city, region_code, country_code."""
        result = _call_posting_to_job(dict(_BASE_POSTING))
        loc = result["locations_structured"][0]
        assert loc.city == "Sunnyvale"
        assert loc.region_code == "CA"
        assert loc.country_code == "US"
        assert loc.unresolved is False

    def test_locations_structured_workplace_type_unspecified_for_plain_city(self):
        """Plain 'City, ST' without remote/hybrid keywords → UNSPECIFIED."""
        result = _call_posting_to_job(dict(_BASE_POSTING))
        loc = result["locations_structured"][0]
        assert loc.workplace_type == "UNSPECIFIED"

    def test_raw_field_preserved(self):
        """JobLocation.raw equals the original locationsText segment."""
        result = _call_posting_to_job(dict(_BASE_POSTING))
        loc = result["locations_structured"][0]
        assert loc.raw == "Sunnyvale, CA"


# ---------------------------------------------------------------------------
# Edge cases — posted_date
# ---------------------------------------------------------------------------


class TestWorkdayPostedDate:
    def test_relative_string_variants(self):
        """The full relative-format table parses at 'approximate' precision."""
        from datetime import UTC, datetime, timedelta

        from jobcannon.engine.ats_platforms._platforms_workday import _parse_posted_date

        utc_today = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        cases = {
            "Posted Today": 0,
            "posted today": 0,
            "Posted Yesterday": 1,
            "Posted 1 Day Ago": 1,
            "Posted 12 Days Ago": 12,
            "Posted 30+ Days Ago": 30,  # lossy floor
            "30+ days ago": 30,  # no 'Posted' prefix
        }
        for text, days in cases.items():
            dt, precision = _parse_posted_date(text)
            assert dt == utc_today - timedelta(days=days), text
            assert precision == "approximate", text

    def test_missing_posted_on_returns_none(self):
        """When postedOn is absent, posted_date is None (not a synthesis)."""
        posting = {k: v for k, v in _BASE_POSTING.items() if k != "postedOn"}
        result = _call_posting_to_job(posting)
        assert result["posted_date"] is None

    def test_empty_posted_on_returns_none(self):
        """Empty string postedOn → posted_date is None."""
        posting = {**_BASE_POSTING, "postedOn": ""}
        result = _call_posting_to_job(posting)
        assert result["posted_date"] is None

    def test_unrecognised_format_returns_none(self):
        """Garbage postedOn strings → posted_date is None."""
        posting = {**_BASE_POSTING, "postedOn": "sometime last quarter"}
        result = _call_posting_to_job(posting)
        assert result["posted_date"] is None
        assert result["posted_date_precision"] is None

    def test_relative_string_yields_approximate_date(self):
        """'Posted 3 Days Ago' → UTC-today − 3d at date precision."""
        from datetime import UTC, datetime, timedelta

        posting = {**_BASE_POSTING, "postedOn": "Posted 3 Days Ago"}
        result = _call_posting_to_job(posting)
        expected = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        ) - timedelta(days=3)
        assert result["posted_date"] == expected
        assert result["posted_date_precision"] == "approximate"

    def test_absolute_date_is_exact(self):
        """Absolute postedOn dates carry 'exact' precision."""
        result = _call_posting_to_job(dict(_BASE_POSTING))
        assert result["posted_date_precision"] == "exact"


# ---------------------------------------------------------------------------
# Edge cases — source_id
# ---------------------------------------------------------------------------


class TestWorkdaySourceId:
    def test_empty_external_path_gives_none_source_id(self):
        """When externalPath is empty, source_id is None."""
        posting = {**_BASE_POSTING, "externalPath": ""}
        result = _call_posting_to_job(posting)
        assert result["source_id"] is None

    def test_external_path_without_leading_slash(self):
        """externalPath without leading slash is still used as-is."""
        posting = {**_BASE_POSTING, "externalPath": "job/Title_R-99"}
        result = _call_posting_to_job(posting)
        assert result["source_id"] == "job/Title_R-99"


# ---------------------------------------------------------------------------
# Edge cases — workplace type detection
# ---------------------------------------------------------------------------


class TestWorkdayWorkplaceType:
    def test_remote_only_locations_text(self):
        """Pure 'Remote' locationsText → JobLocation with workplace_type REMOTE."""
        posting = {**_BASE_POSTING, "locationsText": "Remote"}
        result = _call_posting_to_job(posting)
        locs = result["locations_structured"]
        assert len(locs) == 1
        assert locs[0].workplace_type == "REMOTE"

    def test_hybrid_prefix_in_locations_text(self):
        """'Hybrid - San Francisco, CA' → HYBRID workplace_type, city extracted."""
        posting = {**_BASE_POSTING, "locationsText": "Hybrid - San Francisco, CA"}
        result = _call_posting_to_job(posting)
        locs = result["locations_structured"]
        assert len(locs) == 1
        assert locs[0].workplace_type == "HYBRID"
        assert locs[0].city == "San Francisco"
        assert locs[0].region_code == "CA"
        assert locs[0].unresolved is False

    def test_remote_keyword_in_city_locations_text(self):
        """'New York, NY; Remote' → two locations: one city, one remote."""
        posting = {**_BASE_POSTING, "locationsText": "New York, NY; Remote"}
        result = _call_posting_to_job(posting)
        locs = result["locations_structured"]
        assert len(locs) == 2
        workplace_types = {loc.workplace_type for loc in locs}
        assert "REMOTE" in workplace_types
        cities = [loc.city for loc in locs if loc.city]
        assert "New York" in cities

    def test_empty_locations_text_gives_empty_list(self):
        """Empty locationsText → empty locations_structured list."""
        posting = {**_BASE_POSTING, "locationsText": ""}
        result = _call_posting_to_job(posting)
        assert result["locations_structured"] == []

    def test_locations_structured_raw_field_is_set(self):
        """locations_structured items carry a non-empty raw field."""
        posting = {**_BASE_POSTING, "locationsText": "Austin, TX"}
        result = _call_posting_to_job(posting)
        locs = result["locations_structured"]
        assert len(locs) == 1
        assert locs[0].raw == "Austin, TX"

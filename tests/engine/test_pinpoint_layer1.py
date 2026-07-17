"""Tests for Pinpoint Layer-1 emission: source_id and JobLocation.

Phase 48.04 — Pinpoint Layer-1: emit JobLocation + source_id.

Pinpoint exposes a structured ``location`` dict with ``city``, ``province``,
and ``name`` (country) fields, so ``JobLocation`` is constructed directly from
the API response — no Layer-2 heuristic parser is needed.

Coverage:
  - ``source_id`` extracted from the posting's ``id`` field (str or int).
  - ``locations_structured`` is a list of ``JobLocation`` instances.
  - ``JobLocation.city``, ``.region`` (from ``province``), and ``.country``
    (from ``name``) are populated correctly.
  - ``unresolved=False`` for a fully-structured Pinpoint location.
  - Graceful handling of absent / None / all-blank location dicts.
  - ``_to_canonical`` unit tests (pure function, no HTTP).
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._platforms_pinpoint import (
    _posting_to_job,
    _to_canonical,
)
from jobcannon.engine.location_canonical import JobLocation

_SLUG = "acme"

# ---------------------------------------------------------------------------
# Base fixture — minimal Pinpoint API posting shape
# ---------------------------------------------------------------------------

_BASE_POSTING: dict = {
    "id": "abc-123",
    "title": "Software Engineer",
    "url": "https://acme.pinpointhq.com/postings/abc-123",
    "location": {
        "city": "Toronto",
        "province": "Ontario",
        "name": "Canada",
    },
    "workplace_type": "onsite",
    "description": "<p>Build great things.</p>",
    "compensation_minimum": 90_000,
    "compensation_maximum": 120_000,
}


# ---------------------------------------------------------------------------
# source_id (F-04)
# ---------------------------------------------------------------------------


class TestSourceId:
    def test_source_id_extracted_from_string_id(self):
        """String id → source_id preserved as string."""
        result = _posting_to_job(dict(_BASE_POSTING), _SLUG)
        assert result["source_id"] == "abc-123"

    def test_source_id_integer_id_converted_to_string(self):
        """Numeric id → source_id string."""
        posting = {**_BASE_POSTING, "id": 99999}
        result = _posting_to_job(posting, _SLUG)
        assert result["source_id"] == "99999"

    def test_source_id_none_when_id_absent(self):
        """Missing id key → source_id is None (not empty string)."""
        posting = {k: v for k, v in _BASE_POSTING.items() if k != "id"}
        result = _posting_to_job(posting, _SLUG)
        assert result["source_id"] is None

    def test_source_id_none_when_id_is_none(self):
        """Explicit id=None → source_id is None."""
        posting = {**_BASE_POSTING, "id": None}
        result = _posting_to_job(posting, _SLUG)
        assert result["source_id"] is None


# ---------------------------------------------------------------------------
# JobLocation / locations_structured emission
# ---------------------------------------------------------------------------


class TestJobLocationEmission:
    def test_locations_structured_key_present(self):
        """_posting_to_job result must include locations_structured."""
        result = _posting_to_job(dict(_BASE_POSTING), _SLUG)
        assert "locations_structured" in result

    def test_locations_structured_is_list(self):
        result = _posting_to_job(dict(_BASE_POSTING), _SLUG)
        assert isinstance(result["locations_structured"], list)

    def test_locations_structured_non_empty_for_full_location(self):
        result = _posting_to_job(dict(_BASE_POSTING), _SLUG)
        assert len(result["locations_structured"]) >= 1

    def test_joblocation_is_correct_type(self):
        result = _posting_to_job(dict(_BASE_POSTING), _SLUG)
        loc = result["locations_structured"][0]
        assert isinstance(loc, JobLocation)

    def test_joblocation_city_populated(self):
        result = _posting_to_job(dict(_BASE_POSTING), _SLUG)
        loc = result["locations_structured"][0]
        assert loc.city == "Toronto"

    def test_joblocation_region_from_province(self):
        """province field → region field of JobLocation."""
        result = _posting_to_job(dict(_BASE_POSTING), _SLUG)
        loc = result["locations_structured"][0]
        assert loc.region == "Ontario"

    def test_joblocation_country_from_name(self):
        """name field (country name) → country field of JobLocation."""
        result = _posting_to_job(dict(_BASE_POSTING), _SLUG)
        loc = result["locations_structured"][0]
        assert loc.country == "Canada"

    def test_joblocation_unresolved_false(self):
        """Structured Pinpoint location → unresolved=False."""
        result = _posting_to_job(dict(_BASE_POSTING), _SLUG)
        loc = result["locations_structured"][0]
        assert loc.unresolved is False

    def test_locations_structured_empty_when_location_key_absent(self):
        """Missing location key → empty list."""
        posting = {k: v for k, v in _BASE_POSTING.items() if k != "location"}
        result = _posting_to_job(posting, _SLUG)
        assert result["locations_structured"] == []

    def test_locations_structured_empty_when_location_is_none(self):
        posting = {**_BASE_POSTING, "location": None}
        result = _posting_to_job(posting, _SLUG)
        assert result["locations_structured"] == []

    def test_locations_structured_empty_when_all_fields_blank(self):
        """All sub-fields blank → empty list (no empty JobLocation emitted)."""
        posting = {**_BASE_POSTING, "location": {"city": "", "province": "", "name": ""}}
        result = _posting_to_job(posting, _SLUG)
        assert result["locations_structured"] == []


# ---------------------------------------------------------------------------
# _to_canonical unit tests (pure, no HTTP)
# ---------------------------------------------------------------------------


class TestToCanonical:
    def test_standard_location_produces_single_entry(self):
        posting = {
            "location": {"city": "Vancouver", "province": "British Columbia", "name": "Canada"},
        }
        locs = _to_canonical(posting)
        assert len(locs) == 1

    def test_city_region_country_all_mapped(self):
        posting = {
            "location": {"city": "Vancouver", "province": "British Columbia", "name": "Canada"},
        }
        loc = _to_canonical(posting)[0]
        assert loc.city == "Vancouver"
        assert loc.region == "British Columbia"
        assert loc.country == "Canada"

    def test_unresolved_false_for_full_location(self):
        posting = {
            "location": {"city": "Vancouver", "province": "British Columbia", "name": "Canada"},
        }
        loc = _to_canonical(posting)[0]
        assert loc.unresolved is False

    def test_region_code_is_none(self):
        """Pinpoint does not provide ISO 3166-2 codes → region_code is None."""
        posting = {
            "location": {"city": "Austin", "province": "Texas", "name": "United States"},
        }
        loc = _to_canonical(posting)[0]
        assert loc.region_code is None

    def test_country_code_is_none(self):
        """Pinpoint does not provide ISO 3166-1 codes → country_code is None."""
        posting = {
            "location": {"city": "Austin", "province": "Texas", "name": "United States"},
        }
        loc = _to_canonical(posting)[0]
        assert loc.country_code is None

    def test_missing_location_returns_empty(self):
        assert _to_canonical({}) == []

    def test_none_location_returns_empty(self):
        assert _to_canonical({"location": None}) == []

    def test_non_dict_location_returns_empty(self):
        assert _to_canonical({"location": "Toronto, ON"}) == []

    def test_all_blank_fields_returns_empty(self):
        posting = {"location": {"city": "", "province": "", "name": ""}}
        assert _to_canonical(posting) == []

    def test_city_only_produces_entry(self):
        """A posting with only city set still yields a JobLocation."""
        posting = {"location": {"city": "Austin", "province": "", "name": ""}}
        locs = _to_canonical(posting)
        assert len(locs) == 1
        assert locs[0].city == "Austin"
        assert locs[0].region is None
        assert locs[0].country is None

    def test_raw_combines_available_parts(self):
        """raw field joins non-empty parts with ', '."""
        posting = {
            "location": {"city": "Toronto", "province": "Ontario", "name": "Canada"},
        }
        loc = _to_canonical(posting)[0]
        assert loc.raw == "Toronto, Ontario, Canada"

    def test_workplace_type_remote(self):
        """Pinpoint workplace_type=remote → JobLocation.workplace_type=REMOTE."""
        posting = {
            "location": {"city": "Toronto", "province": "Ontario", "name": "Canada"},
            "workplace_type": "remote",
        }
        loc = _to_canonical(posting)[0]
        assert loc.workplace_type == "REMOTE"

    def test_workplace_type_defaults_to_unspecified(self):
        """No workplace_type field → UNSPECIFIED."""
        posting = {
            "location": {"city": "Toronto", "province": "Ontario", "name": "Canada"},
        }
        loc = _to_canonical(posting)[0]
        assert loc.workplace_type == "UNSPECIFIED"

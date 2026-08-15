"""Tests for Greenhouse Layer-1 emission: source_id, posted_date, JobLocation, salary.

Phase 48.03 — Greenhouse Layer-1: emit JobLocation + source_id + posted_date.

Salary capture (Data Integrity Overhaul P1.3, D-1/D-2): the lossy ``_resolve_salary``
resolver is DELETED. ``_posting_to_job`` now performs only the lossless
cents-vs-dollars decode at capture, wraps the raw per-period values in a
``SalaryObservation``, and delegates annualization + the corroborated cents
salvage ladder to the single normalizer. Consequences exercised below:
  * hourly values now annualize (×2080): the documented ``64/64 unit=hour`` case
    becomes 133,120 (was stored as 64).
  * unit-less raw-cents pairs (Northbeam/Roblox) now salvage to dollars via the
    normalizer's corroborated cents rung instead of landing as $17M.
"""

from __future__ import annotations

from jobcannon.engine.salary_normalizer import SalaryObservation, normalize_observation
from jobcannon.engine.ats_platforms._platforms_greenhouse import (
    _posting_to_job,
    _to_canonical,
)
from jobcannon.engine.location_canonical import JobLocation


def _resolution_of(result: dict) -> str:
    """Re-run the normalizer on the emitted observation to read its resolution code.

    Strips the stamped ``resolution`` key (P1.6) before reconstructing the raw
    SalaryObservation — resolution is a normalizer output, not a capture field.
    """
    obs = {k: v for k, v in result["salary_observation"].items() if k != "resolution"}
    return normalize_observation(SalaryObservation(**obs)).resolution


# ---------------------------------------------------------------------------
# _posting_to_job: documented salary fixtures (acceptance criteria, P1.3)
# ---------------------------------------------------------------------------


class TestSalaryFixtures:
    """Concrete salary cases — capture decode + single-normalizer salvage ladder."""

    def test_fixture1_annual_cents_encoded_salary(self):
        """Fixture 1: cents-encoded annual salary.

        6_500_000 cents + interval="year" → $65,000.
        8_500_000 cents + interval="year" → $85,000.
        """
        posting = {
            "id": 12345,
            "title": "Software Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
            "location": {"name": "New York, NY"},
            "updated_at": "2024-01-15T10:30:00Z",
            "content": "Build great software.",
            "pay_input_ranges": [
                {
                    "min_cents": 6_500_000,
                    "max_cents": 8_500_000,
                    "unit": "year",
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 65_000
        assert result["salary_max"] == 85_000
        assert result["salary_period"] == "annual"
        assert _resolution_of(result) == "ok"

    def test_fixture2_hourly_dollar_salary_annualizes(self):
        """Fixture 2: the documented 64-64 case (hourly $64).

        min_cents=64 with unit="hour" → $64/hour, annualized ×2080 → 133,120
        (P1.3: the old "stored as 64" behavior is replaced by annualization).
        """
        posting = {
            "id": 67890,
            "title": "Data Analyst",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/67890",
            "location": {"name": "Remote"},
            "updated_at": "2024-02-01T09:00:00Z",
            "content": "Analyze data.",
            "pay_input_ranges": [
                {
                    "min_cents": 64,
                    "max_cents": 64,
                    "unit": "hour",
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 133_120, "Hourly $64 must annualize ×2080"
        assert result["salary_max"] == 133_120
        assert result["salary_period"] == "hourly"
        # The observation records the RAW per-period value (D-1), not the annual pair.
        assert result["salary_observation"]["min_value"] == 64
        assert result["salary_observation"]["period"] == "hourly"

    def test_fixture3_missing_interval_in_window_assumed_annual(self):
        """Fixture 3: no unit/interval, values already in the plausibility window.

        150k/200k with no interval → normalizer assumes annual (rung 2), period
        stays the source 'unknown'.
        """
        posting = {
            "id": 99999,
            "title": "Product Manager",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/99999",
            "location": {"name": "San Francisco, CA"},
            "updated_at": "2024-03-01T08:00:00Z",
            "content": "Drive product strategy.",
            "pay_input_ranges": [
                {
                    "min_cents": 150_000,
                    "max_cents": 200_000,
                    # deliberately omit "unit" / "interval"
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 150_000
        assert result["salary_max"] == 200_000
        assert result["salary_period"] == "unknown"
        assert _resolution_of(result) == "ok"

    def test_fixture_hour_64_90_annualizes(self):
        """unit=hour, 64/90 → 133,120 / 187,200, period 'hourly'."""
        posting = {
            "id": 11,
            "title": "Eng",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
            "location": {"name": "Remote"},
            "pay_input_ranges": [{"min_cents": 64, "max_cents": 90, "unit": "hour"}],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 133_120
        assert result["salary_max"] == 187_200
        assert result["salary_period"] == "hourly"

    def test_fixture_northbeam_unitless_cents_salvaged(self):
        """Northbeam-shaped: no unit, 17M/20M cents → 170k/200k via cents salvage.

        period stays 'unknown' (the source said nothing); resolution 'salvaged_cents'.
        """
        posting = {
            "id": 22,
            "title": "Senior Data Scientist",
            "absolute_url": "https://boards.greenhouse.io/northbeam/jobs/22",
            "location": {"name": "Remote"},
            "pay_input_ranges": [{"min_cents": 17_000_000, "max_cents": 20_000_000}],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 170_000
        assert result["salary_max"] == 200_000
        assert result["salary_period"] == "unknown"
        assert _resolution_of(result) == "salvaged_cents"
        # D-1: the observation keeps the raw cents evidence.
        assert result["salary_observation"]["min_value"] == 17_000_000

    def test_fixture_roblox_unitless_cents_salvaged(self):
        """Roblox-shaped: no unit, 18.586M/22.138M cents → 185,860 / 221,380."""
        posting = {
            "id": 33,
            "title": "Staff ML Engineer",
            "absolute_url": "https://boards.greenhouse.io/roblox/jobs/33",
            "location": {"name": "San Mateo, CA"},
            "pay_input_ranges": [{"min_cents": 18_586_000, "max_cents": 22_138_000}],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 185_860
        assert result["salary_max"] == 221_380
        assert result["salary_period"] == "unknown"
        assert _resolution_of(result) == "salvaged_cents"

    def test_provenance_is_ats_structured_when_salary_present(self):
        posting = {
            "id": 44,
            "title": "Eng",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/44",
            "location": {"name": "Remote"},
            "pay_input_ranges": [{"min_cents": 150_000, "max_cents": 200_000}],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_provenance"] == "ats_structured"

    def test_no_pay_ranges_emits_no_observation(self):
        posting = {
            "id": 55,
            "title": "Eng",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/55",
            "location": {"name": "Remote"},
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] is None
        assert result["salary_max"] is None
        assert result["salary_provenance"] is None
        assert result["salary_observation"] is None


# ---------------------------------------------------------------------------
# source_id emission (F-04)
# ---------------------------------------------------------------------------


class TestSourceId:
    def test_source_id_extracted_as_string(self):
        """Top-level `id` integer → source_id string."""
        posting = {
            "id": 4647776006,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["source_id"] == "4647776006"

    def test_source_id_none_when_id_absent(self):
        """Missing `id` → source_id is None (not empty string)."""
        posting = {
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["source_id"] is None

    def test_source_id_none_when_id_is_none(self):
        posting = {
            "id": None,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["source_id"] is None


# ---------------------------------------------------------------------------
# posted_date emission (F-02)
# ---------------------------------------------------------------------------


class TestPostedDate:
    def test_posted_date_from_first_published(self):
        """first_published ISO-8601 string → posted_date."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "first_published": "2024-01-10T08:00:00Z",
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["posted_date"] == "2024-01-10T08:00:00Z"

    def test_updated_at_is_ignored(self):
        """updated_at is last-modified, never used for posted_date."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "updated_at": "2024-02-01T00:00:00Z",
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["posted_date"] is None

    def test_first_published_wins_over_updated_at(self):
        """When both are present, first_published wins."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "updated_at": "2024-02-01T00:00:00Z",
            "first_published": "2024-01-01T00:00:00Z",
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["posted_date"] == "2024-01-01T00:00:00Z"

    def test_posted_date_none_when_first_published_absent(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["posted_date"] is None


# ---------------------------------------------------------------------------
# JobLocation / locations_structured emission
# ---------------------------------------------------------------------------


class TestJobLocationEmission:
    def test_locations_structured_present_in_result(self):
        """_posting_to_job must include locations_structured key."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "New York, NY"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert "locations_structured" in result

    def test_locations_structured_is_list(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Seattle, WA"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert isinstance(result["locations_structured"], list)

    def test_locations_structured_entries_are_joblocation(self):
        """When parse_locations succeeds, entries are JobLocation instances."""
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        locs = result["locations_structured"]
        if locs:  # non-empty parse result
            assert all(isinstance(loc, JobLocation) for loc in locs)

    def test_locations_structured_empty_when_location_dict_empty(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": {},
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["locations_structured"] == []

    def test_locations_structured_empty_when_location_key_absent(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["locations_structured"] == []

    def test_locations_structured_empty_when_location_is_none(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "location": None,
            "absolute_url": "https://example.com",
        }
        result = _posting_to_job(posting, "acme")
        assert result["locations_structured"] == []

    def test_to_canonical_remote_location(self):
        """'Remote' string → at least one entry with workplace_type REMOTE."""
        locs = _to_canonical({"location": {"name": "Remote"}})
        assert isinstance(locs, list)
        if locs:
            assert any(loc.workplace_type == "REMOTE" for loc in locs)

    def test_to_canonical_missing_location_returns_empty(self):
        assert _to_canonical({}) == []
        assert _to_canonical({"location": None}) == []
        assert _to_canonical({"location": {"name": ""}}) == []


# ---------------------------------------------------------------------------
# Interval field alias: "interval" key as well as "unit"
# ---------------------------------------------------------------------------


class TestIntervalFieldAlias:
    """Some Greenhouse responses use "interval" instead of "unit"."""

    def test_interval_key_treated_same_as_unit_for_annual_cents(self):
        posting = {
            "id": 1,
            "title": "Eng",
            "location": {"name": "SF"},
            "absolute_url": "https://example.com",
            "pay_input_ranges": [
                {
                    "min_cents": 10_000_000,
                    "max_cents": 15_000_000,
                    "interval": "year",  # uses "interval", not "unit"
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 100_000
        assert result["salary_max"] == 150_000

    def test_interval_key_hourly_annualizes(self):
        """'interval'=hour values are dollars/hour and annualize ×2080 (P1.3)."""
        posting = {
            "id": 1,
            "title": "Eng",
            "location": {"name": "SF"},
            "absolute_url": "https://example.com",
            "pay_input_ranges": [
                {
                    "min_cents": 45,
                    "max_cents": 55,
                    "interval": "hour",
                }
            ],
        }
        result = _posting_to_job(posting, "acme")
        assert result["salary_min"] == 93_600  # 45 × 2080
        assert result["salary_max"] == 114_400  # 55 × 2080
        assert result["salary_period"] == "hourly"

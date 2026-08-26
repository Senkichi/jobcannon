"""Tests for the Tesla Playwright scanner.

Covers the parser logic against a REAL cua-api JSON fixture (a trimmed slice of an
actual live ``cua-api/apps/careers/state`` capture — all ids/names are authentic):

1. Parser (``_platforms_tesla._parse_cua_api_response``): reads the abbreviated
   listing keys (``t``/``dp``/``l``/``y``) and resolves them to human-readable values
   via the ``lookup.{departments,locations,types}`` tables. Never leaves raw ids.
2. The test is fixture-driven (no network) so CI stays deterministic.

Live-fetch note: Tesla's ``cua-api`` returns 403 to plain HTTP requests (anti-bot),
but the SPA calls it same-origin from a REAL browser, so Playwright interception
reaches it (the fixture was captured that way). A live scan therefore needs a real
(non-bot-flagged) browser session; the parser itself is fully fixture-tested here.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _load_fixture() -> dict:
    fixture_path = Path(__file__).parent / "fixtures" / "tesla_cua_api_response.json"
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)


class TestTeslaFixtureShape:
    """Guard the fixture stays a REAL cua-api capture, not a fabricated stub.

    An earlier draft shipped an invented fixture with top-level ``locations``/
    ``regions`` maps and ``title``/``department`` listing keys — none of which the
    real API uses. These asserts fail loudly if the fixture ever regresses to that.
    """

    def test_fixture_uses_lookup_nesting_and_abbreviated_keys(self):
        api = _load_fixture()
        # Lookup tables are nested under `lookup`, NOT top-level.
        assert "lookup" in api
        assert "locations" not in api and "regions" not in api
        for table in ("departments", "locations", "types"):
            assert table in api["lookup"], f"lookup.{table} missing"
        # Listings use abbreviated keys.
        assert api["listings"], "fixture has no listings"
        first = api["listings"][0]
        for key in ("id", "t", "dp", "l", "y"):
            assert key in first, f"listing missing abbreviated key {key!r}"


class TestTeslaParser:
    def test_resolves_department_location_type_from_lookup(self):
        """Parser resolves dp/l/y via lookup.* to human-readable values."""
        from jobcannon.engine.ats_platforms._platforms_tesla import _parse_cua_api_response

        postings = _parse_cua_api_response(_load_fixture())
        by_id = {p["source_id"]: p for p in postings}

        # AI Engineer — fully resolvable across all three lookup tables.
        ai = by_id["224501"]
        assert ai["title"] == "AI Engineer, Manipulation, Optimus"
        assert ai["department"] == "Tesla AI"  # dp "5" -> lookup.departments
        assert ai["location"] == "Palo Alto, California"  # l "401022" -> lookup.locations
        assert ai["employment_type"] == "fulltime"  # y 1 -> lookup.types
        assert ai["source_url"] == "https://www.tesla.com/careers/search/job/224501"
        assert ai["source_id"] == "224501"

        # No posting should carry a raw numeric id as its location/department (the
        # exact failure the issue calls out: "don't leave raw IDs").
        for p in postings:
            assert not p["location"].strip().isdigit()
            assert not p["department"].strip().isdigit()

    def test_unresolved_location_is_empty_not_raw_id(self):
        """A listing whose location id is absent from lookup.locations -> ''."""
        from jobcannon.engine.ats_platforms._platforms_tesla import _parse_cua_api_response

        postings = _parse_cua_api_response(_load_fixture())
        # id 239069 references location id 31322, deliberately absent from the
        # trimmed lookup.locations table (real-world ~0.24% of ids don't resolve).
        unresolved = next(p for p in postings if p["source_id"] == "239069")
        assert unresolved["location"] == ""
        # Department still resolves (departments are 100% covered in practice).
        assert unresolved["department"] == "Sales & Customer Support"

    def test_reads_real_keys_not_fabricated_keys(self):
        """Parser reads t/dp/l/y from lookup.*, ignoring the fabricated contract.

        A listing carrying ONLY the old invented keys (title/department/location as
        top-level maps) must resolve to empty — proving the parser is bound to the
        real contract and can't silently pass on a fabricated payload again.
        """
        from jobcannon.engine.ats_platforms._platforms_tesla import _parse_cua_api_response

        fabricated = {
            "listings": [
                {"id": "1", "title": "Ghost", "department": "dept_x", "location": "loc_x"}
            ],
            # fabricated top-level maps that the real API never sends
            "departments": {"dept_x": "ShouldNotResolve"},
            "locations": {"loc_x": "ShouldNotResolve"},
        }
        postings = _parse_cua_api_response(fabricated)
        assert len(postings) == 1
        assert postings[0]["title"] == ""  # `t` absent -> empty, not `title`
        assert postings[0]["department"] == ""  # top-level `departments` ignored
        assert postings[0]["location"] == ""  # top-level `locations` ignored

    def test_real_contract_roundtrip_all_fields(self):
        """Every fixture listing resolves department + type; url/id are well-formed."""
        from jobcannon.engine.ats_platforms._platforms_tesla import _parse_cua_api_response

        postings = _parse_cua_api_response(_load_fixture())
        assert len(postings) == len(_load_fixture()["listings"])
        for p in postings:
            assert p["department"]  # 100% resolvable in practice
            assert p["employment_type"] in {"fulltime", "parttime", "intern", "seasonal"}
            assert p["source_url"].startswith("https://www.tesla.com/careers/search/job/")
            assert p["source_id"] and p["source_id"].isdigit()

    def test_empty_listings_returns_empty(self):
        from jobcannon.engine.ats_platforms._platforms_tesla import _parse_cua_api_response

        assert _parse_cua_api_response({"listings": [], "lookup": {}}) == []
        assert _parse_cua_api_response({}) == []

    def test_posting_to_job_has_all_required_keys(self):
        """Canonical job dict has exactly the required keys."""
        from jobcannon.engine.ats_platforms._platforms_tesla import _posting_to_job

        posting = {
            "title": "AI Engineer, Manipulation, Optimus",
            "source_url": "https://www.tesla.com/careers/search/job/224501",
            "source_id": "224501",
            "location": "Palo Alto, California",
            "department": "Tesla AI",
            "employment_type": "fulltime",
        }
        job = _posting_to_job(posting, "tesla")

        assert set(job.keys()) == _REQUIRED_KEYS
        assert job["company_source"] == "Tesla"
        assert job["title"] == "AI Engineer, Manipulation, Optimus"
        assert job["location"] == "Palo Alto, California"
        assert job["source_id"] == "224501"
        assert job["locations_structured"] == []
        assert job["description"] == ""  # deferred to enrichment
        assert job["posted_date"] is None


class TestTeslaRegistration:
    def test_scanner_registered_in_playwright_scanners(self):
        from jobcannon.engine.ats_platforms import PLAYWRIGHT_SCANNERS

        assert "tesla" in PLAYWRIGHT_SCANNERS
        assert PLAYWRIGHT_SCANNERS["tesla"].name == "tesla"
        assert PLAYWRIGHT_SCANNERS["tesla"].company_source == "Tesla"

    def test_tesla_registered_in_ats_registry(self):
        from jobcannon.engine.ats_registry import PLATFORMS

        assert "tesla" in PLATFORMS
        spec = PLATFORMS["tesla"]
        assert spec.playwright_scanner is not None
        # Tesla is a single-company custom scanner, NOT an ATS vendor: it must
        # contribute no bare corporate domain to ATS_DOMAINS (which drives
        # email-sender classification). tesla.com there would mislabel every
        # @tesla.com email as ATS-sourced. See the Phenom precedent.
        assert spec.domains == ()

    def test_tesla_domain_absent_from_ats_domains(self):
        """tesla.com must not leak into the ATS-vendor domain classification set."""
        from jobcannon.engine.ats_registry import ATS_DOMAINS

        assert "tesla.com" not in ATS_DOMAINS

    def test_tesla_in_playwright_platforms_derived_set(self):
        from jobcannon.engine.ats_registry import PLAYWRIGHT_PLATFORMS

        assert "tesla" in PLAYWRIGHT_PLATFORMS

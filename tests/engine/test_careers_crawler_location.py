# PORTED from tests/test_careers_crawler_location.py @ a7f0f38a85dfa0af4d305c04da833785f723d649 (private job-cannon). Ledger L-0578.
"""Tests for P2.2: careers crawler jobLocation capture from JSON-LD and URL slugs.

Covers (per issue #387):
- EY-shaped JSON-LD fixture: jobLocation Place -> Hyderabad (single dict with
  nested PostalAddress address).
- Plain-string jobLocation.
- List-of-Place jobLocation (multi-location posting).
- Location extracted from URL slug when JSON-LD is absent (gazetteer-validated).
- No fabrication: when neither JSON-LD nor a resolvable slug exists, the
  scraped job dict carries no 'location' key (or empty string).
- End-to-end ingest test: a crawler job dict with a JSON-LD location populates
  all five canonical location columns via upsert_job (proves the I-07 path
  is not triggered — Job.location only, no source_meta locations_raw).

# PORT-SEAM: DROPPED (private-only surface, listed in the PR body):
# test_crawler_jsonld_location_populates_all_five_columns and
# test_crawler_no_location_columns_stay_empty drove the private SQLite-backed
# job_finder.db.upsert_job end to end. The public upsert_job is
# host/Postgres-layer (jobcannon/db/_jobs.py, no SQLite equivalent in the
# tests/engine/ bare-sqlite3 harness) and its location-column population is
# already covered generically by tests/host/test_upsert_job.py /
# tests/engine/test_location_gate.py -- this pair only re-exercised that same
# path with a careers-crawler-shaped input, no careers-crawler-specific
# behaviour survives the drop. The json/sqlite3/pytest imports and the
# migrated_conn fixture below fed only those two dropped tests.
"""

from __future__ import annotations

# PORT-SEAM: json/sqlite3/pytest imports dropped (fed only the dropped end-to-end ingest tests)

from jobcannon.engine.careers_crawler._static_tier import (
    _extract_jsonld_postings,
    _location_from_jsonld,
    _location_from_url_slug,
)

# ---------------------------------------------------------------------------
# _location_from_jsonld — unit tests
# ---------------------------------------------------------------------------


class TestLocationFromJsonld:
    def test_ey_shaped_place_with_postal_address(self):
        """EY-style: single Place with a nested PostalAddress dict (canonical shape).

        addressLocality is preferred; region and country codes are excluded because
        short codes like "TG" (Togo vs Telangana) and "IN" (Indiana vs India) are
        ambiguous to parse_locations. The gazetteer resolves the correct country
        from the city name alone. See _place_to_string docstring.
        """
        posting = {
            "@type": "JobPosting",
            "title": "DE-Data Scientist",
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Hyderabad",
                    "addressRegion": "TG",
                    "addressCountry": "IN",
                },
            },
        }
        result = _location_from_jsonld(posting)
        # Locality-only assembly: the gazetteer resolves Hyderabad -> India correctly.
        assert result == "Hyderabad"

    def test_plain_string_job_location(self):
        """jobLocation as a plain string is returned as-is."""
        posting = {"@type": "JobPosting", "title": "Engineer", "jobLocation": "New York, NY"}
        result = _location_from_jsonld(posting)
        assert result == "New York, NY"

    def test_list_of_places(self):
        """Multi-location posting: list of Place dicts joined with ' | '.

        Each Place uses locality-only assembly (see _place_to_string docstring).
        """
        posting = {
            "@type": "JobPosting",
            "title": "Engineer",
            "jobLocation": [
                {
                    "@type": "Place",
                    "address": {
                        "@type": "PostalAddress",
                        "addressLocality": "New York",
                        "addressRegion": "NY",
                        "addressCountry": "US",
                    },
                },
                {
                    "@type": "Place",
                    "address": {
                        "@type": "PostalAddress",
                        "addressLocality": "San Francisco",
                        "addressRegion": "CA",
                        "addressCountry": "US",
                    },
                },
            ],
        }
        result = _location_from_jsonld(posting)
        assert result == "New York | San Francisco"

    def test_place_with_string_address(self):
        """Place whose 'address' is a plain string (non-PostalAddress fallback)."""
        posting = {
            "@type": "JobPosting",
            "title": "PM",
            "jobLocation": {"@type": "Place", "address": "London, UK"},
        }
        result = _location_from_jsonld(posting)
        assert result == "London, UK"

    def test_place_with_name_fallback(self):
        """Place with a 'name' field and no 'address'."""
        posting = {
            "@type": "JobPosting",
            "title": "PM",
            "jobLocation": {"@type": "Place", "name": "Berlin"},
        }
        result = _location_from_jsonld(posting)
        assert result == "Berlin"

    def test_absent_job_location_returns_empty(self):
        """No jobLocation key → empty string (no fabrication)."""
        posting = {"@type": "JobPosting", "title": "Engineer"}
        result = _location_from_jsonld(posting)
        assert result == ""

    def test_none_job_location_returns_empty(self):
        """Explicit None jobLocation → empty string."""
        posting = {"@type": "JobPosting", "title": "Engineer", "jobLocation": None}
        result = _location_from_jsonld(posting)
        assert result == ""

    def test_partial_postal_address_locality_preferred(self):
        """addressLocality is used (region/country are fallbacks, not joined)."""
        posting = {
            "@type": "JobPosting",
            "title": "Engineer",
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Mumbai",
                    "addressRegion": "",
                    "addressCountry": "IN",
                },
            },
        }
        result = _location_from_jsonld(posting)
        assert result == "Mumbai"

    def test_country_only_when_no_locality_or_region(self):
        """When only country is present (no locality/region), it is the sole token."""
        posting = {
            "@type": "JobPosting",
            "title": "Engineer",
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "",
                    "addressRegion": "",
                    "addressCountry": "DE",
                },
            },
        }
        result = _location_from_jsonld(posting)
        assert result == "DE"


# ---------------------------------------------------------------------------
# _extract_jsonld_postings — location key propagation
# ---------------------------------------------------------------------------


class TestExtractJsonldPostingsLocation:
    def test_jsonld_location_key_in_extracted_dict(self):
        """A JobPosting with jobLocation emits a 'location' key in the result dict."""
        data = {
            "@type": "JobPosting",
            "title": "Data Scientist",
            "url": "/jobs/hyderabad-data-scientist",
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Hyderabad",
                    "addressRegion": "TG",
                    "addressCountry": "IN",
                },
            },
        }
        results = _extract_jsonld_postings(data)
        assert len(results) == 1
        # locality-only assembly: see _location_from_jsonld/_place_to_string.
        assert results[0]["location"] == "Hyderabad"

    def test_jsonld_no_job_location_no_key(self):
        """A JobPosting without jobLocation does NOT emit a 'location' key."""
        data = {"@type": "JobPosting", "title": "Engineer", "url": "/jobs/123"}
        results = _extract_jsonld_postings(data)
        assert len(results) == 1
        assert "location" not in results[0]

    def test_item_list_preserves_location(self):
        """ItemList wrapper: location key preserved per posting."""
        data = {
            "@type": "ItemList",
            "itemListElement": [
                {
                    "@type": "JobPosting",
                    "title": "Engineer",
                    "jobLocation": "London",
                },
                {
                    "@type": "JobPosting",
                    "title": "Designer",
                    # No jobLocation
                },
            ],
        }
        results = _extract_jsonld_postings(data)
        assert len(results) == 2
        assert results[0]["location"] == "London"
        assert "location" not in results[1]


# ---------------------------------------------------------------------------
# _location_from_url_slug — unit tests
# ---------------------------------------------------------------------------


class TestLocationFromUrlSlug:
    def test_hyderabad_slug(self):
        """EY-style URL: city name at the start of the job slug resolves."""
        url = "https://careers.ey.com/jobs/Hyderabad-DE-Data-Scientist-VG-W4-CDAO0217"
        result = _location_from_url_slug(url)
        # The gazetteer must resolve "Hyderabad" as a known city.
        assert result == "Hyderabad"

    def test_no_resolvable_location_in_slug(self):
        """A slug that starts with a job-title word returns None (no fabrication)."""
        url = "https://example.com/jobs/Senior-Data-Scientist-12345"
        result = _location_from_url_slug(url)
        # "Senior", "Data", "Scientist" are not gazetteer locations.
        assert result is None

    def test_empty_url_returns_none(self):
        result = _location_from_url_slug("")
        assert result is None

    def test_url_with_no_path_returns_none(self):
        result = _location_from_url_slug("https://example.com")
        assert result is None

    def test_numeric_only_slug_returns_none(self):
        """Numeric-only tokens (IDs) are filtered before the gazetteer check."""
        result = _location_from_url_slug("https://example.com/jobs/12345")
        assert result is None


# PORT-SEAM: End-to-end ingest tests (migrated_conn fixture,
# test_crawler_jsonld_location_populates_all_five_columns,
# test_crawler_no_location_columns_stay_empty) dropped here -- see the module
# docstring DROPPED note above.

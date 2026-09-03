"""PORTED from tests/test_location_parser_scanner_integration.py @ abedc58ab57db586c74bf22c8a80cb40399deb8b (private job-cannon). Ledger L-0014.

Layer-1 scanner integration: _to_canonical for the 4 structured platforms.

Each scanner's ``_to_canonical`` maps the vendor's structured fields to
``list[JobLocation]`` at the ingestion boundary, bypassing the Layer-2
parser. These tests pin the per-vendor field mappings so a vendor API
shape change can't silently regress the structured-location pipeline.

(The private source cites `.planning/SPEC-location-parsing.md` — an
internal planning doc that does not exist in this repo; omitted here.)
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._platforms_ashby import (
    _to_canonical as ashby_canon,
)
from jobcannon.engine.ats_platforms._platforms_lever import (
    _to_canonical as lever_canon,
)
from jobcannon.engine.ats_platforms._platforms_rippling import (
    _to_canonical as rippling_canon,
)
from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
    _to_canonical as smartrecruiters_canon,
)

# ---------- SmartRecruiters ----------


def test_smartrecruiters_full_structured_location_passes_through():
    """SR returns city/region/regionCode/country/countryCode/remote → one JobLocation."""
    posting = {
        "location": {
            "city": "San Francisco",
            "region": "California",
            "regionCode": "CA",
            "country": "United States",
            "countryCode": "US",
            "remote": False,
        }
    }
    out = smartrecruiters_canon(posting)
    assert len(out) == 1
    loc = out[0]
    assert loc.city == "San Francisco"
    assert loc.region == "California"
    assert loc.region_code == "CA"
    assert loc.country == "United States"
    assert loc.country_code == "US"
    assert loc.workplace_type == "UNSPECIFIED"
    assert loc.unresolved is False


def test_smartrecruiters_remote_true_promotes_workplace_type():
    posting = {"location": {"city": "Toronto", "countryCode": "CA", "remote": True}}
    out = smartrecruiters_canon(posting)
    assert len(out) == 1
    assert out[0].workplace_type == "REMOTE"


def test_smartrecruiters_missing_location_returns_empty():
    assert smartrecruiters_canon({}) == []
    assert smartrecruiters_canon({"location": None}) == []
    assert smartrecruiters_canon({"location": "string-not-dict"}) == []


# ---------- Ashby ----------


def test_ashby_primary_postal_address_only():
    posting = {
        "workplaceType": "OnSite",
        "address": {
            "postalAddress": {
                "addressLocality": "New York",
                "addressRegion": "NY",
                "addressCountry": "US",
            }
        },
    }
    out = ashby_canon(posting)
    assert len(out) == 1
    loc = out[0]
    assert loc.city == "New York"
    assert loc.region == "NY"
    assert loc.country_code == "US"
    assert loc.country is None  # 2-letter compresses to country_code only
    assert loc.workplace_type == "ONSITE"
    assert loc.unresolved is False


def test_ashby_secondary_locations_yields_multi_entry_list():
    posting = {
        "workplaceType": "Hybrid",
        "address": {
            "postalAddress": {
                "addressLocality": "London",
                "addressCountry": "GB",
            }
        },
        "secondaryLocations": [
            {
                "address": {
                    "postalAddress": {
                        "addressLocality": "Berlin",
                        "addressCountry": "DE",
                    }
                }
            }
        ],
    }
    out = ashby_canon(posting)
    assert len(out) == 2
    assert out[0].city == "London"
    assert out[1].city == "Berlin"
    # workplace_type propagates from posting-level to every entry
    assert all(loc.workplace_type == "HYBRID" for loc in out)


def test_ashby_pure_remote_fallback_when_no_structured_address():
    posting = {"workplaceType": "Remote", "location": "Remote", "isRemote": True}
    out = ashby_canon(posting)
    assert len(out) == 1
    assert out[0].workplace_type == "REMOTE"
    # No structured address → unresolved=True
    assert out[0].unresolved is True


# ---------- Lever ----------


def test_lever_freeform_locations_with_structured_workplace_type():
    posting = {
        "workplaceType": "on-site",
        "categories": {
            "location": "Stockholm, Sweden",
            "allLocations": ["Stockholm, Sweden", "Berlin, Germany"],
        },
    }
    out = lever_canon(posting)
    # Both distinct locations preserved; primary dedup against allLocations works
    assert len(out) == 2
    assert {loc.raw for loc in out} == {"Stockholm, Sweden", "Berlin, Germany"}
    # Layer-1 trusts the kebab-case enum and normalizes
    assert all(loc.workplace_type == "ONSITE" for loc in out)
    # Lever location strings are freeform → unresolved=True at scanner boundary
    assert all(loc.unresolved is True for loc in out)


def test_lever_remote_with_no_locations_synthesizes_entry():
    posting = {"workplaceType": "remote", "categories": {}}
    out = lever_canon(posting)
    assert len(out) == 1
    assert out[0].workplace_type == "REMOTE"
    assert out[0].raw == "Remote"


def test_lever_unknown_workplace_type_normalizes_to_unspecified():
    posting = {"workplaceType": "futuristic", "categories": {"location": "Tokyo, JP"}}
    out = lever_canon(posting)
    assert len(out) == 1
    assert out[0].workplace_type == "UNSPECIFIED"


# ---------- Rippling ----------


def test_rippling_multi_location_array_unflattens():
    item = {
        "locations": [
            {
                "name": "San Francisco, CA",
                "city": "San Francisco",
                "state": "CA",
                "country": "US",
                "workplaceType": "Hybrid",
            },
            {"name": "Remote", "workplaceType": "Remote"},
        ]
    }
    out = rippling_canon(item)
    assert len(out) == 2
    assert out[0].city == "San Francisco"
    assert out[0].region_code == "CA"
    assert out[0].country_code == "US"
    assert out[0].workplace_type == "HYBRID"
    assert out[1].workplace_type == "REMOTE"


def test_rippling_two_letter_state_compresses_to_region_code():
    item = {
        "locations": [
            {"city": "Austin", "state": "TX", "country": "USA", "workplaceType": "OnSite"}
        ]
    }
    out = rippling_canon(item)
    assert len(out) == 1
    # 2-letter state goes to region_code; full-name 'USA' stays in country
    assert out[0].region_code == "TX"
    assert out[0].region is None
    assert out[0].country == "USA"
    assert out[0].country_code is None


def test_rippling_empty_locations_array_returns_empty():
    assert rippling_canon({"locations": []}) == []
    assert rippling_canon({}) == []
    assert rippling_canon({"locations": "not-a-list"}) == []

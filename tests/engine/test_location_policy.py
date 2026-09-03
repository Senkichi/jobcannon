# PORTED from tests/test_location_policy.py @ 80f7668ed61d9da522cba64bd79c1232bb80f36f (private job-cannon). Ledger L-0196.
"""Tests for the location policy engine foundation (Issue #1210)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from jobcannon.engine.location_policy import (
    BAY_AREA_CITIES,
    classify_geography,
    compute_input_fingerprint,
    compute_location_policy,
    get_location_policy_config,
    get_location_policy_config_hash,
    get_remote_eligible_countries,
    is_bay_area_city,
    is_unresolved_location_policy,
    json_to_verdict,
    normalize_city,
    normalize_country_code,
    normalize_profile_location_policy,
    normalize_region_code,
    verdict_to_json,
)


def _loc(
    workplace_type: str = "UNSPECIFIED",
    *,
    city: str | None = None,
    region: str | None = None,
    region_code: str | None = None,
    country: str | None = None,
    country_code: str | None = None,
    unresolved: bool = False,
) -> dict:
    """Build a location dict matching the JobLocation JSON shape."""
    return {
        "city": city,
        "region": region,
        "region_code": region_code,
        "country": country,
        "country_code": country_code,
        "workplace_type": workplace_type,
        "raw": "",
        "unresolved": unresolved,
    }


def _cfg(**profile_overrides) -> dict:
    """Build a minimal config dict for location policy tests.

    Fixes this test suite's tenant to the same target market the private
    repo's owner-specific defaults used to hardcode (US/CA, San Francisco
    primary) — see Ledger L-0196: that target market is now a per-tenant
    config value, not a module default, so tests supply it explicitly.
    """
    profile = {
        "home_country": "US",
        "target_locations": ["Remote", "San Francisco, CA"],
        "work_arrangement": "remote",
        "remote_eligible_countries": ["US"],
        "location_policy": {
            # A handful of cities this suite exercises as "metro" tier — a
            # tenant-configured stand-in for the private repo's 291-city
            # seed (Ledger L-0149, DIES; does not carry as a shipped
            # default). Not exhaustive, just enough to cover this suite.
            "bay_area_cities": ["oakland", "palo alto", "redwood city", "richmond", "san jose"],
            "target_region_code": "CA",
            "target_country_code": "US",
            "primary_city_fallback": "San Francisco",
        },
    }
    profile.update(profile_overrides)
    return {"profile": profile, "sources": {}, "scoring": {}, "db": {}}


class TestBayAreaGeography:
    """Unit tests for the ported geography-tier matching helpers.

    Ledger L-0196: ``BAY_AREA_CITIES`` ships empty (the private repo's
    291-city seed is Ledger L-0149, DIES — owner-specific, does not carry).
    Tests that exercised the seed now supply an equivalent ``extra_cities``
    set explicitly, matching how a real tenant configures its target metro.
    """

    def test_normalize_city_strips_state_and_zip(self):
        assert normalize_city("San Francisco, CA") == "san francisco"
        assert normalize_city("Palo Alto, CA, 94301") == "palo alto"
        assert normalize_city("New York, NY 10001") == "new york"

    def test_normalize_city_lowercases_and_trims(self):
        assert normalize_city("  San Francisco  ") == "san francisco"

    def test_bay_area_cities_seed_ships_empty(self):
        # Was "seed is lowercase and unique" against the 291-city private
        # default; the seed itself is Ledger L-0149 (DIES) and does not
        # carry, so the ported baseline is the empty set.
        assert BAY_AREA_CITIES == frozenset()

    def test_is_bay_area_city_membership(self):
        assert is_bay_area_city("San Francisco", frozenset({"san francisco"})) is True
        assert is_bay_area_city("Los Angeles", frozenset()) is False

    def test_is_bay_area_city_with_user_extras(self):
        assert is_bay_area_city("Foster City", frozenset({"foster city"})) is True

    def test_classify_geography_primary(self):
        assert classify_geography("San Francisco", "San Francisco", frozenset()) == "primary"

    def test_classify_geography_metro(self):
        assert classify_geography("Oakland", "San Francisco", frozenset({"oakland"})) == "metro"

    def test_classify_geography_outside_target(self):
        assert classify_geography("Los Angeles", "San Francisco", frozenset()) == "outside_target"

    def test_classify_geography_case_insensitive(self):
        assert classify_geography("SAN FRANCISCO", "san francisco", frozenset()) == "primary"


class TestStringNormalization:
    """Unit tests for country/region string normalization."""

    def test_normalize_country_code_common_names(self):
        assert normalize_country_code("United States") == "US"
        assert normalize_country_code("united states of america") == "US"
        assert normalize_country_code("Canada") == "CA"
        assert normalize_country_code("United Kingdom") == "GB"

    def test_normalize_country_code_already_iso(self):
        assert normalize_country_code("us") == "US"
        assert normalize_country_code("CA") == "CA"

    def test_normalize_country_code_unknown_returns_none(self):
        assert normalize_country_code("Xylophone") is None
        assert normalize_country_code(None) is None

    def test_normalize_region_code_us_states(self):
        assert normalize_region_code("California") == "CA"
        assert normalize_region_code("ca") == "CA"
        assert normalize_region_code("New York") == "NY"
        assert normalize_region_code("nj") == "NJ"

    def test_normalize_region_code_canadian_provinces(self):
        assert normalize_region_code("Ontario") == "ON"
        assert normalize_region_code("on") == "ON"
        assert normalize_region_code("British Columbia") == "BC"

    def test_field_aware_ca(self):
        """A bare 'CA' in a country field means Canada; in a region field means California."""
        assert normalize_country_code("CA") == "CA"
        assert normalize_region_code("CA") == "CA"

    def test_normalize_country_code_usa_literal(self):
        """The uppercase 'USA' alias resolves to the US code (#1576)."""
        assert normalize_country_code("USA") == "US"

    def test_normalize_country_code_parenthetical(self):
        """A known two-letter country code in parentheses is extracted, e.g. 'Remote (US)' (#1576)."""
        assert normalize_country_code("Remote (US)") == "US"
        assert normalize_country_code("New York, NY/Remote (US)") == "US"
        assert normalize_country_code("Remote (CA)") == "CA"

    def test_normalize_country_code_parenthetical_rejects_unknown(self):
        """A parenthetical capture that is not a known country code is rejected (#1576).

        A free-text parenthetical can carry a US-state code (``NY``), an
        arbitrary abbreviation (``XY``), or other non-country tokens. Promoting
        any of those to a country code routes through ``_classify_remote`` and
        yields a false active ``ineligible`` verdict. Unknown captures return
        ``None`` so the policy falls into the unresolved branch instead.
        """
        assert normalize_country_code("Remote (XY)") is None
        assert normalize_country_code("Remote (NY)") is None
        assert normalize_country_code("Engineer (AI)") is None


class TestRemoteEligibility:
    """Tests for the remote classification branch."""

    def test_remote_us_eligible(self):
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="US")],
            config=_cfg(),
        )
        assert policy.eligibility == "eligible"
        assert policy.workplace_class == "remote"
        assert policy.geography_tier == "remote"
        assert policy.rank == 5
        assert policy.sort_order == 2
        assert policy.effective_location_fit == 5

    def test_remote_canada_eligible_when_in_remote_eligible(self):
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="CA")],
            config=_cfg(remote_eligible_countries=["US", "CA"]),
        )
        assert policy.eligibility == "eligible"
        assert policy.rank == 5

    def test_remote_unrestricted_unknown(self):
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE")],
            config=_cfg(),
        )
        assert policy.eligibility == "unknown"
        assert policy.workplace_class == "remote"
        assert policy.geography_tier == "unknown"
        assert policy.rank == 0
        assert policy.sort_order == 1
        assert policy.effective_location_fit is None

    def test_remote_gb_ineligible_with_home_us(self):
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="GB")],
            config=_cfg(home_country="US"),
        )
        assert policy.eligibility == "ineligible"
        assert policy.rank == 0
        assert policy.sort_order == 0
        assert policy.effective_location_fit == 1

    def test_unresolved_remote_is_not_same_as_ineligible(self):
        """A missing country is unresolved (None), not ineligible (1) (#1576)."""
        unresolved = compute_location_policy(locations_structured=[_loc("REMOTE")], config=_cfg())
        ineligible = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="GB")],
            config=_cfg(home_country="US"),
        )
        assert unresolved.effective_location_fit is None
        assert ineligible.effective_location_fit == 1
        assert unresolved.effective_location_fit != ineligible.effective_location_fit

    def test_remote_gb_unknown_without_home_country(self):
        """Missing home_country makes country-restricted remote rows unknown.

        This is the _classify_remote home-country-unknown branch (#1576): the
        country resolves (GB) but with no home_country the policy cannot grade
        eligibility, so effective_location_fit is the None sentinel, not the
        legacy integer 2 that conflated 'no judgment' with 'poor fit'.
        """
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="GB")],
            config=_cfg(home_country=None),
        )
        assert policy.eligibility == "unknown"
        assert policy.workplace_class == "remote"
        assert policy.geography_tier == "unknown"
        assert policy.effective_location_fit is None
        assert policy.rank == 0
        assert policy.sort_order == 1

    def test_remote_country_string_canada_eligible(self):
        """Canada can be eligible when remote_eligible_countries includes CA."""
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country="Canada")],
            config=_cfg(remote_eligible_countries=["US", "CA"], home_country=None),
        )
        assert policy.eligibility == "eligible"
        assert policy.rank == 5

    def test_remote_non_us_ineligible(self):
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="IN")],
            config=_cfg(home_country="US"),
        )
        assert policy.eligibility == "ineligible"

    def test_remote_parenthetical_non_country_does_not_false_ineligible(self):
        """A non-country parenthetical in the country string stays unresolved (#1576).

        ``"Remote (NY)"`` must NOT normalize ``NY`` to a country code: ``NY`` is
        a US-state code, and promoting it would route through ``_classify_remote``
        as a country not in ``remote_eligible``/``home_country`` and yield a
        graded ``ineligible`` verdict — a false active ineligible. The validated
        parenthetical capture rejects ``NY`` and the policy falls into the
        unresolved branch (``effective_location_fit is None``) instead.
        """
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country="Remote (NY)")],
            config=_cfg(home_country="US"),
        )
        assert policy.eligibility == "unknown"
        assert policy.effective_location_fit is None
        assert policy.eligibility != "ineligible"


class TestOnSiteHybridEligibility:
    """Tests for the hybrid/onsite classification branch."""

    @pytest.mark.parametrize(
        ("workplace_type", "city", "expected_rank", "expected_fit", "expected_tier"),
        [
            ("HYBRID", "San Francisco", 4, 4, "primary"),
            ("HYBRID", "Oakland", 3, 3, "metro"),
            ("HYBRID", "Palo Alto", 3, 3, "metro"),
            ("ONSITE", "San Francisco", 2, 2, "primary"),
            ("ONSITE", "Oakland", 1, 2, "metro"),
            ("ONSITE", "Redwood City", 1, 2, "metro"),
        ],
    )
    def test_bay_area_hybrid_onsite_rank_table(
        self, workplace_type, city, expected_rank, expected_fit, expected_tier
    ):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    workplace_type,
                    city=city,
                    region="California",
                    region_code="CA",
                    country="United States",
                    country_code="US",
                )
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "eligible"
        assert policy.workplace_class == workplace_type.lower()
        assert policy.geography_tier == expected_tier
        assert policy.rank == expected_rank
        assert policy.effective_location_fit == expected_fit
        assert policy.sort_order == 2

    def test_onsite_metro_floored_to_fit_two(self):
        """On-site metro is rank 1 but effective_location_fit floors to 2."""
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "ONSITE",
                    city="Oakland",
                    region="California",
                    region_code="CA",
                    country_code="US",
                )
            ],
            config=_cfg(),
        )
        assert policy.rank == 1
        assert policy.effective_location_fit == 2

    def test_la_outside_target(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="Los Angeles",
                    region="California",
                    region_code="CA",
                    country_code="US",
                )
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "ineligible"
        assert policy.geography_tier == "outside_target"
        assert policy.rank == 0
        assert policy.sort_order == 0
        assert policy.effective_location_fit == 1

    @pytest.mark.parametrize(
        "city, region, region_code",
        [
            ("Newark", "New Jersey", "NJ"),
            ("Dublin", "Ohio", "OH"),
            ("Richmond", "Virginia", "VA"),
            ("Seattle", "Washington", "WA"),
            ("New York", "New York", "NY"),
        ],
    )
    def test_non_bay_us_cities_ineligible(self, city, region, region_code):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city=city,
                    region=region,
                    region_code=region_code,
                    country="United States",
                    country_code="US",
                )
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "ineligible"
        assert policy.geography_tier == "outside_target"

    def test_richmond_ca_is_metro(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="Richmond",
                    region="California",
                    region_code="CA",
                    country_code="US",
                )
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "eligible"
        assert policy.geography_tier == "metro"

    def test_toronto_ineligible(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="Toronto",
                    region="Ontario",
                    region_code="ON",
                    country="Canada",
                    country_code="CA",
                )
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "ineligible"
        assert policy.geography_tier == "outside_target"

    def test_london_ineligible(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="London",
                    region="England",
                    country="United Kingdom",
                    country_code="GB",
                )
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "ineligible"

    def test_sacramento_ineligible(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="Sacramento",
                    region="California",
                    region_code="CA",
                    country_code="US",
                )
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "ineligible"
        assert policy.geography_tier == "outside_target"

    @pytest.mark.parametrize(
        "field_to_drop",
        ["country_code", "region_code", "city"],
    )
    def test_missing_geo_fields_unknown(self, field_to_drop):
        loc = _loc(
            "HYBRID",
            city="San Francisco",
            region="California",
            region_code="CA",
            country="United States",
            country_code="US",
        )
        loc[field_to_drop] = None
        # Drop the corresponding raw string too; otherwise normalization backfills.
        if field_to_drop == "country_code":
            loc["country"] = None
        elif field_to_drop == "region_code":
            loc["region"] = None
        policy = compute_location_policy(locations_structured=[loc], config=_cfg())
        assert policy.eligibility == "unknown"
        assert policy.rank == 0
        assert policy.sort_order == 1
        assert policy.effective_location_fit is None

    def test_unresolved_locations_ignored(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc("HYBRID", unresolved=True),
                _loc(
                    "HYBRID",
                    city="San Francisco",
                    region="California",
                    region_code="CA",
                    country_code="US",
                ),
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "eligible"
        assert policy.geography_tier == "primary"

    def test_all_unresolved_uses_fallback_unknown(self):
        """Unresolved locations are ignored; row-level fallback has no city/region."""
        policy = compute_location_policy(
            locations_structured=[_loc("HYBRID", unresolved=True)],
            workplace_type="HYBRID",
            primary_country_code="US",
            config=_cfg(),
        )
        assert policy.eligibility == "unknown"


class TestMultiLocationAndPostings:
    """Tests for multi-location best-door and posting rollups."""

    def _presence(self, city, country_code="US", region_code="CA"):
        return _loc(
            "HYBRID",
            city=city,
            region="California",
            region_code=region_code,
            country_code=country_code,
        )

    def test_sf_plus_toronto_eligible(self):
        policy = compute_location_policy(
            locations_structured=[
                self._presence("San Francisco"),
                _loc(
                    "HYBRID",
                    city="Toronto",
                    region="Ontario",
                    region_code="ON",
                    country_code="CA",
                ),
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "eligible"
        assert policy.geography_tier == "primary"

    def test_sf_plus_la_eligible(self):
        policy = compute_location_policy(
            locations_structured=[self._presence("San Francisco"), self._presence("Los Angeles")],
            config=_cfg(),
        )
        assert policy.eligibility == "eligible"
        assert policy.geography_tier == "primary"

    def test_la_plus_ny_ineligible(self):
        policy = compute_location_policy(
            locations_structured=[
                self._presence("Los Angeles"),
                _loc(
                    "HYBRID",
                    city="New York",
                    region="New York",
                    region_code="NY",
                    country_code="US",
                ),
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "ineligible"

    def test_best_door_prefers_higher_sort_order_then_rank(self):
        """A remote-eligible location should beat an ineligible on-site one."""
        policy = compute_location_policy(
            locations_structured=[
                _loc("REMOTE", country_code="US"),
                _loc(
                    "HYBRID",
                    city="Toronto",
                    region="Ontario",
                    region_code="ON",
                    country_code="CA",
                ),
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "eligible"
        assert policy.workplace_class == "remote"
        assert policy.rank == 5

    def test_postings_rollup_best_door(self):
        postings = [
            {"locations_structured": [self._presence("Los Angeles")]},
            {"locations_structured": [self._presence("San Francisco")]},
        ]
        policy = compute_location_policy(postings=postings, config=_cfg())
        assert policy.eligibility == "eligible"
        assert policy.geography_tier == "primary"
        assert policy.posting_policies is not None
        assert len(policy.posting_policies) == 2

    def test_postings_all_ineligible(self):
        postings = [
            {"locations_structured": [self._presence("Los Angeles")]},
            {"locations_structured": [self._presence("New York")]},
        ]
        policy = compute_location_policy(postings=postings, config=_cfg())
        assert policy.eligibility == "ineligible"

    def test_synthetic_location_from_row_fallback(self):
        """No postings and no locations_structured uses row-level workplace_type/primary_country_code."""
        policy = compute_location_policy(
            locations_structured=[],
            workplace_type="REMOTE",
            primary_country_code="US",
            config=_cfg(),
        )
        assert policy.eligibility == "eligible"
        assert policy.workplace_class == "remote"
        assert policy.rank == 5


class TestPrimaryCityDetection:
    """Tests that primary_city is derived from target_locations."""

    def test_primary_city_from_target_locations(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="Palo Alto",
                    region="California",
                    region_code="CA",
                    country_code="US",
                )
            ],
            config=_cfg(target_locations=["Palo Alto, CA"]),
        )
        assert policy.primary_city == "Palo Alto"
        assert policy.geography_tier == "primary"

    def test_primary_city_defaults_to_san_francisco(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="San Francisco",
                    region="California",
                    region_code="CA",
                    country_code="US",
                )
            ],
            config=_cfg(target_locations=["New York, NY"]),
        )
        assert policy.primary_city == "San Francisco"

    def test_primary_city_skips_remote_token(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="San Jose",
                    region="California",
                    region_code="CA",
                    country_code="US",
                )
            ],
            config=_cfg(target_locations=["Remote", "San Jose, CA"]),
        )
        assert policy.primary_city == "San Jose"

    def test_primary_city_region_string_does_not_match(self):
        """Primary city matches only city names, not region strings like 'California'."""
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="San Francisco",
                    region="California",
                    region_code="CA",
                    country_code="US",
                )
            ],
            config=_cfg(target_locations=["California", "Remote"]),
        )
        assert policy.primary_city == "San Francisco"

    def test_primary_city_with_user_extra_cities(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="Foster City",
                    region="California",
                    region_code="CA",
                    country_code="US",
                )
            ],
            config=_cfg(
                target_locations=["Foster City, CA"],
                location_policy={"bay_area_cities": ["Foster City"]},
            ),
        )
        assert policy.primary_city == "Foster City"
        assert policy.geography_tier == "primary"


class TestConfigHelpers:
    """Tests for job_finder.config location-policy helpers."""

    def test_get_remote_eligible_countries_default(self):
        assert get_remote_eligible_countries({}) == frozenset({"US"})

    def test_get_remote_eligible_countries_uppercases(self):
        cfg = {"profile": {"remote_eligible_countries": ["us", "ca"]}}
        assert get_remote_eligible_countries(cfg) == frozenset({"US", "CA"})

    def test_get_location_policy_config_defaults(self):
        cfg = _cfg()
        cfg["profile"].pop("location_policy")
        result = get_location_policy_config(cfg)
        assert result["bay_area_cities"] == frozenset()
        assert result["max_radius_miles"] == 50
        assert result["geocoding_enabled"] is False

    def test_normalize_profile_location_policy_appends_extras(self):
        cfg = _cfg(location_policy={"bay_area_cities": ["Foster City", "  ", 123]})
        normalized = normalize_profile_location_policy(cfg)
        block = normalized["profile"]["location_policy"]
        assert block["bay_area_cities"] == frozenset({"foster city"})

    def test_normalize_profile_location_policy_does_not_mutate_input(self):
        cfg = _cfg()
        normalized = normalize_profile_location_policy(cfg)
        assert normalized is not cfg
        assert normalized["profile"] is not cfg["profile"]

    def test_get_location_policy_config_hash_excludes_last_rescored_config_hash(self):
        cfg = _cfg()
        cfg["profile"]["location_policy"] = {
            **cfg["profile"]["location_policy"],
            "last_rescored_config_hash": "abc123",
        }
        h1 = get_location_policy_config_hash(cfg)
        cfg2 = _cfg()
        h2 = get_location_policy_config_hash(cfg2)
        assert h1 == h2

    def test_get_location_policy_config_hash_changes_with_target_locations(self):
        h1 = get_location_policy_config_hash(_cfg(target_locations=["Remote"]))
        h2 = get_location_policy_config_hash(_cfg(target_locations=["San Francisco, CA"]))
        assert h1 != h2


class TestInputFingerprintAndSerialization:
    """Tests for compute_input_fingerprint and JSON serialization."""

    def test_compute_input_fingerprint_is_sha256_hex(self):
        fp = compute_input_fingerprint(
            locations_structured=[_loc("HYBRID", city="San Francisco", country_code="US")],
            config=_cfg(),
        )
        assert isinstance(fp, str)
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_compute_input_fingerprint_changes_with_config(self):
        fp1 = compute_input_fingerprint(
            locations_structured=[_loc("HYBRID", city="San Francisco", country_code="US")],
            config=_cfg(target_locations=["Remote"]),
        )
        fp2 = compute_input_fingerprint(
            locations_structured=[_loc("HYBRID", city="San Francisco", country_code="US")],
            config=_cfg(target_locations=["San Francisco, CA"]),
        )
        assert fp1 != fp2

    def test_compute_input_fingerprint_stable_for_same_inputs(self):
        args = {
            "locations_structured": [_loc("HYBRID", city="San Francisco", country_code="US")],
            "config": _cfg(),
        }
        assert compute_input_fingerprint(**args) == compute_input_fingerprint(**args)

    def test_verdict_to_json_and_json_to_verdict(self):
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="San Francisco",
                    region="California",
                    region_code="CA",
                    country="United States",
                    country_code="US",
                )
            ],
            config=_cfg(),
        )
        text = verdict_to_json(policy)
        parsed = json_to_verdict(text)
        assert parsed["eligibility"] == "eligible"
        assert parsed["workplace_class"] == "hybrid"
        assert parsed["geography_tier"] == "primary"
        assert parsed["rank"] == 4
        assert parsed["primary_city"] == "San Francisco"

    def test_computed_at_is_naive_utc_iso_no_z(self):
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="US")],
            config=_cfg(),
        )
        assert policy.computed_at.endswith("Z") is False
        ts = datetime.fromisoformat(policy.computed_at)
        assert ts.tzinfo is None


class TestWorkArrangementNoEffect:
    """work_arrangement does not change eligibility."""

    def test_work_arrangement_does_not_change_eligibility(self):
        loc = _loc(
            "HYBRID",
            city="San Francisco",
            region="California",
            region_code="CA",
            country_code="US",
        )
        p1 = compute_location_policy(
            locations_structured=[loc], config=_cfg(work_arrangement="remote")
        )
        p2 = compute_location_policy(
            locations_structured=[loc], config=_cfg(work_arrangement="hybrid")
        )
        assert p1.eligibility == p2.eligibility == "eligible"
        assert p1.rank == p2.rank == 4


class TestFieldAwareCA:
    """Field-aware 'CA' normalization: country CA=Canada, region CA=California."""

    def test_country_ca_means_canada(self):
        """A country string 'CA' in a remote location means Canada."""
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country="CA")],
            config=_cfg(remote_eligible_countries=["US"], home_country="US"),
        )
        assert policy.eligibility == "ineligible"

    def test_region_ca_means_california(self):
        """A region string 'CA' in a hybrid location means California."""
        policy = compute_location_policy(
            locations_structured=[
                _loc(
                    "HYBRID",
                    city="San Francisco",
                    region="CA",
                    country="United States",
                    country_code="US",
                )
            ],
            config=_cfg(),
        )
        assert policy.eligibility == "eligible"
        assert policy.geography_tier == "primary"


# ---------------------------------------------------------------------------
# #1202: has_subcountry_constraint gate — compute_location_policy returns None
# ---------------------------------------------------------------------------


class TestSubcountryConstraintGatePolicy:
    """Issue #1202: when has_subcountry_constraint=True, compute_location_policy
    returns None (defer to LLM judgment) instead of mis-firing _classify_remote
    (rank=5 for REMOTE-in-home-country) on a posting the candidate cannot
    actually take.
    """

    def test_genworth_remote_us_state_restricted_returns_none(self):
        """Genworth case: REMOTE in US (home_country) → _classify_remote would
        return rank=5, but the JD restricts to ~37 named Eastern/Central
        timezone states. With the gate set, the policy must abstain (None)."""
        loc = _loc("REMOTE", country_code="US")
        result = compute_location_policy(
            locations_structured=[loc],
            workplace_type="REMOTE",
            primary_country_code="US",
            config=_cfg(),
            has_subcountry_constraint=True,
        )
        assert result is None

    def test_gate_false_keeps_existing_remote_us_behavior(self):
        """Without the gate, REMOTE in US (home_country) returns rank=5
        (eligible). Proves the gate is opt-in and cannot regress existing
        behavior."""
        loc = _loc("REMOTE", country_code="US")
        policy = compute_location_policy(
            locations_structured=[loc],
            workplace_type="REMOTE",
            primary_country_code="US",
            config=_cfg(),
            has_subcountry_constraint=False,
        )
        assert policy is not None
        assert policy.rank == 5
        assert policy.eligibility == "eligible"

    def test_gate_short_circuits_before_any_classification(self):
        """Even an onsite-in-target-geo posting (San Francisco hybrid →
        eligible) must abstain when the gate is set — the constraint is a
        hard fact that overrides all rule-table geography matching."""
        loc = _loc(
            "HYBRID",
            city="San Francisco",
            region="California",
            region_code="CA",
            country_code="US",
        )
        result = compute_location_policy(
            locations_structured=[loc],
            workplace_type="HYBRID",
            config=_cfg(),
            has_subcountry_constraint=True,
        )
        assert result is None

    def test_gate_default_false_preserves_unchanged_callers(self):
        """Callers that don't pass the parameter get the legacy behavior —
        the gate defaults to False and never fires."""
        loc = _loc("REMOTE", country_code="US")
        policy = compute_location_policy(
            locations_structured=[loc],
            workplace_type="REMOTE",
            config=_cfg(),
        )
        assert policy is not None
        assert policy.rank == 5

    def test_gate_none_with_postings_returns_none(self):
        """When postings are provided and the gate is set, the entire policy
        abstains (returns None) — every posting for that job defers to LLM."""
        posting = {
            "locations_structured": [_loc("REMOTE", country_code="US")],
            "workplace_type": "REMOTE",
        }
        result = compute_location_policy(
            postings=[posting],
            config=_cfg(),
            has_subcountry_constraint=True,
        )
        assert result is None


class TestIsUnresolvedLocationPolicy:
    """Unit tests for is_unresolved_location_policy (#1576).

    The canonical signal for an unresolved policy is the combination of
    ``eligibility='unknown'`` and ``geography_tier='unknown'``. New verdicts
    store ``effective_location_fit: null`` and old verdicts store the legacy
    integer 2, so the function keys on the eligibility/tier pair rather than
    on the effective value alone.
    """

    def test_new_unresolved_verdict_with_null_effective_fit(self):
        """A new unresolved verdict (effective_location_fit=null) is unresolved."""
        policy = compute_location_policy(locations_structured=[_loc("REMOTE")], config=_cfg())
        assert policy.effective_location_fit is None
        assert is_unresolved_location_policy(verdict_to_json(policy)) is True

    def test_legacy_unresolved_verdict_with_integer_two(self):
        """A legacy unresolved verdict storing the integer 2 is still
        recognised by the eligibility/tier check (#1576 back-compat)."""
        legacy = json.dumps(
            {
                "eligibility": "unknown",
                "geography_tier": "unknown",
                "effective_location_fit": 2,
            }
        )
        assert is_unresolved_location_policy(legacy) is True

    def test_resolved_eligible_verdict_is_not_unresolved(self):
        """An eligible verdict is resolved."""
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="US")], config=_cfg()
        )
        assert policy.eligibility == "eligible"
        assert is_unresolved_location_policy(verdict_to_json(policy)) is False

    def test_resolved_ineligible_verdict_is_not_unresolved(self):
        """An ineligible verdict is resolved (it carries a graded judgment)."""
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="GB")],
            config=_cfg(home_country="US"),
        )
        assert policy.eligibility == "ineligible"
        assert is_unresolved_location_policy(verdict_to_json(policy)) is False

    def test_null_input_returns_false(self):
        """None / empty input is not unresolved (no verdict to inspect)."""
        assert is_unresolved_location_policy(None) is False
        assert is_unresolved_location_policy("") is False

    def test_malformed_json_returns_false(self):
        """Malformed JSON is not unresolved (parse failure is not a verdict)."""
        assert is_unresolved_location_policy("{not json") is False
        assert is_unresolved_location_policy("null") is False
        assert is_unresolved_location_policy("[]") is False

    def test_missing_eligibility_keys_returns_false(self):
        """A dict missing the eligibility/tier keys is not unresolved."""
        assert is_unresolved_location_policy(json.dumps({"effective_location_fit": 2})) is False

    def test_only_one_unknown_axis_returns_false(self):
        """Both eligibility AND geography_tier must be 'unknown' — one alone
        is not the unresolved signal (e.g. an unknown-tier eligible row)."""
        assert (
            is_unresolved_location_policy(
                json.dumps({"eligibility": "unknown", "geography_tier": "remote"})
            )
            is False
        )
        assert (
            is_unresolved_location_policy(
                json.dumps({"eligibility": "eligible", "geography_tier": "unknown"})
            )
            is False
        )

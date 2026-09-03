# PORTED from tests/test_posting_location_policy.py @ 4721bce234365ee6c27c11b0212303f067a73507 (private job-cannon). Ledger L-0516.
"""Tests for the per-posting location_policy write path (issue #1214).

Covers ``apply_location_policy_to_postings`` — the helper that replaces the
retired ``_apply_location_fit_override`` posting-write path — and the
postings best-door rollup scenarios named in issue #1214's acceptance
criteria: remote + on-site geo-target -> remote wins; all-ineligible ->
ineligible; all-unknown -> unknown; eligible + unknown -> eligible.

The rollup itself (``compute_location_policy(postings=...)``) is issue
#1210's engine and already has baseline coverage in test_location_policy.py;
this module adds the four named scenarios explicitly and, for each, verifies
the per-posting enrichment fields that ``apply_location_policy_to_postings``
writes.

# PORT-SEAM: overlap notes (ported anyway per the L-0509 precedent, noted
# here rather than skipped). TestPostingsRollupScenarios's 4 named
# compute_location_policy(postings=...) scenarios overlap
# tests/engine/test_location_policy.py::TestMultiLocationAndPostings (same
# rollup engine, different concrete locations/cities). The
# apply_location_policy_to_postings write-path tests
# (TestApplyLocationPolicyToPostings) are net-new -- no pre-existing
# coverage of that helper. TestApplyTargetsSurfacesPolicyColorEndToEnd
# overlaps tests/engine/test_direct_link.py's
# test_apply_targets_surfaces_location_fit_color_when_present /
# test_apply_targets_sets_location_fit_color_none_for_legacy_posting (same
# issue #1215 rank-2/emerald scenario), kept anyway for its
# compute_location_policy -> apply_location_policy_to_postings ->
# apply_targets full-chain coverage the other file does not exercise.
"""

from __future__ import annotations

# PORT-SEAM: job_finder.web.location_policy -> jobcannon.engine.location_policy.
from jobcannon.engine.location_policy import (
    apply_location_policy_to_postings,
    compute_location_policy,
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
    profile = {
        "home_country": "US",
        "target_locations": ["Remote", "San Francisco, CA"],
        "work_arrangement": "remote",
        "remote_eligible_countries": ["US"],
        # PORT-SEAM: private's bare bay_area_cities: [] relied on the
        # module-level 291-city BAY_AREA_CITIES seed (Ledger L-0149, DIES,
        # owner-specific) to resolve "San Francisco" as the primary_city via
        # _detect_primary_city's is_bay_area_city() check. That seed ships
        # empty publicly (Ledger L-0196, see tests/engine/test_location_policy.py's
        # own _cfg()), so primary_city_fallback + target_region_code are
        # supplied explicitly here -- same adaptation already established in
        # that file's _cfg().
        "location_policy": {
            "bay_area_cities": [],
            "target_region_code": "CA",
            "target_country_code": "US",
            "primary_city_fallback": "San Francisco",
        },
    }
    profile.update(profile_overrides)
    return {"profile": profile, "sources": {}, "scoring": {}, "db": {}}


def _remote_us() -> dict:
    return {"locations_structured": [_loc("REMOTE", country_code="US")]}


def _sf_hybrid() -> dict:
    return {
        "locations_structured": [
            _loc(
                "HYBRID",
                city="San Francisco",
                region="California",
                region_code="CA",
                country_code="US",
            )
        ]
    }


def _nyc_onsite() -> dict:
    """New York region_code != 'CA' -> ineligible under this engine's Bay-Area rule."""
    return {
        "locations_structured": [
            _loc(
                "ONSITE",
                city="New York City",
                region="New York",
                region_code="NY",
                country_code="US",
            )
        ]
    }


def _unspecified() -> dict:
    return {"locations_structured": [_loc("UNSPECIFIED")]}


class TestPostingsRollupScenarios:
    """compute_location_policy(postings=...) best-door rollup, per issue #1214."""

    def test_remote_plus_onsite_remote_wins(self):
        policy = compute_location_policy(postings=[_remote_us(), _sf_hybrid()], config=_cfg())
        assert policy.eligibility == "eligible"
        assert policy.workplace_class == "remote"
        assert policy.rank == 5

    def test_all_ineligible(self):
        policy = compute_location_policy(postings=[_nyc_onsite(), _nyc_onsite()], config=_cfg())
        assert policy.eligibility == "ineligible"

    def test_all_unknown(self):
        policy = compute_location_policy(postings=[_unspecified(), _unspecified()], config=_cfg())
        assert policy.eligibility == "unknown"

    def test_eligible_plus_unknown_eligible_wins(self):
        policy = compute_location_policy(postings=[_unspecified(), _sf_hybrid()], config=_cfg())
        assert policy.eligibility == "eligible"


class TestApplyLocationPolicyToPostings:
    """The per-posting write helper that replaces _apply_location_fit_override."""

    def test_enriches_each_posting_with_rank_color_and_verdict(self):
        postings = [_remote_us(), _sf_hybrid(), _nyc_onsite()]
        policy = compute_location_policy(postings=postings, config=_cfg())

        enriched = apply_location_policy_to_postings(postings, policy)

        assert enriched is not postings
        assert len(enriched) == 3
        remote_posting, sf_posting, nyc_posting = enriched

        assert remote_posting["location_fit"] == 5
        assert remote_posting["location_fit_color"] == "bg-emerald-500"
        assert remote_posting["location_policy_verdict"]["eligibility"] == "eligible"

        assert sf_posting["location_fit"] == 4
        assert sf_posting["location_fit_color"] == "bg-emerald-500"
        assert sf_posting["location_policy_verdict"]["eligibility"] == "eligible"

        assert nyc_posting["location_fit"] == 0
        assert nyc_posting["location_fit_color"] == "bg-red-500"
        assert nyc_posting["location_policy_verdict"]["eligibility"] == "ineligible"

        # Original postings list and dicts are not mutated.
        assert "location_fit" not in postings[0]
        assert enriched[0] is not postings[0]

    def test_unknown_eligibility_maps_to_amber(self):
        postings = [_unspecified()]
        policy = compute_location_policy(postings=postings, config=_cfg())
        enriched = apply_location_policy_to_postings(postings, policy)
        assert enriched[0]["location_fit_color"] == "bg-amber-500"
        assert enriched[0]["location_policy_verdict"]["eligibility"] == "unknown"

    def test_empty_postings_returns_empty_list(self):
        policy = compute_location_policy(locations_structured=[], config=_cfg())
        assert apply_location_policy_to_postings([], policy) == []

    def test_row_level_only_policy_has_no_posting_breakdown_and_is_a_noop(self):
        """A policy computed WITHOUT a postings= argument has posting_policies=None;
        the helper must not crash zipping against it — it returns the postings
        list unchanged (new list, not the same object)."""
        postings = [_sf_hybrid()]
        policy = compute_location_policy(
            locations_structured=[_loc("REMOTE", country_code="US")], config=_cfg()
        )
        assert policy.posting_policies is None

        result = apply_location_policy_to_postings(postings, policy)
        assert result == postings
        assert result is not postings


def _sf_onsite() -> dict:
    """San Francisco ON-SITE (not hybrid) -> eligible, primary-tier, rank 2.

    This is the exact shape issue #1215 is about: under the OLD apply-badge
    thresholds (>=4 green / ==3 amber / else red), rank 2 would render red —
    indistinguishable from an ineligible posting (rank 0). It must render
    emerald because it is eligible.
    """
    return {
        "locations_structured": [
            _loc(
                "ONSITE",
                city="San Francisco",
                region="California",
                region_code="CA",
                country_code="US",
            )
        ]
    }


class TestApplyTargetsSurfacesPolicyColorEndToEnd:
    """Issue #1215: apply_targets (jobcannon.engine.direct_link) must surface the
    per-posting location_fit_color written by apply_location_policy_to_postings
    (#1214) end-to-end, unchanged, so the Apply-button badge is policy-driven
    rather than re-deriving color from the retired fixed-scale location_fit
    thresholds that #1215 replaces in the templates.
    """

    def test_eligible_onsite_rank_2_posting_surfaces_emerald_through_apply_targets(self):
        # PORT-SEAM: job_finder.web.direct_link -> jobcannon.engine.direct_link.
        from jobcannon.engine.direct_link import apply_targets

        posting = _sf_onsite() | {"apply_url": "https://jobs.ashbyhq.com/acme/1"}
        policy = compute_location_policy(postings=[posting], config=_cfg())
        enriched = apply_location_policy_to_postings([posting], policy)

        # The #1214 write path: rank 2, eligible, emerald.
        assert enriched[0]["location_fit"] == 2
        assert enriched[0]["location_fit_color"] == "bg-emerald-500"
        assert enriched[0]["location_policy_verdict"]["eligibility"] == "eligible"

        # The #1215 read path: apply_targets must pass location_fit_color through
        # unchanged rather than re-deriving a color from location_fit == 2 (which
        # the retired threshold ladder would have colored red).
        targets = apply_targets({"postings": enriched})
        assert len(targets) == 1
        assert targets[0]["location_fit"] == 2
        assert targets[0]["location_fit_color"] == "bg-emerald-500"

    def test_ineligible_posting_surfaces_red_through_apply_targets(self):
        # PORT-SEAM: job_finder.web.direct_link -> jobcannon.engine.direct_link.
        from jobcannon.engine.direct_link import apply_targets

        posting = _nyc_onsite() | {"apply_url": "https://jobs.ashbyhq.com/acme/2"}
        policy = compute_location_policy(postings=[posting], config=_cfg())
        enriched = apply_location_policy_to_postings([posting], policy)

        assert enriched[0]["location_fit"] == 0
        assert enriched[0]["location_fit_color"] == "bg-red-500"

        targets = apply_targets({"postings": enriched})
        assert targets[0]["location_fit_color"] == "bg-red-500"

    def test_unknown_posting_surfaces_amber_through_apply_targets(self):
        # PORT-SEAM: job_finder.web.direct_link -> jobcannon.engine.direct_link.
        from jobcannon.engine.direct_link import apply_targets

        posting = _unspecified() | {"apply_url": "https://jobs.ashbyhq.com/acme/3"}
        policy = compute_location_policy(postings=[posting], config=_cfg())
        enriched = apply_location_policy_to_postings([posting], policy)

        assert enriched[0]["location_fit_color"] == "bg-amber-500"

        targets = apply_targets({"postings": enriched})
        assert targets[0]["location_fit_color"] == "bg-amber-500"

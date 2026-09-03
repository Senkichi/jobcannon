"""Tests for compute_location_fit — P3.1 deterministic location_fit override.

Covers the FULL rule table including all †-rows (requires home_country).

Rule table (first-match-wins):
    Row 1: any REMOTE unrestricted, 'Remote' ∈ targets  → (5, "fully remote, remote targeted")
    Row 2: any REMOTE restricted to home_country †       → (5, "fully remote, remote targeted")
    Row 3: all REMOTE restricted to countries ≠ home †  → (1, "remote but ineligible geography")
    Row 4: all onsite/hybrid/UNSP, countries ≠ home, no geo target match † → (1, "on-site outside candidate geography")
    Row 5: any city/region/country matches non-Remote target → (5, "on-site/hybrid in target geography")
    otherwise → None

Reference: issue #390.
"""

from __future__ import annotations

from jobcannon.engine.location_fit import compute_location_fit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _loc(
    workplace_type: str = "UNSPECIFIED",
    country_code: str | None = None,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
    unresolved: bool = False,
) -> dict:
    """Build a location dict matching the JobLocation JSON shape."""
    return {
        "workplace_type": workplace_type,
        "country_code": country_code,
        "city": city,
        "region": region,
        "country": country,
        "region_code": None,
        "raw": "",
        "unresolved": unresolved,
    }


def _remote(country_code: str | None = None, **kw) -> dict:
    return _loc(workplace_type="REMOTE", country_code=country_code, **kw)


def _onsite(country_code: str | None = None, **kw) -> dict:
    return _loc(workplace_type="ONSITE", country_code=country_code, **kw)


def _hybrid(country_code: str | None = None, **kw) -> dict:
    return _loc(workplace_type="HYBRID", country_code=country_code, **kw)


# ---------------------------------------------------------------------------
# Row 1: any REMOTE unrestricted, 'Remote' ∈ targets → (5, ...)
# ---------------------------------------------------------------------------


class TestRow1RemoteUnrestricted:
    def test_remote_no_country_code_with_remote_target(self):
        locs = [_remote()]  # no country_code = unrestricted
        score, reason = compute_location_fit(locs, "REMOTE", None, ["Remote"], "US")
        assert score == 5
        assert "remote" in reason.lower()

    def test_remote_no_country_code_multiple_targets(self):
        locs = [_remote()]
        score, _ = compute_location_fit(locs, "REMOTE", None, ["Remote", "San Francisco"], "US")
        assert score == 5

    def test_remote_no_country_code_no_remote_target_skips_row1(self):
        """Without 'Remote' in targets, row 1 must not fire."""
        locs = [_remote()]
        result = compute_location_fit(locs, "REMOTE", None, ["San Francisco"], "US")
        # Row 1 does NOT fire; row 5 checks city match (city is None here → no match)
        assert result is None

    def test_remote_no_country_code_no_home_country(self):
        """Row 1 does not require home_country."""
        locs = [_remote()]
        score, _ = compute_location_fit(locs, "REMOTE", None, ["Remote"], None)
        assert score == 5

    def test_remote_row1_fires_even_with_mixed_locations(self):
        """Best location wins — one unrestricted remote + one onsite fires row 1."""
        locs = [_remote(), _onsite(country_code="IN")]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5


# ---------------------------------------------------------------------------
# Row 2: any REMOTE restricted to home_country  † → (5, ...)
# ---------------------------------------------------------------------------


class TestRow2RemoteHomecountry:
    def test_remote_us_home_us_target_remote(self):
        locs = [_remote(country_code="US")]
        score, reason = compute_location_fit(locs, "REMOTE", "US", ["Remote"], "US")
        assert score == 5
        assert "remote" in reason.lower()

    def test_remote_home_country_matches_case_insensitively(self):
        locs = [_remote(country_code="us")]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_row2_requires_remote_in_targets(self):
        """Row 2 needs 'Remote' ∈ targets to fire."""
        locs = [_remote(country_code="US")]
        # 'Remote' not in targets → row 2 skipped; row 5 checks city (None) → None
        result = compute_location_fit(locs, None, None, ["New York"], "US")
        assert result is None

    def test_row2_skipped_when_no_home_country(self):
        """†-row: without home_country, row 2 cannot fire."""
        locs = [_remote(country_code="US")]
        # home_country=None, 'Remote' in targets, but row 2 skipped
        # Row 1 also skipped (country_code="US" makes it restricted, not unrestricted)
        # Row 5: city=None → None
        result = compute_location_fit(locs, None, None, ["Remote"], None)
        assert result is None

    def test_row2_skipped_when_home_country_doesnt_match(self):
        """Row 2 requires the REMOTE location's country = home_country."""
        locs = [_remote(country_code="GB")]  # GB, not US
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Row 1 skipped (restricted), row 2 skipped (GB ≠ US)
        # Row 3 fires: all remote, restricted to country ≠ home
        score, reason = result
        assert score == 1
        assert "ineligible" in reason.lower()


# ---------------------------------------------------------------------------
# Row 3: all REMOTE restricted to countries ≠ home_country † → (1, ...)
# ---------------------------------------------------------------------------


class TestRow3AllRemoteIneligible:
    def test_remote_in_country_outside_home(self):
        """Single REMOTE restricted to IN ≠ US home."""
        locs = [_remote(country_code="IN")]
        score, reason = compute_location_fit(locs, "REMOTE", "IN", ["Remote"], "US")
        assert score == 1
        assert "ineligible" in reason.lower()

    def test_remote_multiple_all_outside_home(self):
        """All three remotes restricted to non-home countries."""
        locs = [_remote("IN"), _remote("GB"), _remote("DE")]
        score, reason = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 1
        assert "ineligible" in reason.lower()

    def test_row3_skipped_when_one_remote_is_home(self):
        """Mix of home + non-home remotes: row 3 doesn't fire (best-wins)."""
        locs = [_remote("IN"), _remote("US")]
        # Row 2: any remote restricted to home → fires
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_row3_skipped_when_no_home_country(self):
        """† — without home_country, row 3 cannot fire."""
        locs = [_remote("IN")]
        # No home_country: row 1 skipped (restricted), row 2 skipped, row 3 skipped
        # Row 5: city=None → None
        result = compute_location_fit(locs, None, None, ["Remote"], None)
        assert result is None

    def test_row3_skipped_when_remote_has_no_country(self):
        """An unrestricted REMOTE (no country_code) is NOT outside any country."""
        locs = [_remote()]  # no country_code
        # Row 1 fires first (unrestricted + Remote in targets)
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_row3_skipped_when_has_onsite_fallback(self):
        """Row 3 must NOT fire when there is also a non-remote location."""
        locs = [_remote("IN"), _onsite("US")]
        # All-remote check fails → row 3 skipped
        # Row 5: onsite US city/region? city=None → check ...
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Onsite US, no geo target match → row 4 check? onsite in home country → row 4 NOT fire (all outside)
        # Falls to row 5: city=None, country="US" not in [Remote] targets → None
        assert result is None


# ---------------------------------------------------------------------------
# Row 4: all onsite/hybrid/UNSP outside home_country, no geo target match †
# → (1, "on-site outside candidate geography")
# ---------------------------------------------------------------------------


class TestRow4OnsiteOutsideHome:
    def test_onsite_in_india_us_home(self):
        """Classic Hyderabad regression: EY DS job, targets=[Remote], home=US."""
        locs = [_onsite("IN", city="Hyderābād", region="Telangana", country="India")]
        score, reason = compute_location_fit(locs, "ONSITE", "IN", ["Remote"], "US")
        assert score == 1
        assert "on-site outside" in reason.lower()

    def test_hyderabad_regression_end_to_end(self):
        """Issue #390 Hyderabad regression test.

        EY DE-Data-Scientist-VG-W4-CDAO0217 row facts:
          locations_structured: [{city: "Hyderābād", country_code: "IN", workplace_type: "ONSITE"}]
          primary_country_code: "IN"
          targets: ["Remote"], home: "US"
        Expected: (1, "on-site outside candidate geography")
        """
        locs = [_onsite("IN", city="Hyderābād", region="Telangana", country="India")]
        result = compute_location_fit(locs, "ONSITE", "IN", ["Remote"], "US")
        assert result is not None
        score, reason = result
        assert score == 1, f"Expected 1, got {score}"
        assert "on-site outside" in reason.lower()

    def test_onsite_multiple_all_outside_home(self):
        locs = [_onsite("IN"), _onsite("SG")]
        score, reason = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 1
        assert "on-site outside" in reason.lower()

    def test_row4_skipped_when_no_home_country(self):
        """† — without home_country, row 4 cannot fire."""
        locs = [_onsite("IN")]
        result = compute_location_fit(locs, None, None, ["Remote"], None)
        # Row 5: city=None → None
        assert result is None

    def test_row4_skipped_when_in_home_country(self):
        """Onsite in home country — row 4 must NOT fire; falls to row 5/None."""
        locs = [_onsite("US", city="Seattle")]
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Row 4 skipped (in home country); row 5 city "Seattle" vs ["Remote"] → no match
        assert result is None

    def test_row4_skipped_when_geo_target_matches(self):
        """Even if onsite/foreign, a geo target match in the same posting blocks row 4."""
        # This should not happen in practice (location both foreign AND target?),
        # but the rule says "no target_location matches any city/region" — if it does,
        # row 4 is skipped and row 5 fires.
        locs = [_onsite("GB", city="London", country="United Kingdom")]
        score, reason = compute_location_fit(locs, None, None, ["Remote", "London"], "US")
        # Row 4 check: does any target match London? Yes → row 4 skipped.
        # Row 5: city "london" in target "london" → (5, ...)
        assert score == 5

    def test_row4_hybrid_onsite_outside_home(self):
        locs = [_hybrid("CN")]
        score, reason = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 1
        assert "on-site outside" in reason.lower()

    def test_row4_unspecified_outside_home(self):
        locs = [_loc("UNSPECIFIED", country_code="DE")]
        score, reason = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 1
        assert "on-site outside" in reason.lower()

    def test_row4_no_country_code_skips_row4(self):
        """UNSPECIFIED with no country_code — _country_outside_home returns False."""
        locs = [_loc("UNSPECIFIED")]  # no country_code
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Cannot confirm outside → row 4 not fired → None
        assert result is None


# ---------------------------------------------------------------------------
# Row 5: any city/region/country matches a non-Remote target
# → (5, "on-site/hybrid in target geography")
# ---------------------------------------------------------------------------


class TestRow5GeoTargetMatch:
    def test_city_exact_match(self):
        locs = [_onsite("US", city="San Francisco", region="California")]
        score, reason = compute_location_fit(locs, None, None, ["San Francisco", "Remote"], "US")
        assert score == 5
        assert "target geography" in reason.lower()

    def test_region_match(self):
        locs = [_onsite("US", city="San Jose", region="California")]
        score, reason = compute_location_fit(locs, None, None, ["California"], "US")
        assert score == 5

    def test_country_match(self):
        locs = [_onsite("CA", city="Toronto", country="Canada")]
        score, reason = compute_location_fit(locs, None, None, ["Canada"], "US")
        assert score == 5

    def test_remote_target_excluded_from_geo_matching(self):
        """'Remote' as a target_location must NOT trigger row 5."""
        locs = [_onsite("IN", city="Mumbai")]
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # Row 4 fires (onsite, IN ≠ US, no geo match)
        assert result == (1, "on-site outside candidate geography")

    def test_case_insensitive_match(self):
        locs = [_onsite("US", city="new york")]
        score, _ = compute_location_fit(locs, None, None, ["New York"], "US")
        assert score == 5

    def test_substring_match_city_in_target(self):
        locs = [_onsite("US", city="New York")]
        score, _ = compute_location_fit(locs, None, None, ["New York, NY"], "US")
        assert score == 5

    def test_multiple_locs_one_matches(self):
        locs = [_onsite("IN"), _onsite("US", city="Seattle")]
        score, _ = compute_location_fit(locs, None, None, ["Seattle"], "US")
        assert score == 5


# ---------------------------------------------------------------------------
# None — LLM judges
# ---------------------------------------------------------------------------


class TestFallsThrough:
    def test_no_structured_data(self):
        result = compute_location_fit([], None, None, ["Remote"], "US")
        assert result is None

    def test_all_unresolved(self):
        locs = [_loc(unresolved=True), _loc(unresolved=True)]
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        # All unresolved; no denormalized fallback either
        assert result is None

    def test_onsite_in_home_country_no_target_match(self):
        """Onsite in home country, city not in targets — desirability is judgment."""
        locs = [_onsite("US", city="Omaha")]
        result = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert result is None

    def test_empty_targets(self):
        locs = [_remote()]
        result = compute_location_fit(locs, None, None, [], "US")
        # No 'Remote' in targets → row 1 skipped; no geo targets → row 5 skipped
        assert result is None

    def test_no_home_no_match(self):
        locs = [_onsite("IN")]
        result = compute_location_fit(locs, None, None, ["Remote"], None)
        assert result is None


# ---------------------------------------------------------------------------
# Denormalized-column fallback (empty locations_structured)
# ---------------------------------------------------------------------------


class TestDenormalizedFallback:
    def test_fallback_remote_unrestricted(self):
        """When locations_structured is empty, use workplace_type + primary_country_code."""
        result = compute_location_fit(
            [],
            "REMOTE",  # workplace_type
            None,  # primary_country_code = unrestricted
            ["Remote"],
            "US",
        )
        assert result == (5, "fully remote, remote targeted")

    def test_fallback_onsite_outside_home(self):
        result = compute_location_fit(
            [],
            "ONSITE",
            "IN",
            ["Remote"],
            "US",
        )
        assert result == (1, "on-site outside candidate geography")

    def test_fallback_none_when_no_denorm_data(self):
        result = compute_location_fit([], None, None, ["Remote"], "US")
        assert result is None


# ---------------------------------------------------------------------------
# Multi-location best-wins semantics
# ---------------------------------------------------------------------------


class TestMultiLocation:
    def test_best_wins_one_remote_one_onsite_outside(self):
        """Job offerable in Remote OR Hyderabad — best for candidate is Remote."""
        locs = [_remote(), _onsite("IN")]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_best_wins_onsite_us_in_targets(self):
        """NYC + Bangalore: NYC matches target → (5, ...)."""
        locs = [_onsite("IN", city="Bengaluru"), _onsite("US", city="New York")]
        score, _ = compute_location_fit(locs, None, None, ["New York", "Remote"], "US")
        assert score == 5

    def test_all_remote_mixed_home_notHome(self):
        """US-remote + IN-remote: row 2 fires (any REMOTE in home)."""
        locs = [_remote("US"), _remote("IN")]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5


# ---------------------------------------------------------------------------
# Unresolved entries contribute nothing
# ---------------------------------------------------------------------------


class TestUnresolvedContributesNothing:
    def test_unresolved_plus_resolved_remote(self):
        locs = [_loc(unresolved=True), _remote()]
        score, _ = compute_location_fit(locs, None, None, ["Remote"], "US")
        assert score == 5

    def test_all_unresolved_with_denorm_fallback(self):
        """When all structured are unresolved, denormalized columns kick in."""
        locs = [_loc(unresolved=True)]
        result = compute_location_fit(locs, "ONSITE", "IN", ["Remote"], "US")
        # Denormalized fallback: ONSITE IN vs US home → row 4
        assert result == (1, "on-site outside candidate geography")


# ---------------------------------------------------------------------------
# Hyderabad end-to-end classification regression (issue #390 exit criterion)
# ---------------------------------------------------------------------------


class TestHyderabadClassificationRegression:
    """The issue exit criterion: Hyderabad-class jobs can NEVER classify 'apply'.

    EY row facts: ONSITE IN, no remote, targets=[Remote], home=US.
    With the deterministic override → location_fit=1 → derive_classification
    branch "any axis == 1" → "reject".
    """

    def test_hyderabad_classifies_reject(self):
        """Deterministic 1 on location_fit drives derive_classification to 'reject'."""
        from jobcannon.engine.classification import derive_classification

        # Simulate what score_and_persist_job would do:
        # 1. LLM returns location_fit=5 (hallucinated, historical Hyderabad bug)
        # 2. P3.1 override replaces it with 1
        # 3. derive_classification on the overridden sub_scores
        sub_scores_after_override = {
            "title_fit": 4,
            "location_fit": 1,  # P3.1 deterministic override
            "comp_fit": 4,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 4,
        }
        classification = derive_classification(sub_scores_after_override, None)
        assert classification == "reject", (
            f"Hyderabad job with location_fit=1 must classify 'reject', got {classification!r}"
        )

    def test_hyderabad_historical_llm5_would_apply(self):
        """Confirm the bug we're fixing: historical LLM-5 was 'apply'."""
        from jobcannon.engine.classification import derive_classification

        sub_scores_llm_only = {
            "title_fit": 4,
            "location_fit": 5,  # historical LLM hallucination
            "comp_fit": 4,
            "domain_match": 4,
            "seniority_match": 4,
            "skills_match": 4,
        }
        classification = derive_classification(sub_scores_llm_only, None)
        assert classification == "apply", "Confirm the pre-fix bug: should have been 'apply'"


# ---------------------------------------------------------------------------
# Rows R-a / R-b: remote-first refinement (work_arrangement == "remote")
# ---------------------------------------------------------------------------


class TestRemoteFirstRefinement:
    """A remote-first candidate: hybrid/on-site is capped at 4 in a target geo
    and is a disqualifier (1) outside one. UNSPECIFIED modality stays deferred.

    Contrast with TestRow5GeoTargetMatch, which exercises the SAME geographies
    for a NON-remote candidate (work_arrangement unset) and expects a full 5.
    """

    def test_hybrid_in_target_geo_capped_at_four(self):
        locs = [_hybrid("US", city="San Francisco", region="California")]
        result = compute_location_fit(
            locs, "HYBRID", "US", ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result == (4, "on-site/hybrid in target geography, remote preferred")

    def test_onsite_in_target_geo_capped_at_four(self):
        locs = [_onsite("US", city="San Francisco", region="California")]
        result = compute_location_fit(
            locs, "ONSITE", "US", ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result == (4, "on-site/hybrid in target geography, remote preferred")

    def test_hybrid_outside_target_is_reject(self):
        """The Brigit NYC-only regression: hybrid NYC, target=SF → reject."""
        locs = [_hybrid("US", city="New York City", region="New York")]
        result = compute_location_fit(
            locs, "HYBRID", "US", ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result == (1, "on-site/hybrid outside target geography")

    def test_onsite_in_home_country_not_target_is_reject(self):
        """Stricter than legacy: onsite in a home-country city NOT in targets is
        now a reject for a remote-first candidate (legacy → None/LLM)."""
        locs = [_onsite("US", city="Omaha", region="Nebraska")]
        result = compute_location_fit(
            locs, "ONSITE", "US", ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result == (1, "on-site/hybrid outside target geography")

    def test_multi_city_hybrid_best_target_wins(self):
        """Job offerable hybrid in NYC OR SF — SF matches target → 4 (not reject)."""
        locs = [
            _hybrid(city="New York City", region="New York", country="USA"),
            _hybrid(city="San Francisco", region="California", country="United States"),
        ]
        result = compute_location_fit(
            locs, "HYBRID", None, ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result == (4, "on-site/hybrid in target geography, remote preferred")

    def test_denorm_hybrid_fallback_when_structured_unspecified(self):
        """Exact Brigit 'Lead Data Scientist' shape: structured SF marked
        UNSPECIFIED, but the job-level workplace_type=HYBRID → treated as
        presence-required in a target geo → 4 (not 5)."""
        locs = [
            _loc("UNSPECIFIED", city="San Francisco", region="California", country="United States")
        ]
        result = compute_location_fit(
            locs, "HYBRID", None, ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result == (4, "on-site/hybrid in target geography, remote preferred")

    def test_unspecified_modality_in_target_not_capped(self):
        """No modality signal at either level → could be remote → NOT capped/
        rejected; falls through to Row 5's geography match → 5."""
        locs = [
            _loc("UNSPECIFIED", city="San Francisco", region="California", country="United States")
        ]
        result = compute_location_fit(
            locs, "UNSPECIFIED", None, ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result == (5, "on-site/hybrid in target geography")

    def test_unspecified_home_no_geo_defers(self):
        """Hybrid with no resolvable geo (only home country_code) → not rejected
        (we can't say it's outside target); defers to the LLM."""
        locs = [_hybrid("US")]  # no city/region/country
        result = compute_location_fit(
            locs, "HYBRID", "US", ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result is None

    def test_foreign_onsite_no_city_still_caught_by_row4(self):
        """R-b defers (no geo name) but legacy Row 4 still rejects foreign onsite."""
        locs = [_onsite("IN")]  # country_code only, no city name
        result = compute_location_fit(
            locs, "ONSITE", "IN", ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result == (1, "on-site outside candidate geography")

    def test_remote_still_five_for_remote_first(self):
        """Remote-eligible option always beats presence-required (Row 1)."""
        locs = [_remote()]
        result = compute_location_fit(
            locs, "REMOTE", None, ["San Francisco", "Remote"], "US", work_arrangement="remote"
        )
        assert result == (5, "fully remote, remote targeted")

    def test_non_remote_pref_keeps_full_five(self):
        """Backward compat: a hybrid-preferring candidate still gets 5 for a
        hybrid role in a target geography (rows R-a/R-b do not fire)."""
        locs = [_hybrid("US", city="San Francisco", region="California")]
        result = compute_location_fit(
            locs, "HYBRID", "US", ["San Francisco", "Remote"], "US", work_arrangement="hybrid"
        )
        assert result == (5, "on-site/hybrid in target geography")

    def test_unset_work_arrangement_keeps_legacy_behavior(self):
        """No work_arrangement passed → legacy Row 5 → 5 (proves the refinement
        is opt-in and cannot silently regress the existing rule table)."""
        locs = [_hybrid("US", city="New York City", region="New York")]
        result = compute_location_fit(locs, "HYBRID", "US", ["New York", "Remote"], "US")
        assert result == (5, "on-site/hybrid in target geography")


class TestRemoteFirstClassificationRegression:
    """End-to-end: how the R-a (4) vs R-b (1) verdict drives the final class."""

    _BRIGIT_LEAD_DS = {
        "title_fit": 5,
        "location_fit": 5,  # LLM value, pre-override
        "comp_fit": 3,
        "domain_match": 4,
        "seniority_match": 5,
        "skills_match": 5,
    }

    def test_hybrid_sf_capped_to_four_still_applies(self):
        """SF-hybrid (R-a → 4) keeps the strong Brigit role at 'apply' — the
        user said SF hybrid is viable, just not a perfect remote match."""
        from jobcannon.engine.classification import derive_classification

        subs = {**self._BRIGIT_LEAD_DS, "location_fit": 4}
        assert derive_classification(subs, None) == "apply"

    def test_hybrid_nyc_reject_to_one_rejects(self):
        """NYC-only hybrid (R-b → 1) flips the role to 'reject' (any axis==1)."""
        from jobcannon.engine.classification import derive_classification

        subs = {**self._BRIGIT_LEAD_DS, "location_fit": 1}
        assert derive_classification(subs, None) == "reject"


# ---------------------------------------------------------------------------
# #1202: has_subcountry_constraint gate — short-circuits to None
# ---------------------------------------------------------------------------


class TestSubcountryConstraintGate:
    """Issue #1202: when a JD carries a geographic constraint finer than
    country/region/city (e.g. a remote role restricted to a named subset of US
    states), the structured facts under-express it and the rule table would
    mis-fire. The ``has_subcountry_constraint`` gate short-circuits to None so
    the LLM's own judgment is authoritative.
    """

    def test_genworth_remote_us_state_restricted_returns_none(self):
        """Genworth case: REMOTE in US (home_country) → Row 2 would fire (5),
        but the JD restricts to ~37 named Eastern/Central timezone states.
        With the gate set, the override must abstain (None)."""
        locs = [_remote("US")]  # structured says REMOTE in US
        result = compute_location_fit(
            locs,
            "REMOTE",
            "US",
            ["Remote"],
            "US",
            has_subcountry_constraint=True,
        )
        assert result is None

    def test_gate_false_keeps_existing_row2_behavior(self):
        """Without the gate, Row 2 fires normally (REMOTE in home_country → 5).
        Proves the gate is opt-in and cannot regress the existing rule table."""
        locs = [_remote("US")]
        score, reason = compute_location_fit(
            locs,
            "REMOTE",
            "US",
            ["Remote"],
            "US",
            has_subcountry_constraint=False,
        )
        assert score == 5
        assert "remote" in reason.lower()

    def test_gate_short_circuits_before_any_rule(self):
        """Even an onsite-in-target-geo posting (Row 5 → 5) must abstain when
        the gate is set — the constraint is a hard fact that overrides all
        rule-table geography matching."""
        locs = [_onsite("US", city="San Francisco", region="California")]
        result = compute_location_fit(
            locs,
            "ONSITE",
            "US",
            ["San Francisco", "Remote"],
            "US",
            has_subcountry_constraint=True,
        )
        assert result is None

    def test_gate_default_false_preserves_unchanged_callers(self):
        """Callers that don't pass the parameter get the legacy behavior —
        the gate defaults to False and never fires."""
        locs = [_remote("US")]
        score, _ = compute_location_fit(locs, "REMOTE", "US", ["Remote"], "US")
        assert score == 5  # Row 2 fires as before

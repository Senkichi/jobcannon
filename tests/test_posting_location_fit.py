# PORTED from tests/test_posting_location_fit.py @ 6fd9f9b31c6a32c7262de3619d247008425e2cde (private job-cannon). Ledger L-0515.
"""Tests for per-posting location_fit computation (P3, issue #642).

Covers the compute_posting_fits helper in isolation. Per-posting fit is
computed from each posting's own locations_structured + workplace_type,
and the row-level location_fit is set to the maximum (best door wins).

The orchestrator integration tests that used to live here exercised
``scoring_orchestrator._apply_location_fit_override``, which issue #1214
retired (the deterministic location_policy engine replaces it end to end,
including the per-posting write path via
``location_policy.apply_location_policy_to_postings``). Those tests were
removed rather than ported 1:1 — the retired function no longer exists,
and equivalent per-posting-write coverage for the new pipeline lives in
tests/test_posting_location_policy.py.

Reference: issue #642, issue #1214.
"""

from __future__ import annotations

# PORT-SEAM: job_finder.web.location_fit -> jobcannon.engine.location_fit.
from jobcannon.engine.location_fit import compute_posting_fits

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


def _posting(
    ats_platform: str = "ashby",
    source_id: str = "test-id",
    apply_url: str = "https://example.com/apply",
    locations_structured: list[dict] | None = None,
    workplace_type: str = "UNSPECIFIED",
    confidence: float = 1.0,
) -> dict:
    """Build a posting descriptor matching the P1 shape."""
    return {
        "ats_platform": ats_platform,
        "source_id": source_id,
        "apply_url": apply_url,
        "locations_structured": locations_structured or [],
        "workplace_type": workplace_type,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# compute_posting_fits unit tests
# ---------------------------------------------------------------------------


class TestComputePostingFits:
    def test_brigit_two_postings_fit_and_rollup(self):
        """Two Ashby posting descriptors (SF-hybrid, NYC-hybrid) on one row,
        remote-first candidate: SF descriptor location_fit == 4, NYC descriptor
        location_fit == 1, row location_fit == 4."""
        postings = [
            _posting(
                ats_platform="ashby",
                source_id="6a214803",
                apply_url="https://brigit.com/apply/sf",
                locations_structured=[
                    _loc(
                        workplace_type="HYBRID",
                        city="San Francisco",
                        region="California",
                        country="United States",
                        country_code="US",
                    )
                ],
                workplace_type="HYBRID",
            ),
            _posting(
                ats_platform="ashby",
                source_id="b10f0fae",
                apply_url="https://brigit.com/apply/nyc",
                locations_structured=[
                    _loc(
                        workplace_type="HYBRID",
                        city="New York City",
                        region="New York",
                        country="USA",
                        country_code="US",
                    )
                ],
                workplace_type="HYBRID",
            ),
        ]

        updated_postings, rollup = compute_posting_fits(
            postings=postings,
            target_locations=["San Francisco"],
            home_country="US",
            work_arrangement="remote",
        )

        # SF hybrid in target geo → 4 (remote-first cap)
        assert updated_postings[0]["location_fit"] == 4
        # NYC hybrid outside target geo → 1
        assert updated_postings[1]["location_fit"] == 1
        # Rollup = best door = 4
        assert rollup == 4

    def test_single_posting_row_unchanged(self):
        """A one-posting row produces the same row-level location_fit as the
        current row-level override for the same facts (no regression)."""
        # PORT-SEAM: job_finder.web.location_fit -> jobcannon.engine.location_fit.
        from jobcannon.engine.location_fit import compute_location_fit

        posting = _posting(
            locations_structured=[
                _loc(
                    workplace_type="HYBRID",
                    city="San Francisco",
                    region="California",
                    country="United States",
                    country_code="US",
                )
            ],
            workplace_type="HYBRID",
        )

        updated_postings, rollup = compute_posting_fits(
            postings=[posting],
            target_locations=["San Francisco"],
            home_country="US",
            work_arrangement="remote",
        )

        # Per-posting fit
        assert updated_postings[0]["location_fit"] == 4
        # Rollup matches per-posting (single posting)
        assert rollup == 4

        # Verify it matches the row-level compute_location_fit for same facts
        row_verdict = compute_location_fit(
            locations_structured=posting["locations_structured"],
            workplace_type=posting["workplace_type"],
            primary_country_code="US",
            target_locations=["San Francisco"],
            home_country="US",
            work_arrangement="remote",
        )
        assert row_verdict == (4, "on-site/hybrid in target geography, remote preferred")

    def test_empty_postings_falls_through_to_row_override(self):
        """A row with postings == '[]'/NULL uses the existing merged-set path
        unchanged (returns empty list, None rollup)."""
        updated_postings, rollup = compute_posting_fits(
            postings=[],
            target_locations=["San Francisco"],
            home_country="US",
            work_arrangement="remote",
        )

        assert updated_postings == []
        assert rollup is None

    def test_all_postings_undecided_no_override(self):
        """Every posting yields compute_location_fit is None; row override does
        not fire, per-posting location_fit stays None, LLM sub-score untouched."""
        postings = [
            _posting(
                locations_structured=[
                    _loc(
                        workplace_type="UNSPECIFIED",
                        city="Unknown City",
                        country_code="US",
                    )
                ],
                workplace_type="UNSPECIFIED",
            ),
            _posting(
                locations_structured=[
                    _loc(
                        workplace_type="UNSPECIFIED",
                        city="Another Unknown",
                        country_code="US",
                    )
                ],
                workplace_type="UNSPECIFIED",
            ),
        ]

        updated_postings, rollup = compute_posting_fits(
            postings=postings,
            target_locations=["Remote"],
            home_country="US",
            work_arrangement="remote",
        )

        # Both postings undecided → location_fit not set
        assert "location_fit" not in updated_postings[0]
        assert "location_fit" not in updated_postings[1]
        # Rollup is None → override should not fire
        assert rollup is None

    def test_postings_other_fields_preserved(self):
        """After fit compute, ats_platform/source_id/apply_url/locations_structured/
        workplace_type/confidence on each descriptor are byte-for-byte unchanged;
        only location_fit is added."""
        original_posting = _posting(
            ats_platform="greenhouse",
            source_id="gh-123",
            apply_url="https://greenhouse.io/apply/123",
            locations_structured=[
                _loc(
                    workplace_type="HYBRID",
                    city="Seattle",
                    region="Washington",
                    country="United States",
                    country_code="US",
                )
            ],
            workplace_type="HYBRID",
            confidence=0.95,
        )

        updated_postings, _rollup = compute_posting_fits(
            postings=[original_posting],
            target_locations=["Seattle"],
            home_country="US",
            work_arrangement="hybrid",
        )

        result = updated_postings[0]
        # All original fields preserved
        assert result["ats_platform"] == "greenhouse"
        assert result["source_id"] == "gh-123"
        assert result["apply_url"] == "https://greenhouse.io/apply/123"
        assert result["locations_structured"] == original_posting["locations_structured"]
        assert result["workplace_type"] == "HYBRID"
        assert result["confidence"] == 0.95
        # Only location_fit added
        assert "location_fit" in result
        assert result["location_fit"] == 5

    def test_helper_is_pure_immutable(self):
        """The new helper returns new list/dict objects; the input postings list
        is not mutated."""
        original_posting = _posting(
            locations_structured=[
                _loc(
                    workplace_type="HYBRID",
                    city="San Francisco",
                    country_code="US",
                )
            ],
            workplace_type="HYBRID",
        )
        postings = [original_posting]

        # Capture original object identity
        original_list_id = id(postings)
        original_dict_id = id(postings[0])

        updated_postings, _rollup = compute_posting_fits(
            postings=postings,
            target_locations=["San Francisco"],
            home_country="US",
            work_arrangement="remote",
        )

        # New list returned
        assert id(updated_postings) != original_list_id
        # New dict returned
        assert id(updated_postings[0]) != original_dict_id
        # Original unchanged
        assert "location_fit" not in postings[0]


# ---------------------------------------------------------------------------
# #1202: has_subcountry_constraint gate — per-posting short-circuit
# ---------------------------------------------------------------------------


class TestPostingSubcountryConstraintGate:
    """Issue #1202: when has_subcountry_constraint=True, every posting
    short-circuits to None (the JD carries a constraint the structured facts
    cannot represent). The rollup must also be None.
    """

    def test_all_postings_none_when_gate_set(self):
        """Genworth case: REMOTE in US postings would score 5 via Row 2,
        but the gate forces every posting to None."""
        posting = _posting(
            locations_structured=[_loc(workplace_type="REMOTE", country_code="US")],
            workplace_type="REMOTE",
        )
        updated, rollup = compute_posting_fits(
            postings=[posting, posting],
            target_locations=["Remote"],
            home_country="US",
            work_arrangement="remote",
            has_subcountry_constraint=True,
        )
        assert all("location_fit" not in p for p in updated)
        assert rollup is None

    def test_gate_false_preserves_normal_behavior(self):
        """Without the gate, postings score normally (Row 2 → 5)."""
        posting = _posting(
            locations_structured=[_loc(workplace_type="REMOTE", country_code="US")],
            workplace_type="REMOTE",
        )
        updated, rollup = compute_posting_fits(
            postings=[posting],
            target_locations=["Remote"],
            home_country="US",
            work_arrangement="remote",
            has_subcountry_constraint=False,
        )
        assert updated[0]["location_fit"] == 5
        assert rollup == 5

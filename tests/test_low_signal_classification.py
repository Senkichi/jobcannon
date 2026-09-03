# PORTED from tests/test_low_signal_classification.py @ 6ae8d7c545a7ec4ca847df6fce2565d544bbf24a (private job-cannon). Ledger L-0502.
"""Tests for the low_signal classification rule (Phase 2d sub-fix + issue #210).

5th classification value distinct from apply/consider/skip/reject.

Rule precedence in derive_classification:
    1. legitimacy_note truthy           -> 'reject'
    2. enrichment_tier in terminal set
       AND jd_full_length < threshold   -> 'low_signal'
    3. flat-neutral vector (all == 3)   -> 'low_signal'  (issue #210 branch C)
    4. any sub-score == 1               -> 'reject'
    5. positive evidence                -> 'apply'       (issue #210 branch B)
    6. all sub-scores >= 2              -> 'consider'
    7. otherwise                        -> 'skip'

Terminal tiers (no further automatic enrichment): 'exhausted', 'agentic',
'agentic_exhausted' (see _TERMINAL_ENRICHMENT_TIERS in _classification.py).

NOTE: all-3s is now caught by the flat-neutral branch (C) before the enrichment
branch even matters, so tests that want to exercise the *enrichment*-driven
low_signal path (branch 2) vs the standard rubric use a non-flat strong vector
(``_STRONG``) — otherwise branch C would mask which branch produced the verdict.
``_STRONG`` (all-4s) carries positive evidence -> 'apply' on the normal path.
"""

import pytest

from jobcannon.engine.classification import derive_classification

_KEYS = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)
# Strong, non-flat vector: positive evidence -> 'apply' on the standard path.
_STRONG = dict.fromkeys(_KEYS, 4)
# Flat-neutral vector: all axes at the "couldn't tell" midpoint -> 'low_signal'.
_NEUTRAL = dict.fromkeys(_KEYS, 3)


def test_low_signal_when_exhausted_and_short_jd():
    # Strong (non-flat) vector so the verdict comes from the enrichment branch,
    # not the flat-neutral branch.
    cls = derive_classification(
        sub_scores=dict(_STRONG),
        legitimacy_note=None,
        enrichment_tier="exhausted",
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


def test_not_low_signal_when_jd_long_enough():
    cls = derive_classification(
        sub_scores=dict(_STRONG),
        legitimacy_note=None,
        enrichment_tier="exhausted",
        jd_full_length=5000,
        low_signal_threshold=1500,
    )
    assert cls == "apply"


def test_not_low_signal_when_enrichment_not_exhausted():
    """Short JD with enrichment_tier=None is a re-enrichment candidate, not low_signal."""
    cls = derive_classification(
        sub_scores=dict(_STRONG),
        legitimacy_note=None,
        enrichment_tier=None,
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    # Standard rule applies; enrichment can still run on a future tier.
    assert cls == "apply"


def test_legitimacy_note_overrides_low_signal():
    sub_scores = dict.fromkeys(
        (
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ),
        3,
    )
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note="scam pattern",
        enrichment_tier="exhausted",
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "reject"


def test_any_axis_one_after_low_signal_check_does_not_promote():
    """Rule order: legitimacy → low_signal → any-1-reject. low_signal wins over any-1.

    This is intentional per spec D-2.5: a job with insufficient JD signal cannot
    be confidently rejected on rubric outputs (the 1 may itself be a hallucination
    from the model trying to score against an empty prompt). Surfacing low_signal
    is the honest answer.
    """
    sub_scores = {
        "title_fit": 1,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
    }
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier="exhausted",
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


# --- Coverage for the terminal-tier set (issue #225) -------------------------
#
# The low_signal rule must fire for every tier from which no further automatic
# enrichment will run. The agentic enricher flips 'exhausted' to 'agentic' on
# success and 'agentic_exhausted' on failure, both of which are also terminal.

_TERMINAL_TIERS = ("exhausted", "agentic", "agentic_exhausted")


@pytest.mark.parametrize("tier", _TERMINAL_TIERS)
def test_low_signal_fires_for_terminal_tier_with_short_jd(tier):
    # Strong (non-flat) vector isolates the enrichment branch from branch C.
    cls = derive_classification(
        sub_scores=dict(_STRONG),
        legitimacy_note=None,
        enrichment_tier=tier,
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


@pytest.mark.parametrize("tier", _TERMINAL_TIERS)
def test_terminal_tier_short_jd_with_hallucinated_one_is_low_signal_not_reject(tier):
    """Acceptance criterion: a short-JD terminal-tier row with a hallucinated 1
    sub-score must classify as low_signal, NOT reject. The low_signal branch
    sits before the any-axis-1 reject precisely to absorb rubric noise on
    insufficient-context jobs.
    """
    sub_scores = {
        "title_fit": 1,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
    }
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier=tier,
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


@pytest.mark.parametrize("tier", _TERMINAL_TIERS)
def test_terminal_tier_long_jd_classifies_on_normal_rubric(tier):
    """Long-JD rows at a terminal tier follow the standard rubric (not low_signal)."""
    cls = derive_classification(
        sub_scores=dict(_STRONG),
        legitimacy_note=None,
        enrichment_tier=tier,
        jd_full_length=5000,
        low_signal_threshold=1500,
    )
    assert cls == "apply"


@pytest.mark.parametrize("tier", ("free", "ddg", "low", "serpapi", "mid"))
def test_non_terminal_tier_short_jd_is_not_low_signal(tier):
    """Non-terminal tiers with short JDs are re-enrichment candidates and must
    not be diverted to low_signal — preserves the original
    test_not_low_signal_when_enrichment_not_exhausted contract across the
    full non-terminal vocabulary.
    """
    cls = derive_classification(
        sub_scores=dict(_STRONG),
        legitimacy_note=None,
        enrichment_tier=tier,
        jd_full_length=500,
        low_signal_threshold=1500,
    )
    assert cls == "apply"


# --- Flat-neutral branch (issue #210 branch C) -------------------------------
#
# An all-3s vector is degenerate at the "couldn't tell" midpoint and must be
# surfaced as low_signal regardless of JD length or enrichment_tier — it does
# not depend on the enrichment-string match, so it also covers the agentic-tier
# cohort that the exact-string branch (2) misses (issue #225).


def test_flat_neutral_is_low_signal_independent_of_jd_length():
    """All-3s with a long JD and no terminal enrichment -> low_signal, not apply."""
    cls = derive_classification(
        sub_scores=dict(_NEUTRAL),
        legitimacy_note=None,
        enrichment_tier=None,
        jd_full_length=8000,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


@pytest.mark.parametrize(
    "tier", (None, "free", "ddg", "low", "serpapi", "mid", "exhausted", "agentic")
)
def test_flat_neutral_is_low_signal_across_all_tiers(tier):
    """Flat-neutral -> low_signal regardless of enrichment_tier (covers #225)."""
    cls = derive_classification(
        sub_scores=dict(_NEUTRAL),
        legitimacy_note=None,
        enrichment_tier=tier,
        jd_full_length=8000,
        low_signal_threshold=1500,
    )
    assert cls == "low_signal"


# ---------------------------------------------------------------------------
# Issue #227: degenerate flag routes to low_signal (belt-and-braces with the
# upstream cascade quality floor).
# ---------------------------------------------------------------------------

_ALL_FIVES = dict.fromkeys(
    (
        "title_fit",
        "location_fit",
        "comp_fit",
        "domain_match",
        "seniority_match",
        "skills_match",
    ),
    5,
)


def test_degenerate_uniform_high_vector_routes_low_signal_not_apply():
    """A degenerate all-5s vector (no-signal) must classify low_signal, not apply."""
    cls = derive_classification(
        sub_scores=dict(_ALL_FIVES),
        legitimacy_note=None,
        degenerate=True,
    )
    assert cls == "low_signal"


def test_flat_neutral_legitimacy_note_still_wins():
    """legitimacy_note precedence holds over the flat-neutral branch."""
    cls = derive_classification(
        sub_scores=dict(_NEUTRAL),
        legitimacy_note="scam pattern",
        enrichment_tier=None,
        jd_full_length=8000,
        low_signal_threshold=1500,
    )
    assert cls == "reject"


# --- Positive-evidence apply branch (issue #210 branch B) --------------------


def test_strong_vector_is_apply():
    """A genuinely strong vector (>= 3 strong axes, mean >= 3.5) -> apply."""
    cls = derive_classification(
        sub_scores={
            "title_fit": 5,
            "location_fit": 4,
            "comp_fit": 4,
            "domain_match": 5,
            "seniority_match": 4,
            "skills_match": 4,
        },
        legitimacy_note=None,
        enrichment_tier=None,
        jd_full_length=8000,
        low_signal_threshold=1500,
    )
    assert cls == "apply"


def test_non_degenerate_same_vector_classifies_apply():
    """The same all-5s vector WITHOUT the degenerate flag is a normal apply."""
    cls = derive_classification(
        sub_scores=dict(_ALL_FIVES),
        legitimacy_note=None,
        degenerate=False,
    )
    assert cls == "apply"


@pytest.mark.parametrize(
    "sub_scores,expected",
    [
        # mean exactly 3.5 but only 2 strong axes (< 3) -> consider, not apply.
        (
            {
                "title_fit": 5,
                "location_fit": 4,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
            },
            "consider",
        ),
        # one strong axis, mean 3.17 -> consider.
        (
            {
                "title_fit": 4,
                "location_fit": 3,
                "comp_fit": 3,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
            },
            "consider",
        ),
        # exactly 3 strong axes AND mean 3.5 -> apply (lower boundary).
        (
            {
                "title_fit": 4,
                "location_fit": 4,
                "comp_fit": 4,
                "domain_match": 3,
                "seniority_match": 3,
                "skills_match": 3,
            },
            "apply",
        ),
        # 3 strong axes but mean 3.33 (< 3.5) -> consider (mean floor binds).
        (
            {
                "title_fit": 4,
                "location_fit": 4,
                "comp_fit": 4,
                "domain_match": 2,
                "seniority_match": 3,
                "skills_match": 3,
            },
            "consider",
        ),
    ],
)
def test_apply_boundary_requires_strength_and_mean(sub_scores, expected):
    """apply requires BOTH >= apply_min_strong_axes strong axes AND mean >= floor."""
    cls = derive_classification(
        sub_scores=sub_scores,
        legitimacy_note=None,
        enrichment_tier=None,
        jd_full_length=8000,
        low_signal_threshold=1500,
    )
    assert cls == expected


def test_apply_thresholds_are_configurable():
    """Lowering apply_min_strong_axes / apply_mean_floor promotes a weaker vector."""
    # {4,3,3,3,3,3}: mean 3.17, 1 strong axis. Default -> consider.
    weak = {
        "title_fit": 4,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
    }
    assert derive_classification(weak, None, jd_full_length=8000) == "consider"
    # With relaxed knobs (1 strong axis, mean floor 3.0) it becomes apply.
    assert (
        derive_classification(
            weak,
            None,
            jd_full_length=8000,
            apply_mean_floor=3.0,
            apply_min_strong_axes=1,
        )
        == "apply"
    )


def test_legitimacy_reject_beats_degenerate():
    """A flagged scam is still a reject — legitimacy precedence wins over degenerate."""
    cls = derive_classification(
        sub_scores=dict(_ALL_FIVES),
        legitimacy_note="scam pattern",
        degenerate=True,
    )
    assert cls == "reject"


def test_degenerate_independent_of_jd_length():
    """A degenerate score is no-signal even with a long JD (unlike the enrichment rule)."""
    cls = derive_classification(
        sub_scores=dict(_ALL_FIVES),
        legitimacy_note=None,
        enrichment_tier="free",
        jd_full_length=50_000,
        degenerate=True,
    )
    assert cls == "low_signal"

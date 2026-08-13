"""Axis-exclusion mechanism for derive_classification.

Exclusion is substitution-with-a-marker plus a parallel excluded-axis set,
never key removal — the six-key domain guard forbids partial vectors on
purpose. The sabotage tests here fail if the mechanism is ever "simplified"
to key removal, or if the substituted marker starts leaking into the mean /
strong-axis / flat-neutral arithmetic.

The invariant test pins the mislabel class where a profile carrying no
location constraint (hence no stored location-policy verdict) gets a posting
rejected on a location_fit score the model produced against nothing.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.classification import (
    derive_classification,
    effective_sub_scores,
)

# Pinned literals (not imported from the module under test — a test that
# re-derives its inputs from the constant it guards can't catch drift).
_ALL_KEYS = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


def _vector(**overrides: int) -> dict:
    base: dict = dict.fromkeys(_ALL_KEYS, 3)
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Substitution mechanics: six keys always intact, input never mutated
# ---------------------------------------------------------------------------


def test_exclusion_preserves_six_keys_and_never_mutates_input():
    scores = _vector(location_fit=1, title_fit=4, domain_match=4)
    snapshot = dict(scores)
    derive_classification(scores, None, excluded_axes={"location_fit"})
    assert scores == snapshot  # marker substituted into a NEW dict only
    assert set(scores) == set(_ALL_KEYS)


def test_raw_value_in_excluded_slot_is_still_validated():
    # Exclusion does not launder garbage: the value guard runs on the raw
    # vector before any substitution.
    scores = _vector(location_fit=0)
    with pytest.raises(ValueError, match=r"values must be int in 1\.\.5"):
        derive_classification(scores, None, excluded_axes={"location_fit"})


# ---------------------------------------------------------------------------
# The excluded axis never moves the mean or the strong-axis count.
# Two fixtures straddle the apply/consider boundary so a leak in either
# direction flips the expected label:
#   - included [4,4,4,3,3]: strong=3, mean=3.6  -> "apply". A raw 1 leaking
#     into the any-axis-1 check -> "reject"; a raw 1..2 leaking into the
#     mean drags it below 3.5 -> "consider". Both would fail the assert.
#   - included [4,4,3,3,3]: strong=2, mean=3.4  -> "consider". A raw 4..5
#     leaking into strong/mean lifts it over both floors -> "apply".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw_location_fit", [1, 2, 3, 4, 5])
def test_excluded_axis_cannot_drag_vector_below_apply(raw_location_fit: int):
    scores = _vector(title_fit=4, comp_fit=4, domain_match=4, location_fit=raw_location_fit)
    result = derive_classification(scores, None, excluded_axes={"location_fit"})
    assert result == "apply"


@pytest.mark.parametrize("raw_location_fit", [1, 2, 3, 4, 5])
def test_excluded_axis_cannot_push_vector_over_apply(raw_location_fit: int):
    scores = _vector(title_fit=4, comp_fit=4, location_fit=raw_location_fit)
    result = derive_classification(scores, None, excluded_axes={"location_fit"})
    assert result == "consider"


# ---------------------------------------------------------------------------
# Flat-neutral is a six-REAL-axis pattern
# ---------------------------------------------------------------------------


def test_all_threes_manufactured_by_substitution_is_not_low_signal():
    """Raw vector carries a real signal (location_fit=1) that exclusion
    substitutes to the neutral marker, making the literal vector all-3s. A
    marker-manufactured all-3s must not silently convert a real verdict into
    low_signal; only a vector whose six REAL axes all sit at the midpoint is
    the no-discrimination tell."""
    scores = _vector(location_fit=1)
    result = derive_classification(scores, None, excluded_axes={"location_fit"})
    assert result != "low_signal"
    assert result == "consider"


def test_genuine_all_threes_without_exclusion_stays_low_signal():
    # Companion control: the suppression above is scoped to exclusions.
    assert derive_classification(_vector(), None) == "low_signal"


# ---------------------------------------------------------------------------
# Sabotage: break the mechanism, confirm the guard fires
# ---------------------------------------------------------------------------


def test_sabotage_key_removal_exclusion_raises():
    """A sabotaged exclusion path that removes the key instead of
    substituting must die at the six-key domain guard, never classify a
    partial vector — with or without excluded_axes naming the removed key."""
    scores = _vector(location_fit=1)
    scores.pop("location_fit")
    with pytest.raises(ValueError, match="missing keys"):
        derive_classification(scores, None)
    with pytest.raises(ValueError, match="missing keys"):
        derive_classification(scores, None, excluded_axes={"location_fit"})


# ---------------------------------------------------------------------------
# Invariant: a profile with no location constraint can never yield reject
# via location_fit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location_fit", [1, 2, 3, 4, 5])
def test_no_location_constraint_never_rejects_via_location_fit(location_fit: int):
    """No location constraint means no stored location-policy verdict:
    effective_sub_scores passes the vector through unchanged, and the scoring
    caller excludes the axis the model scored against nothing. No raw value
    1..5 may then produce a reject via location_fit (every other axis here is
    >= 3, so any reject would be location-caused)."""
    scores = effective_sub_scores(
        _vector(title_fit=4, domain_match=4, location_fit=location_fit), None
    )
    result = derive_classification(scores, None, excluded_axes={"location_fit"})
    assert result != "reject"


def test_positive_control_unexcluded_raw_one_still_rejects():
    # The parametrization above can only prove the invariant if a raw 1 on an
    # UNexcluded location_fit axis does reject.
    scores = _vector(title_fit=4, domain_match=4, location_fit=1)
    assert derive_classification(scores, None) == "reject"


# ---------------------------------------------------------------------------
# excluded_axes domain validation
# ---------------------------------------------------------------------------


def test_unknown_excluded_axis_raises():
    with pytest.raises(ValueError, match="unknown axes"):
        derive_classification(_vector(title_fit=4), None, excluded_axes={"nonexistent_axis"})


def test_excluding_all_six_axes_raises():
    with pytest.raises(ValueError, match="all six axes"):
        derive_classification(_vector(title_fit=4), None, excluded_axes=set(_ALL_KEYS))


# ---------------------------------------------------------------------------
# Parity: an empty exclusion set is byte-for-byte the pre-existing behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        (dict.fromkeys(_ALL_KEYS, 5), "apply"),
        (dict.fromkeys(_ALL_KEYS, 3), "low_signal"),
        (dict.fromkeys(_ALL_KEYS, 2), "consider"),
        ({**dict.fromkeys(_ALL_KEYS, 4), "title_fit": 1}, "reject"),
    ],
)
def test_empty_exclusion_set_is_parity_with_default(scores: dict, expected: str):
    assert derive_classification(scores, None) == expected
    assert derive_classification(scores, None, excluded_axes=()) == expected
    assert derive_classification(scores, None, excluded_axes=frozenset()) == expected

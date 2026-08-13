"""Domain guard tests for derive_classification (issue #257).

Validates that:
  - Malformed sub-score dicts raise ValueError (wrong/missing/extra keys,
    out-of-range values, non-int values including bool).
  - Valid 6-key 1–5 vectors still return the expected label unchanged.
  - The legitimacy_note and low_signal short-circuits still return without
    raising even when sub_scores is empty (they fire before the guard).

Ported from the private repo's tests/test_derive_classification_domain.py.
The redrive-script tests (scripts.redrive_classification batch reconciliation
over a live DB) are NOT ported — that script lives outside this task's
manifest.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.classification import (
    derive_classification,
    effective_sub_scores,
    get_effective_location_fit,
    is_non_degenerate_low_signal,
)

_VALID = {
    "title_fit": 4,
    "location_fit": 4,
    "comp_fit": 4,
    "domain_match": 4,
    "seniority_match": 4,
    "skills_match": 4,
}

_ALL_KEYS = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


# ---------------------------------------------------------------------------
# Malformed input must raise ValueError
# ---------------------------------------------------------------------------


def test_empty_dict_raises():
    with pytest.raises(ValueError, match="missing keys"):
        derive_classification({}, None)


def test_partial_dict_raises():
    partial = {"title_fit": 5, "comp_fit": 4}
    with pytest.raises(ValueError, match="missing keys"):
        derive_classification(partial, None)


def test_extra_key_raises():
    extra = dict(_VALID)
    extra["bonus_key"] = 3
    with pytest.raises(ValueError, match="extra keys"):
        derive_classification(extra, None)


def test_wrong_keys_raises():
    wrong = {k.upper(): v for k, v in _VALID.items()}
    with pytest.raises(ValueError, match="missing keys"):
        derive_classification(wrong, None)


@pytest.mark.parametrize("bad_value", [0, 6, 9, -1])
def test_out_of_range_int_raises(bad_value: int):
    bad = dict(_VALID)
    bad["title_fit"] = bad_value
    with pytest.raises(ValueError, match=r"values must be int in 1\.\.5"):
        derive_classification(bad, None)


@pytest.mark.parametrize("bad_value", ["5", 3.0, True, False, None])
def test_non_int_value_raises(bad_value):
    bad = dict(_VALID)
    bad["comp_fit"] = bad_value
    with pytest.raises(ValueError, match=r"values must be int in 1\.\.5"):
        derive_classification(bad, None)


# bool is an int subclass — both True and False must be rejected
def test_bool_true_raises():
    bad = dict(_VALID)
    bad["skills_match"] = True  # True == 1 as int but is bool
    with pytest.raises(ValueError, match=r"values must be int in 1\.\.5"):
        derive_classification(bad, None)


def test_bool_false_raises():
    bad = dict(_VALID)
    bad["skills_match"] = False  # False == 0 as int but is bool
    with pytest.raises(ValueError, match=r"values must be int in 1\.\.5"):
        derive_classification(bad, None)


# ---------------------------------------------------------------------------
# Valid vectors still produce the expected label (byte-for-byte unchanged)
# ---------------------------------------------------------------------------


def test_all_fives_returns_apply():
    scores = dict.fromkeys(_ALL_KEYS, 5)
    assert derive_classification(scores, None) == "apply"


def test_has_a_one_returns_reject():
    scores = dict(_VALID)
    scores["title_fit"] = 1
    assert derive_classification(scores, None) == "reject"


def test_all_twos_returns_consider():
    scores = dict.fromkeys(_ALL_KEYS, 2)
    assert derive_classification(scores, None) == "consider"


def test_all_threes_returns_low_signal():
    # Flat-neutral vector is no-signal, not apply (issue #210 branch C).
    scores = dict.fromkeys(_ALL_KEYS, 3)
    assert derive_classification(scores, None) == "low_signal"


# ---------------------------------------------------------------------------
# Short-circuit branches still fire on empty sub_scores without raising
# ---------------------------------------------------------------------------


def test_legitimacy_note_short_circuit_does_not_raise_on_empty():
    # legitimacy_note fires BEFORE the domain guard — must not raise
    result = derive_classification({}, "scam pattern")
    assert result == "reject"


def test_low_signal_short_circuit_does_not_raise_on_empty():
    # low_signal fires BEFORE the domain guard — must not raise
    result = derive_classification(
        {},
        None,
        enrichment_tier="exhausted",
        jd_full_length=100,
        low_signal_threshold=1500,
    )
    assert result == "low_signal"


# ---------------------------------------------------------------------------
# get_effective_location_fit — single parsing point for the policy override.
# A value that is not a genuine int (bool is an int subclass; "4" and 4.0 are
# not ints; malformed JSON parses to nothing) must yield None, never a
# coerced score. One assertion per case.
# ---------------------------------------------------------------------------


def test_get_effective_location_fit_bool_true_is_none():
    assert get_effective_location_fit('{"effective_location_fit": true}') is None


def test_get_effective_location_fit_string_digit_is_none():
    assert get_effective_location_fit('{"effective_location_fit": "4"}') is None


def test_get_effective_location_fit_float_is_none():
    assert get_effective_location_fit('{"effective_location_fit": 4.0}') is None


def test_get_effective_location_fit_malformed_json_is_none():
    assert get_effective_location_fit("{malformed") is None


def test_get_effective_location_fit_none_verdict_is_none():
    assert get_effective_location_fit(None) is None


def test_get_effective_location_fit_valid_int_returns_it():
    # Positive control: the None cases above are only meaningful because a
    # well-formed verdict DOES parse to its int.
    assert get_effective_location_fit('{"effective_location_fit": 4}') == 4


def test_get_effective_location_fit_non_dict_json_is_none():
    assert get_effective_location_fit("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# effective_sub_scores — swaps location_fit to the policy's effective value,
# or returns the original dict unchanged when no valid verdict exists.
# ---------------------------------------------------------------------------


def test_effective_sub_scores_no_verdict_equals_input():
    scores = dict(_VALID)
    assert effective_sub_scores(scores, None) == scores


def test_effective_sub_scores_malformed_verdict_equals_input():
    scores = dict(_VALID)
    assert effective_sub_scores(scores, "{malformed") == scores


def test_effective_sub_scores_substitutes_only_location_fit():
    scores = dict(_VALID)  # location_fit == 4
    swapped = effective_sub_scores(scores, '{"effective_location_fit": 2}')
    assert swapped["location_fit"] == 2
    others = {k: v for k, v in swapped.items() if k != "location_fit"}
    assert others == {k: v for k, v in scores.items() if k != "location_fit"}
    # New dict, input never mutated.
    assert scores["location_fit"] == 4


# ---------------------------------------------------------------------------
# is_non_degenerate_low_signal — shared rule for the two genuine low_signal
# paths (terminal enrichment + short JD; flat-neutral vector).
# ---------------------------------------------------------------------------


def test_is_non_degenerate_low_signal_terminal_tier_short_jd():
    assert is_non_degenerate_low_signal(dict(_VALID), "exhausted", 100, 1500) is True


def test_is_non_degenerate_low_signal_flat_neutral_vector():
    scores = dict.fromkeys(_ALL_KEYS, 3)
    assert is_non_degenerate_low_signal(scores, None, 5000, 1500) is True


def test_is_non_degenerate_low_signal_false_with_signal_and_text():
    assert is_non_degenerate_low_signal(dict(_VALID), "free", 5000, 1500) is False

"""Pins `get_agentic_exhausted_retry_policy`'s value-matrix behaviour byte-for-byte
against private `job_finder/config.py` @307c369c0688763a18c6989adb81b229928e20d0.

Refuter round 1 (PR #378, B1) caught a rewrite: an earlier version of the inlined
helper (`jobcannon/engine/agentic_enricher.py`) clamped negative config values to
0 instead of falling back to the DEFAULT_AGENTIC_* constants, and swallowed
non-numeric config values (`try/except (TypeError, ValueError)`) instead of
letting them raise -- both diverging from private while the in-file PORT-SEAM
comment claimed a verbatim copy. This test pins the two behaviours the review's
value matrix exercised, so a future edit that reintroduces either divergence
fails loudly instead of only tripping the fidelity-diff tool's seam-edit
allowlist (which does not check inlined-block byte-identity, only comment
presence -- see the review's closing note)."""

from __future__ import annotations

import pytest

from jobcannon.engine.agentic_enricher import (
    DEFAULT_AGENTIC_EXHAUSTED_COOLDOWN_DAYS,
    DEFAULT_AGENTIC_EXHAUSTED_MAX_RETRIES,
    get_agentic_exhausted_retry_policy,
)


def test_none_config_returns_defaults():
    assert get_agentic_exhausted_retry_policy(None) == (
        DEFAULT_AGENTIC_EXHAUSTED_COOLDOWN_DAYS,
        DEFAULT_AGENTIC_EXHAUSTED_MAX_RETRIES,
    )


def test_empty_config_returns_defaults():
    assert get_agentic_exhausted_retry_policy({}) == (
        DEFAULT_AGENTIC_EXHAUSTED_COOLDOWN_DAYS,
        DEFAULT_AGENTIC_EXHAUSTED_MAX_RETRIES,
    )


def test_negative_values_fall_back_to_default_not_zero():
    """Private clamps a negative value to the DEFAULT constant, NOT to 0. A
    public rewrite once clamped to 0 instead -- a 0-day cooldown means "retry
    every sweep," the exact always-satisfied outcome the docstring says the
    clamp prevents. Pin the DEFAULT fallback, not a 0 floor."""
    config = {"agentic": {"retry_cooldown_days": -5, "retry_max_attempts": -1}}
    result = get_agentic_exhausted_retry_policy(config)
    assert result == (
        DEFAULT_AGENTIC_EXHAUSTED_COOLDOWN_DAYS,
        DEFAULT_AGENTIC_EXHAUSTED_MAX_RETRIES,
    )
    assert result != (0, 0)


def test_non_numeric_value_raises_valueerror():
    """Private lets a non-numeric config value raise ValueError from int(...) --
    it does not catch and fall back. A public rewrite once added a
    try/except (TypeError, ValueError) around both int() calls, silently
    swallowing malformed config instead of surfacing it. Pin the raise."""
    config = {"agentic": {"retry_cooldown_days": "abc"}}
    with pytest.raises(ValueError):
        get_agentic_exhausted_retry_policy(config)


def test_non_numeric_max_retries_also_raises_valueerror():
    config = {"agentic": {"retry_max_attempts": "not-a-number"}}
    with pytest.raises(ValueError):
        get_agentic_exhausted_retry_policy(config)


def test_positive_values_pass_through():
    config = {"agentic": {"retry_cooldown_days": 7, "retry_max_attempts": 5}}
    assert get_agentic_exhausted_retry_policy(config) == (7, 5)

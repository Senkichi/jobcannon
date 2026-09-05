# PORTED from tests/test_agentic_batch_limit.py @ 4db226bf648b401bcb95dac74be3088ac1455e9c (private job-cannon). Ledger L-0597.
"""Unit tests for the config-driven agentic backfill cap.

Covers `_resolve_batch_limit`: the per-run job cap is now read from
`agentic.batch_limit` (default 50) when the scheduled job passes no explicit
limit, while an explicit limit (one-shot CLI) still overrides config.

No network / DB access.
"""

from __future__ import annotations

from jobcannon.engine.agentic_enricher import DEFAULT_AGENTIC_BATCH_LIMIT, _resolve_batch_limit


def test_default_when_absent():
    """Empty/absent config → centralized default."""
    assert _resolve_batch_limit({}, None) == DEFAULT_AGENTIC_BATCH_LIMIT
    assert _resolve_batch_limit({"agentic": {}}, None) == DEFAULT_AGENTIC_BATCH_LIMIT


def test_config_value_honored():
    """agentic.batch_limit drives the cap on the scheduled-job path (limit=None)."""
    assert _resolve_batch_limit({"agentic": {"batch_limit": 150}}, None) == 150
    assert _resolve_batch_limit({"agentic": {"batch_limit": 1}}, None) == 1


def test_explicit_limit_overrides_config():
    """An explicit limit (one-shot CLI) always wins over config."""
    assert _resolve_batch_limit({"agentic": {"batch_limit": 150}}, 10) == 10
    assert _resolve_batch_limit({}, 25) == 25


def test_malformed_config_falls_back_to_default():
    """A non-int batch_limit must not crash the nightly run."""
    assert _resolve_batch_limit({"agentic": {"batch_limit": "oops"}}, None) == (
        DEFAULT_AGENTIC_BATCH_LIMIT
    )


def test_string_int_coerced():
    """YAML may yield a stringy number; it is coerced, not rejected."""
    assert _resolve_batch_limit({"agentic": {"batch_limit": "75"}}, None) == 75

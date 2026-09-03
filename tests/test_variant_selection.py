"""Tests: scoring resolves the prompt+schema variant from scoring.prompt_variant config.

Phase 4 Task 4.1 — wires the config knob that lets the eval harness (and
production) swap the v3.0 baseline rubric for an experimental variant
without code edits.

Contract:
    config["scoring"]["prompt_variant"] == "baseline"  -> v3_scoring_prompt module
    config["scoring"]["prompt_variant"] == "<name>"    -> scoring_prompts.variants.<name>
    config["scoring"]["prompt_variant"] is missing     -> baseline (default)
    Unknown variant name                                 -> ImportError mentioning the name
"""

from __future__ import annotations

import sys
import types

import pytest

from jobcannon.engine.job_scorer import (
    _build_system_prompt,
    _resolve_schema,
    _resolve_variant_module,
)

# ---------------------------------------------------------------------------
# Variant resolution
# ---------------------------------------------------------------------------


def test_baseline_variant_resolves_to_v3_module():
    """`baseline` aliases the production v3 prompt module."""
    mod = _resolve_variant_module("baseline")
    from jobcannon.engine.scoring_prompts import v3_scoring_prompt as v3

    assert mod is v3


_TEST_CTX = "## Candidate context\n\n### Targeting\n- Target titles: Test Analyst"


def test_missing_variant_name_defaults_to_baseline():
    """An empty/None config picks baseline so existing callers are unaffected."""
    prompt = _build_system_prompt(candidate_context=_TEST_CTX, config=None)
    # Baseline header signature
    assert "Six dimensions — 1-5 integer scale" in prompt
    assert "STRICT FIELD NAMES" in prompt


def test_named_variant_module_loaded(monkeypatch):
    """A planted variant module is resolved when named in config.

    Verifies the splice path (uses V3_SCORING_PROMPT_HEADER + ctx) and the
    schema swap. The no-context aggregate path was removed when
    candidate_context became required — the rubric is unscorable without
    candidate facts (target locations, comp floor, etc.).
    """
    fake_mod = types.ModuleType("jobcannon.engine.scoring_prompts.variants.fixture_variant_v4test")
    fake_mod.V3_SCORING_PROMPT = "FIXTURE_PROMPT_MARKER"
    fake_mod.V3_SCORING_PROMPT_HEADER = "FIXTURE_HEADER_MARKER"
    fake_mod.FIELD_REINFORCEMENT = "FIXTURE_FIELD_MARKER"
    fake_mod.FEWSHOT_EXAMPLES = "FIXTURE_FEWSHOT_MARKER"
    fake_mod.JOB_ASSESSMENT_SCHEMA = {"type": "object", "fixture": True}
    monkeypatch.setitem(
        sys.modules,
        "jobcannon.engine.scoring_prompts.variants.fixture_variant_v4test",
        fake_mod,
    )

    config = {"scoring": {"prompt_variant": "fixture_variant_v4test"}}

    spliced = _build_system_prompt(candidate_context="CTX_MARKER", config=config)
    assert "FIXTURE_HEADER_MARKER" in spliced
    assert "FIXTURE_FIELD_MARKER" in spliced
    assert "CTX_MARKER" in spliced
    assert "FIXTURE_FEWSHOT_MARKER" in spliced

    # Schema swap.
    assert _resolve_schema(config) == {"type": "object", "fixture": True}


def test_unknown_variant_raises_clear_error():
    """An ImportError naming the variant — never a silent fallback to baseline."""
    config = {"scoring": {"prompt_variant": "does_not_exist_v4xxx"}}
    with pytest.raises(ImportError, match="does_not_exist_v4xxx"):
        _build_system_prompt(candidate_context=_TEST_CTX, config=config)
    with pytest.raises(ImportError, match="does_not_exist_v4xxx"):
        _resolve_schema(config)


def test_missing_candidate_context_raises():
    """The orchestrator MUST resolve a context; an empty string is a bug."""
    with pytest.raises(ValueError, match="candidate_context is required"):
        _build_system_prompt(candidate_context="", config=None)
    with pytest.raises(ValueError, match="candidate_context is required"):
        _build_system_prompt(candidate_context=None, config=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Splice ordering under the baseline variant
# ---------------------------------------------------------------------------


def test_candidate_context_splice_ordering_under_baseline_variant():
    """The Phase 2a splice (FIELD -> ctx -> FEWSHOT) is preserved under baseline."""
    ctx = "## Candidate context\n\n### Targeting\n- Target titles: Foo Analyst"
    prompt = _build_system_prompt(
        candidate_context=ctx, config={"scoring": {"prompt_variant": "baseline"}}
    )

    fr_idx = prompt.find("STRICT FIELD NAMES")
    fs_idx = prompt.find("Fewshot calibration examples")
    ctx_idx = prompt.find("## Candidate context")
    assert fr_idx >= 0 and fs_idx >= 0 and ctx_idx >= 0
    assert fr_idx < ctx_idx < fs_idx


def test_resolve_schema_returns_baseline_schema_by_default():
    """No config -> baseline schema -> the production JOB_ASSESSMENT_SCHEMA."""
    from jobcannon.engine.scoring_prompts.v3_scoring_prompt import JOB_ASSESSMENT_SCHEMA

    assert _resolve_schema(None) is JOB_ASSESSMENT_SCHEMA
    assert _resolve_schema({}) is JOB_ASSESSMENT_SCHEMA
    assert _resolve_schema({"scoring": {}}) is JOB_ASSESSMENT_SCHEMA

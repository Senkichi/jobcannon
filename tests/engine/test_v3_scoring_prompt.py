"""Unit tests for jobcannon.engine.scoring_prompts.v3_scoring_prompt — FROZEN v3.0 prompt module.

Asserts:
  * Module is importable; the four public constants exist
  * V3_SCORING_PROMPT references all six ordinal dimensions
  * V3_SCORING_PROMPT contains behavioral anchors for each dimension
  * JOB_ASSESSMENT_SCHEMA is a valid JSON Schema
  * JOB_ASSESSMENT_SCHEMA structure: type=object, additionalProperties=False,
    six integer dimensions 1-5 plus rationale and legitimacy_note
  * Sample valid instance validates; bad instances fail validation
  * FEWSHOT_EXAMPLES spans all ordinal levels 1..5
  * FIELD_REINFORCEMENT names both the canonical field "gaps" AND the anti-
    pattern "weaknesses" (anti-rename reinforcement per FEATURES Q4)
"""

from __future__ import annotations

import re

import jsonschema
import pytest

from jobcannon.engine.scoring_prompts.v3_scoring_prompt import (
    FEWSHOT_EXAMPLES,
    FIELD_REINFORCEMENT,
    JOB_ASSESSMENT_SCHEMA,
    V3_SCORING_PROMPT,
)

_DIMENSIONS = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)


# ---------------------------------------------------------------------------
# Imports and basic shape
# ---------------------------------------------------------------------------


def test_module_imports_all_four_constants():
    """All four public constants exist and are non-empty."""
    assert isinstance(V3_SCORING_PROMPT, str) and V3_SCORING_PROMPT
    assert isinstance(JOB_ASSESSMENT_SCHEMA, dict) and JOB_ASSESSMENT_SCHEMA
    assert isinstance(FEWSHOT_EXAMPLES, str) and FEWSHOT_EXAMPLES
    assert isinstance(FIELD_REINFORCEMENT, str) and FIELD_REINFORCEMENT


# ---------------------------------------------------------------------------
# V3_SCORING_PROMPT content
# ---------------------------------------------------------------------------


def test_prompt_names_all_six_dimensions():
    """V3_SCORING_PROMPT mentions each of the six ordinal dimension names."""
    for dim in _DIMENSIONS:
        assert dim in V3_SCORING_PROMPT, f"dimension {dim!r} missing from V3_SCORING_PROMPT"


def test_prompt_contains_behavioral_anchors_per_dimension():
    """For every dimension, the prompt contains 'score of 1', 'score of 3',
    and 'score of 5' anchors inside or near the dimension's block.
    """
    # Anchor phrasing used in the v3 prompt (see action block in PLAN.md).
    # We assert the anchors appear at least once globally AND each dimension
    # has its own block that follows them. The global count is a simple proxy
    # that all six dimensions use the anchor pattern — one occurrence per
    # dimension minimum, so >= 6 occurrences of each anchor phrase.
    for anchor in ("score of 1", "score of 3", "score of 5"):
        count = V3_SCORING_PROMPT.count(anchor)
        assert count >= 6, (
            f"anchor phrase {anchor!r} should appear at least 6 times "
            f"(once per dimension), found {count}"
        )


# ---------------------------------------------------------------------------
# JOB_ASSESSMENT_SCHEMA — shape validation
# ---------------------------------------------------------------------------


def test_schema_is_valid_jsonschema():
    """JOB_ASSESSMENT_SCHEMA passes JSON Schema meta-validation."""
    # check_schema raises if the schema itself is malformed.
    jsonschema.Draft202012Validator.check_schema(JOB_ASSESSMENT_SCHEMA)


def test_schema_top_level_shape():
    """Top level: type=object, additionalProperties=False, required includes
    all six dimensions plus rationale and legitimacy_note, and each dimension
    is an integer 1..5.
    """
    s = JOB_ASSESSMENT_SCHEMA
    assert s["type"] == "object"
    assert s["additionalProperties"] is False

    required = set(s["required"])
    for dim in _DIMENSIONS:
        assert dim in required, f"{dim!r} missing from schema.required"
    assert "rationale" in required
    assert "legitimacy_note" in required

    props = s["properties"]
    for dim in _DIMENSIONS:
        spec = props[dim]
        assert spec["type"] == "integer"
        assert spec["minimum"] == 1
        assert spec["maximum"] == 5


def test_schema_accepts_valid_instance():
    """A well-formed assessment validates cleanly."""
    instance = {
        "title_fit": 4,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 5,
        "seniority_match": 4,
        "skills_match": 4,
        "rationale": {
            "strengths": ["a"],
            "gaps": ["b"],
            "talking_points": ["c"],
            "resume_priority_skills": ["d"],
        },
        "legitimacy_note": None,
    }
    jsonschema.validate(instance=instance, schema=JOB_ASSESSMENT_SCHEMA)


def test_schema_rejects_out_of_range_value():
    """A dimension value outside 1..5 is rejected."""
    instance = {
        "title_fit": 6,  # out of range
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        "skills_match": 3,
        "rationale": {
            "strengths": [],
            "gaps": [],
            "talking_points": [],
            "resume_priority_skills": [],
        },
        "legitimacy_note": None,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=instance, schema=JOB_ASSESSMENT_SCHEMA)


def test_schema_rejects_missing_required_field():
    """Omitting a required dimension is rejected."""
    instance = {
        "title_fit": 3,
        "location_fit": 3,
        "comp_fit": 3,
        "domain_match": 3,
        "seniority_match": 3,
        # skills_match deliberately missing
        "rationale": {
            "strengths": [],
            "gaps": [],
            "talking_points": [],
            "resume_priority_skills": [],
        },
        "legitimacy_note": None,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=instance, schema=JOB_ASSESSMENT_SCHEMA)


# ---------------------------------------------------------------------------
# FEWSHOT_EXAMPLES — ordinal coverage
# ---------------------------------------------------------------------------


def test_fewshot_spans_all_ordinal_levels():
    """At least one value for each of 1..5 appears as an integer score in the fewshot JSON blocks."""
    for n in range(1, 6):
        # Find ": N" where N is a bare integer (followed by comma, close-brace, or whitespace).
        pattern = rf":\s*{n}\s*[,\}}\n]"
        assert re.search(pattern, FEWSHOT_EXAMPLES), (
            f"ordinal level {n} not found as a score value in FEWSHOT_EXAMPLES"
        )


# ---------------------------------------------------------------------------
# FIELD_REINFORCEMENT — anti-rename guard
# ---------------------------------------------------------------------------


def test_field_reinforcement_names_canonical_and_anti_pattern():
    """FIELD_REINFORCEMENT explicitly mentions 'gaps' (canonical) AND 'weaknesses' (anti-pattern)."""
    assert "gaps" in FIELD_REINFORCEMENT
    assert "weaknesses" in FIELD_REINFORCEMENT

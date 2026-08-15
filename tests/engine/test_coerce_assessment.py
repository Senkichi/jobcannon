"""Negative-path test pack for job_scorer._coerce_assessment.

Trimmed engine-native port of the private repo's
tests/test_malformed_llm_output.py (Phase 1A Task 4 deviation — see PR body).
That file's other half drives ``_sanitize_output``/``_validate_schema`` from
``job_finder.web.model_provider``, the provider-cascade dispatcher's own
sanitize/validate step — model_provider is a host module (Phase 1B ADAPT
target), not part of this engine port, so those tests do not port. Only the
tests exercising ``_coerce_assessment`` directly (job_scorer's own,
dispatcher-independent coercion step) are kept here, verbatim in behavior.

Purpose: pin the CURRENT observable behavior of _coerce_assessment so that
provider-shaped regressions (missing axis, wrapped score objects, etc.)
cause a test failure rather than silently producing a plausible-but-wrong
JobAssessment.

No live model or network calls — all inputs are crafted dicts.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.job_scorer import _coerce_assessment, _IncompleteAssessmentError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_RATIONALE = {
    "strengths": ["Good Python"],
    "gaps": [],
    "talking_points": [],
    "resume_priority_skills": [],
}

_VALID_PAYLOAD: dict = {
    "title_fit": 4,
    "location_fit": 3,
    "comp_fit": 3,
    "domain_match": 3,
    "seniority_match": 4,
    "skills_match": 4,
    "rationale": _VALID_RATIONALE,
    "legitimacy_note": None,
}


# ---------------------------------------------------------------------------
# Baseline: confirm valid payload coerces cleanly
# ---------------------------------------------------------------------------


def test_valid_payload_coerces_to_full_assessment():
    result = _coerce_assessment(_VALID_PAYLOAD, provider="ollama")
    assert set(result.sub_scores.keys()) == {
        "title_fit",
        "location_fit",
        "comp_fit",
        "domain_match",
        "seniority_match",
        "skills_match",
    }
    assert result.sub_scores["title_fit"] == 4
    assert result.provider == "ollama"


# ---------------------------------------------------------------------------
# Missing axis
# ---------------------------------------------------------------------------


def test_coerce_assessment_missing_axis_raises_incomplete():
    """_coerce_assessment is fail-closed.

    A missing axis raises ``_IncompleteAssessmentError`` rather than
    producing a partial sub-score vector that ``derive_classification``
    could read as a spurious ``apply``.
    """
    payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "comp_fit"}
    with pytest.raises(_IncompleteAssessmentError):
        _coerce_assessment(payload, provider="ollama")


# ---------------------------------------------------------------------------
# Out-of-range integers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [0, 6, -1])
def test_out_of_range_int_reaches_coerce_assessment_as_is(bad_value: int):
    """_coerce_assessment applies int() but does NOT clamp to [1,5].

    The value passes through; callers should validate before coercing.
    """
    payload = {**_VALID_PAYLOAD, "title_fit": bad_value}
    result = _coerce_assessment(payload, provider="test")
    assert result.sub_scores["title_fit"] == bad_value


# ---------------------------------------------------------------------------
# Verbose enum text
# ---------------------------------------------------------------------------


def test_coerce_assessment_verbose_string_axis_raises_incomplete():
    """_coerce_assessment calls int() on a verbose string → ValueError.

    Fail-closed: an uncoercible axis raises
    ``_IncompleteAssessmentError`` rather than being silently dropped.
    """
    payload = {**_VALID_PAYLOAD, "title_fit": "4 - strong match"}
    with pytest.raises(_IncompleteAssessmentError):
        _coerce_assessment(payload, provider="gemini")


# ---------------------------------------------------------------------------
# v4d2-style wrapped axes {"score": N, "evidence": "..."}
# ---------------------------------------------------------------------------


def _make_wrapped_payload() -> dict:
    return {
        "title_fit": {"score": 4, "evidence": "Direct role match"},
        "location_fit": {"score": 3, "evidence": "Remote"},
        "comp_fit": {"score": 3, "evidence": "In range"},
        "domain_match": {"score": 3, "evidence": "Adjacent"},
        "seniority_match": {"score": 4, "evidence": "Good level"},
        "skills_match": {"score": 4, "evidence": "Strong"},
        "rationale": _VALID_RATIONALE,
        "legitimacy_note": None,
    }


def test_coerce_assessment_unwraps_score_key():
    """_coerce_assessment unwraps {"score": N} and extracts the integer."""
    payload = _make_wrapped_payload()
    result = _coerce_assessment(payload, provider="ollama")
    assert result.sub_scores["title_fit"] == 4
    assert result.sub_scores["skills_match"] == 4
    assert len(result.sub_scores) == 6


def test_wrapped_missing_score_key_raises_incomplete():
    """{"evidence": "..."} without "score" is not a dict with "score" key.

    _coerce_assessment checks `isinstance(raw, dict) and "score" in raw`.
    A dict without "score" falls through to int(raw) → TypeError, which
    is fail-closed: it raises ``_IncompleteAssessmentError``.
    """
    payload = {**_VALID_PAYLOAD, "title_fit": {"evidence": "no score here"}}
    with pytest.raises(_IncompleteAssessmentError):
        _coerce_assessment(payload, provider="test")

"""Tests for candidate-context splicing in job_scorer.

Originally Phase 2a sub-fixes 2/3 and 3/3. The single-point-of-enforcement
refactor (June 2026) made ``candidate_context`` required at every layer:

    - _build_system_prompt: candidate_context REQUIRED — empty raises.
    - score_job: candidate_context REQUIRED in signature.
    - score_and_persist_job: callers do NOT pass candidate_context; the
      private repo's orchestrator resolves it from config via
      _resolve_candidate_context. That orchestrator is a host module (Phase
      1B ADAPT target) and is not part of this engine port, so its
      end-to-end test (test_orchestrator_passes_candidate_context_through in
      the private repo) is left behind — see Task 4 PR body.

Porting note (Phase 1A Task 4): score_job's call_model is now a required
keyword-only parameter rather than a module-level import, so
``fake_call_model`` below is passed directly via ``call_model=`` instead of
``monkeypatch.setattr("job_finder.web.job_scorer.call_model", ...)``. Its
envelope shape (data/cost_usd/input_tokens/output_tokens/model/provider/
schema_valid) is the authoritative source for the ``_canned_model`` fixture
in test_scoring_seam.py.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobcannon.engine.job_scorer import _build_system_prompt, score_job

from .conftest import FakeModelResult


def test_build_system_prompt_includes_candidate_context_when_provided():
    ctx = "## Candidate context\n\n### Targeting\n- Target titles: Foo Analyst"
    prompt = _build_system_prompt(candidate_context=ctx)
    assert "Foo Analyst" in prompt
    # Splice point: between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES
    fr_idx = prompt.find("STRICT FIELD NAMES")  # first line of FIELD_REINFORCEMENT
    fs_idx = prompt.find("Fewshot calibration examples")
    ctx_idx = prompt.find("## Candidate context")
    assert fr_idx >= 0, "FIELD_REINFORCEMENT must appear in spliced prompt"
    assert fs_idx >= 0, "FEWSHOT_EXAMPLES must appear in spliced prompt"
    assert ctx_idx >= 0, "Candidate context must appear in spliced prompt"
    assert fr_idx < ctx_idx < fs_idx, (
        "Candidate context must be spliced between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES"
    )


def test_build_system_prompt_rejects_empty_candidate_context():
    """The no-context fallback was removed — empty context is a programming bug.

    Before this refactor, six of seven scoring call sites passed ``None`` (the
    fallback) and produced wrong scores (e.g. Bangalore on-site rated 4/5
    for a Remote/SF candidate). Hard-failing here is the trip-wire that
    catches any future caller who forgets to thread the context.
    """
    with pytest.raises(ValueError, match="candidate_context is required"):
        _build_system_prompt(candidate_context="")
    with pytest.raises(ValueError, match="candidate_context is required"):
        _build_system_prompt(candidate_context=None)  # type: ignore[arg-type]


def test_score_job_threads_candidate_context_into_call_model():
    """Verify that score_job passes candidate_context through to call_model."""
    captured: dict = {}

    def fake_call_model(**kwargs):
        captured["system"] = kwargs.get("system", "")
        return FakeModelResult(
            data={
                "title_fit": 3,
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
            },
            cost_usd=0.0,
            input_tokens=100,
            output_tokens=50,
            model="qwen2.5:14b",
            provider="ollama",
            schema_valid=True,
        )

    conn = sqlite3.connect(":memory:")
    job = {
        "dedup_key": "x|y",
        "title": "T",
        "company": "C",
        "location": "Remote",
        "jd_full": "Long enough JD " * 50,
    }
    ctx = "## Candidate context\n- Target titles: Specific Role"
    result = score_job(job, conn, {}, candidate_context=ctx, call_model=fake_call_model)
    assert result.status == "ok"
    assert "Specific Role" in captured["system"]

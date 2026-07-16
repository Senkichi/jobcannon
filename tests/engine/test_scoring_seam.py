"""The scorer must be fully driveable by an injected model callable —
no provider cascade, no network, no config file."""

from __future__ import annotations

from types import SimpleNamespace

from jobcannon.engine.constants import SUB_SCORE_KEYS
from jobcannon.engine import job_scorer

_TEST_CTX = "## Candidate context\n\n### Targeting\n- Target titles: Test Role"


def _canned_model(*args, **kwargs):
    # call_model returns a ModelResult-shaped OBJECT (job_scorer reads
    # result.data / .schema_valid / .provider / .model / .degenerate), NOT a
    # JSON string. Axis keys come from the real SUB_SCORE_KEYS (title_fit,
    # location_fit, comp_fit, domain_match, seniority_match, skills_match) —
    # mirrors the fake_call_model in the private repo's
    # tests/test_job_scorer_profile_injection.py, which is the porting source.
    return SimpleNamespace(
        data={**{k: 3 for k in SUB_SCORE_KEYS}, "rationale": "canned", "legitimacy_note": "ok"},
        schema_valid=True,
        provider="fake",
        model="fake-model",
        degenerate=False,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
    )


def _good_job() -> dict:
    """Minimal job dict with a non-empty jd_full — same shape the ported
    job_scorer unit tests use (tests/engine/test_job_scorer.py)."""
    return {
        "dedup_key": "acme|senior-ml-engineer",
        "title": "Senior ML Engineer",
        "company": "Acme Corp",
        "company_canonical": "acme corp",
        "location": "Remote US",
        "salary_min": 180000,
        "salary_max": 260000,
        "jd_full": "Build scalable ML platforms. Python, PyTorch, AWS.",
    }


def test_score_with_injected_callable():
    """score_job is fully driveable via call_model= with no import-time
    provider wiring: build a minimal job dict, call the public entry point
    with call_model=_canned_model, and assert the six SUB_SCORE_KEYS
    round-trip into the result's sub-scores."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    result = job_scorer.score_job(
        _good_job(),
        conn,
        {"providers": {}},
        _TEST_CTX,
        call_model=_canned_model,
    )

    assert result.status == "ok", f"expected ok, got {result.status} ({result.error})"
    assert result.data is not None
    for key in SUB_SCORE_KEYS:
        assert key in result.data.sub_scores, f"missing sub-score key: {key}"
        assert result.data.sub_scores[key] == 3
    assert result.provider == "fake"
    assert result.model == "fake-model"

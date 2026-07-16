"""Tests for jobcannon.engine.job_scorer.

D-28 carry-forward: Byte-identical determinism is not achievable on the
local Ollama + CUDA stack below Ollama's abstraction. Phase 33's probe
showed 2 of 3 fixtures drift on repeated temperature=0 seed=42 runs.
The success criterion for v3 scoring is ordinal stability — axis rankings
preserved across invocations — NOT byte-equality. No byte-identical test
in this file; rescore gates (Plan 4 G1-G4) capture the same intent via
G3 correlation across the full baseline.

Porting note (Phase 1A Task 4): the private repo mocked the provider
cascade by patching the module-level ``job_finder.web.job_scorer.call_model``
import. The engine has no such import — ``call_model`` is a required
keyword-only parameter of ``score_job`` — so every test below passes a
``MagicMock`` (or a ``FakeModelResult``-returning callable) directly as
``call_model=`` instead of patching an import.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from jobcannon.engine.classification import JobAssessment
from jobcannon.engine.job_scorer import (
    JOB_ASSESSMENT_SCHEMA,
    ScoringResult,
    _build_user_message,
    _coerce_assessment,
    _derive_max_jd_chars,
    score_job,
)

from .conftest import FakeModelResult

# Minimal stub context for unit tests of score_job — these tests cover
# the scorer's plumbing (call_model dispatch, schema validation, skip
# preconditions) rather than rubric-context interaction, so any non-empty
# block satisfies the required-arg contract.
_TEST_CTX = "## Candidate context\n\n### Targeting\n- Target titles: Test Role"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _good_response_data() -> dict:
    """A schema-valid v3 response dict matching JOB_ASSESSMENT_SCHEMA."""
    return {
        "title_fit": 5,
        "location_fit": 4,
        "comp_fit": 4,
        "domain_match": 3,
        "seniority_match": 5,
        "skills_match": 4,
        "rationale": {
            "strengths": ["Python expert", "ML background"],
            "gaps": ["No Kubernetes"],
            "talking_points": ["Led a 6-person platform team"],
            "resume_priority_skills": ["Python", "PyTorch"],
        },
        "legitimacy_note": None,
    }


def _good_model_result(provider: str = "ollama") -> FakeModelResult:
    """Build a FakeModelResult the dispatcher would return on success."""
    return FakeModelResult(
        data=_good_response_data(),
        cost_usd=0.0,
        input_tokens=500,
        output_tokens=200,
        model="qwen2.5:14b",
        provider=provider,
        schema_valid=True,
    )


def _good_job() -> dict:
    """Minimal job dict with a non-empty jd_full."""
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


@pytest.fixture
def mock_conn():
    """Stand-in conn — score_job does not write to it directly."""
    return MagicMock()


@pytest.fixture
def config():
    """Minimal config dict — resolver inherits from peer tier if scoring absent."""
    return {"providers": {}}


# ---------------------------------------------------------------------------
# Tests: JD truncation (issue #1081)
# ---------------------------------------------------------------------------


class TestJDTruncation:
    """JD truncation is derived from num_ctx, not hardcoded (issue #1081)."""

    def test_derive_max_jd_chars_default_config(self):
        """Default num_ctx=12288 yields expected max_jd_chars."""
        config = {"providers": {}}
        max_chars = _derive_max_jd_chars(config)
        # 12288 - 2300 (system) - 2048 (headroom) = 7940 tokens * 3 chars/token = 23820
        assert max_chars == 23820

    def test_derive_max_jd_chars_custom_num_ctx(self):
        """Custom num_ctx is reflected in max_jd_chars."""
        config = {"providers": {"ollama": {"num_ctx": 8192}}}
        max_chars = _derive_max_jd_chars(config)
        # 8192 - 2300 - 2048 = 3844 tokens * 3 = 11532
        assert max_chars == 11532

    def test_derive_max_jd_chars_none_config(self):
        """None config uses default num_ctx."""
        max_chars = _derive_max_jd_chars(None)
        assert max_chars == 23820

    def test_build_user_message_truncates_when_jd_exceeds_budget(self, caplog):
        """JD longer than derived max_jd_chars is truncated with warning."""
        config = {"providers": {"ollama": {"num_ctx": 8192}}}
        max_chars = _derive_max_jd_chars(config)  # 11676

        job = _good_job()
        job["jd_full"] = "x" * (max_chars + 1000)  # Exceeds budget

        with caplog.at_level("WARNING"):
            msg = _build_user_message(job, config)

        # Should be truncated - the JD section should be at most max_chars
        jd_section = msg.split("Job Description:\n")[1]
        assert len(jd_section) <= max_chars
        # Should log warning
        assert any("hard-truncating tail" in record.message for record in caplog.records)

    def test_build_user_message_no_truncation_when_jd_within_budget(self):
        """JD within budget is sent whole."""
        config = {"providers": {"ollama": {"num_ctx": 12288}}}
        max_chars = _derive_max_jd_chars(config)

        job = _good_job()
        job["jd_full"] = "x" * (max_chars - 1000)  # Within budget

        msg = _build_user_message(job, config)

        # Should contain full JD
        assert job["jd_full"] in msg

    def test_derive_max_jd_chars_pathologically_low_num_ctx(self):
        """Pathologically low num_ctx is floored at 0 to prevent negative budget.

        Without max(0, ...) clamp, num_ctx below ~4300 would produce negative
        available_tokens, causing jd_full to be sliced by a negative index
        (truncating from the END). This test verifies the floor prevents that.
        """
        config = {"providers": {"ollama": {"num_ctx": 4000}}}
        max_chars = _derive_max_jd_chars(config)
        # 4000 - 2300 (system) - 2048 (headroom) = -348 tokens -> clamped to 0
        assert max_chars == 0


# ---------------------------------------------------------------------------
# Tests: SCORER-05 skip precondition
# ---------------------------------------------------------------------------


class TestSkipPrecondition:
    """score_job returns status='skipped' when jd_full is empty/None (SCORER-05)."""

    def test_skips_on_empty_jd_full(self, mock_conn, config):
        """Empty string jd_full -> skipped, no call_model invocation."""
        job = _good_job()
        job["jd_full"] = ""
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)
        assert result.status == "skipped"
        assert result.data is None
        assert result.provider is None
        mock_call.assert_not_called()

    def test_skips_on_none_jd_full(self, mock_conn, config):
        """None jd_full -> skipped, no call_model invocation."""
        job = _good_job()
        job["jd_full"] = None
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)
        assert result.status == "skipped"
        assert result.data is None
        mock_call.assert_not_called()

    def test_skips_on_missing_jd_full_key(self, mock_conn, config):
        """Missing jd_full key entirely -> skipped, no call_model invocation."""
        job = _good_job()
        del job["jd_full"]
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)
        assert result.status == "skipped"
        mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: happy path + dispatcher routing
# ---------------------------------------------------------------------------


class TestHappyPath:
    """score_job routes through the injected call_model(tier='score', ...) and
    returns a JobAssessment."""

    def test_happy_path_returns_job_assessment(self, mock_conn, config):
        """Valid job + valid model result -> status='ok' with populated JobAssessment."""
        mock_call = MagicMock(return_value=_good_model_result(provider="ollama"))
        result = score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "ok"
        assert isinstance(result.data, JobAssessment)
        assert result.provider == "ollama"
        assert result.error is None

    def test_assessment_has_all_six_sub_scores(self, mock_conn, config):
        """JobAssessment.sub_scores has all 6 D-05 keys as integers."""
        mock_call = MagicMock(return_value=_good_model_result())
        result = score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.data is not None
        for key in (
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ):
            assert key in result.data.sub_scores, f"Missing sub-score key: {key}"
            assert isinstance(result.data.sub_scores[key], int)

    def test_assessment_rationale_has_d03_keys(self, mock_conn, config):
        """JobAssessment.rationale has all 4 keys from the v3 schema."""
        mock_call = MagicMock(return_value=_good_model_result())
        result = score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.data is not None
        rationale = result.data.rationale
        for key in ("strengths", "gaps", "talking_points", "resume_priority_skills"):
            assert key in rationale, f"Missing rationale key: {key}"

    def test_assessment_classification_is_sentinel_empty_string(self, mock_conn, config):
        """score_job leaves classification='' — persist_job_assessment derives it."""
        mock_call = MagicMock(return_value=_good_model_result())
        result = score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.data is not None
        # The classification field is a sentinel; real value is derived at persist time.
        assert result.data.classification == ""

    def test_call_model_invoked_with_tier_score(self, mock_conn, config):
        """call_model is called with tier='score' (renamed from 'scoring' in commit abeecf9)."""
        mock_call = MagicMock(return_value=_good_model_result())
        score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert mock_call.call_count == 1
        kwargs = mock_call.call_args.kwargs
        assert kwargs.get("tier") == "score"

    def test_call_model_invoked_with_job_assessment_schema(self, mock_conn, config):
        """call_model receives output_schema=JOB_ASSESSMENT_SCHEMA (identity-equal)."""
        mock_call = MagicMock(return_value=_good_model_result())
        score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        kwargs = mock_call.call_args.kwargs
        assert kwargs.get("output_schema") is JOB_ASSESSMENT_SCHEMA

    def test_system_prompt_contains_v3_content(self, mock_conn, config):
        """The system arg passed to call_model contains v3 prompt + fewshots + reinforcement."""
        mock_call = MagicMock(return_value=_good_model_result())
        score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        kwargs = mock_call.call_args.kwargs
        system = kwargs.get("system", "")
        # FIELD_REINFORCEMENT has a distinctive marker: "STRICT FIELD NAMES"
        assert "STRICT FIELD NAMES" in system, "system prompt missing FIELD_REINFORCEMENT"
        # FEWSHOT_EXAMPLES has a distinctive marker: "Fewshot calibration"
        assert "Fewshot calibration" in system, "system prompt missing FEWSHOT_EXAMPLES"

    def test_user_message_contains_job_content(self, mock_conn, config):
        """The user message includes title, company, location, and jd_full."""
        mock_call = MagicMock(return_value=_good_model_result())
        score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        kwargs = mock_call.call_args.kwargs
        messages = kwargs.get("messages") or []
        assert len(messages) == 1
        content = messages[0].get("content", "")
        assert "Senior ML Engineer" in content
        assert "Acme Corp" in content or "acme corp" in content
        assert "Remote US" in content
        assert "Build scalable ML platforms" in content  # jd_full excerpt

    def test_job_id_is_dedup_key(self, mock_conn, config):
        """job_id passed to call_model is the job's dedup_key (str)."""
        mock_call = MagicMock(return_value=_good_model_result())
        score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        kwargs = mock_call.call_args.kwargs
        assert kwargs.get("job_id") == "acme|senior-ml-engineer"


# ---------------------------------------------------------------------------
# Tests: JD handling (Layer 1 — send jd_full whole; safety-net truncate only
# for pathological oversized postings, never a silent section-drop). Plus
# build_comp_context folding for ATS-sourced compensation.
# ---------------------------------------------------------------------------


def _user_content(job, mock_conn, config) -> str:
    """Run score_job with call_model mocked and return the user message text."""
    mock_call = MagicMock(return_value=_good_model_result())
    score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)
    return mock_call.call_args.kwargs["messages"][0]["content"]


class TestJdHandling:
    """_build_user_message sends jd_full whole; truncates only past the derived cap."""

    def test_normal_jd_sent_verbatim(self, mock_conn, config):
        """A posting under the cap is sent whole — no truncation, no dropped
        middle section (the regression Layer 1 eliminates)."""
        job = _good_job()
        content = _user_content(job, mock_conn, config)
        assert job["jd_full"] in content

    def test_jd_at_cap_boundary_sent_whole(self, mock_conn, config):
        """Exactly at the derived max_jd_chars is still sent whole (boundary is inclusive)."""
        max_chars = _derive_max_jd_chars(config)

        job = _good_job()
        # Unique tail marker right at the cap so we can prove nothing was cut.
        job["jd_full"] = "x" * (max_chars - 5) + "ENDXX"
        assert len(job["jd_full"]) == max_chars
        content = _user_content(job, mock_conn, config)
        assert "ENDXX" in content

    def test_oversized_jd_truncated_with_warning_not_silently(self, mock_conn, config, caplog):
        """A pathological >cap posting is hard-truncated to the cap AND logs a
        warning — the truncation is never silent."""
        import logging

        max_chars = _derive_max_jd_chars(config)

        job = _good_job()
        job["jd_full"] = "y" * (max_chars + 5000) + "TAILMARKER"
        with caplog.at_level(logging.WARNING, logger="jobcannon.engine.job_scorer"):
            content = _user_content(job, mock_conn, config)

        # JD portion is bounded by the cap; the far tail is dropped.
        assert "TAILMARKER" not in content
        # But the drop is announced, not silent.
        assert any("hard-truncating" in r.message for r in caplog.records)
        assert any(str(max_chars) in r.message for r in caplog.records)

    def test_comp_data_json_appends_compensation_line(self, mock_conn, config):
        """build_comp_context folds ATS-sourced comp into the user message."""
        job = _good_job()
        job["comp_data_json"] = json.dumps({"compensationTierSummary": "$200k base + 0.1% equity"})
        content = _user_content(job, mock_conn, config)
        assert "Compensation: $200k base + 0.1% equity" in content

    def test_no_comp_data_json_omits_compensation_line(self, mock_conn, config):
        """Without comp_data_json, no Compensation line is added (Salary only)."""
        job = _good_job()
        job.pop("comp_data_json", None)
        content = _user_content(job, mock_conn, config)
        assert "Compensation:" not in content


# ---------------------------------------------------------------------------
# Tests: error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    """score_job returns status='error' when dispatcher fails or returns invalid data."""

    def test_dispatcher_exception_returns_error(self, mock_conn, config):
        """Exception in call_model -> ScoringResult(status='error') with reason."""
        mock_call = MagicMock(side_effect=RuntimeError("ollama timeout"))
        result = score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "error"
        assert result.data is None
        assert result.error is not None
        assert "ollama timeout" in result.error

    def test_schema_invalid_result_returns_error(self, mock_conn, config):
        """schema_valid=False on the ModelResult -> status='error'."""
        bad = FakeModelResult(
            data={"title_fit": 5},  # incomplete
            cost_usd=0.0,
            input_tokens=100,
            output_tokens=10,
            model="qwen2.5:14b",
            provider="ollama",
            schema_valid=False,
        )
        mock_call = MagicMock(return_value=bad)
        result = score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "error"
        assert result.provider == "ollama"

    def test_empty_data_returns_error(self, mock_conn, config):
        """Empty data dict -> status='error'."""
        empty = FakeModelResult(
            data={},
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            model="qwen2.5:14b",
            provider="ollama",
            schema_valid=True,
        )
        mock_call = MagicMock(return_value=empty)
        result = score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "error"


# ---------------------------------------------------------------------------
# Tests: schema contract
# ---------------------------------------------------------------------------


class TestSchemaContract:
    """The v3 schema does NOT emit classification; Python derives it later."""

    def test_schema_has_no_classification_key(self):
        """JOB_ASSESSMENT_SCHEMA.properties must not contain 'classification'.

        Per CONTEXT D-06 anti-pattern 3: classification is Python-derived
        from sub_scores + legitimacy_note at persist time — never LLM-emitted.
        """
        assert "classification" not in JOB_ASSESSMENT_SCHEMA.get("properties", {}), (
            "v3 schema must not declare classification as a property "
            "(derive_classification owns the 4-way rule)"
        )
        assert "classification" not in JOB_ASSESSMENT_SCHEMA.get("required", []), (
            "v3 schema must not require classification from the LLM"
        )

    def test_coerce_ignores_any_classification_field(self):
        """_coerce_assessment ignores a classification field if the model emits one."""
        data = _good_response_data()
        data["classification"] = "apply"  # lying/hallucinated — must be ignored
        assessment = _coerce_assessment(data, provider="ollama")
        # Sentinel empty string — persist_job_assessment overwrites with derived value.
        assert assessment.classification == ""

    def test_coerce_extracts_sub_scores_from_top_level(self):
        """Sub-score fields are at the top level of the LLM response (not nested)."""
        data = _good_response_data()
        assessment = _coerce_assessment(data, provider="ollama")
        assert assessment.sub_scores == {
            "title_fit": 5,
            "location_fit": 4,
            "comp_fit": 4,
            "domain_match": 3,
            "seniority_match": 5,
            "skills_match": 4,
        }

    def test_coerce_defensively_converts_string_sub_scores_to_int(self):
        """If a sub-score arrives as a string (dispatcher coercion gap), cast to int."""
        data = _good_response_data()
        data["title_fit"] = "5"
        assessment = _coerce_assessment(data, provider="ollama")
        assert assessment.sub_scores["title_fit"] == 5
        assert isinstance(assessment.sub_scores["title_fit"], int)

    def test_coerce_unwraps_d2_evidence_score_pairs(self):
        """Variant v4d2 wraps each axis as {evidence, score}; coerce extracts the int."""
        data = _good_response_data()
        # Wrap each axis as the D2 variant emits it.
        for key in (
            "title_fit",
            "location_fit",
            "comp_fit",
            "domain_match",
            "seniority_match",
            "skills_match",
        ):
            data[key] = {"evidence": f"<jd-quote-for-{key}>", "score": data[key]}
        assessment = _coerce_assessment(data, provider="ollama")
        assert assessment.sub_scores == {
            "title_fit": 5,
            "location_fit": 4,
            "comp_fit": 4,
            "domain_match": 3,
            "seniority_match": 5,
            "skills_match": 4,
        }
        assert all(isinstance(v, int) for v in assessment.sub_scores.values())


# ---------------------------------------------------------------------------
# Tests: issue #227 quality floor — fail-closed coercion + degenerate flag
# ---------------------------------------------------------------------------


class TestQualityFloorCoercion:
    """_coerce_assessment fails closed on partial/uncoercible vectors (#227)."""

    def test_coerce_raises_on_missing_axis(self):
        """A missing required axis raises instead of producing a partial vector."""
        from jobcannon.engine.job_scorer import _IncompleteAssessmentError

        data = _good_response_data()
        del data["domain_match"]
        with pytest.raises(_IncompleteAssessmentError, match="domain_match"):
            _coerce_assessment(data, provider="ollama")

    def test_coerce_raises_on_uncoercible_axis(self):
        """A non-int-coercible axis raises rather than being silently dropped."""
        from jobcannon.engine.job_scorer import _IncompleteAssessmentError

        data = _good_response_data()
        data["title_fit"] = "not-a-number"
        with pytest.raises(_IncompleteAssessmentError, match="title_fit"):
            _coerce_assessment(data, provider="ollama")

    def test_partial_vector_does_not_classify_apply(self):
        """A partial high-score vector must NOT survive into an apply verdict.

        Pre-#227 bug: dropping an axis left a partial dict that
        derive_classification read with all(v >= 3 ...) passing vacuously over
        the surviving axes → spurious apply. Now coercion fails closed first.
        """
        from jobcannon.engine.job_scorer import _IncompleteAssessmentError

        # All-5s but one axis missing — the dangerous case.
        # (skills_match deliberately absent)
        data = dict.fromkeys(
            (
                "title_fit",
                "location_fit",
                "comp_fit",
                "domain_match",
                "seniority_match",
            ),
            5,
        )
        data["rationale"] = {
            "strengths": ["x"],
            "gaps": [],
            "talking_points": [],
            "resume_priority_skills": [],
        }
        with pytest.raises(_IncompleteAssessmentError):
            _coerce_assessment(data, provider="ollama")

    def test_score_job_returns_error_on_incomplete_assessment(self, mock_conn, config):
        """score_job catches incomplete coercion and returns status='error'."""
        partial = _good_response_data()
        del partial["seniority_match"]
        bad = FakeModelResult(
            data=partial,
            cost_usd=0.0,
            input_tokens=1,
            output_tokens=1,
            model="qwen2.5:14b",
            provider="ollama",
            schema_valid=True,
        )
        mock_call = MagicMock(return_value=bad)
        result = score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)
        assert result.status == "error"
        assert result.data is None
        assert "incomplete assessment" in (result.error or "")

    def test_complete_vector_accepted(self):
        """A complete vector still coerces fine and is not flagged degenerate."""
        assessment = _coerce_assessment(_good_response_data(), provider="ollama")
        assert len(assessment.sub_scores) == 6
        assert assessment.degenerate is False

    def test_degenerate_flag_threaded_through(self):
        """_coerce_assessment(degenerate=True) sets JobAssessment.degenerate."""
        assessment = _coerce_assessment(_good_response_data(), provider="ollama", degenerate=True)
        assert assessment.degenerate is True

    def test_score_job_propagates_degenerate_flag(self, mock_conn, config):
        """A flagged degenerate ModelResult yields a degenerate JobAssessment."""
        flagged = FakeModelResult(
            data=_good_response_data(),
            cost_usd=0.0,
            input_tokens=1,
            output_tokens=1,
            model="qwen2.5:14b",
            provider="ollama",
            schema_valid=True,
            degenerate=True,
        )
        mock_call = MagicMock(return_value=flagged)
        result = score_job(_good_job(), mock_conn, config, _TEST_CTX, call_model=mock_call)
        assert result.status == "ok"
        assert result.data.degenerate is True


# ---------------------------------------------------------------------------
# Tests: module-level invariants
# ---------------------------------------------------------------------------


class TestModuleInvariants:
    """score_job is a pure-function addition with no in-tree caller (a host
    wires it via the injected call_model= parameter)."""

    def test_scoring_result_is_frozen(self):
        """ScoringResult is @dataclass(frozen=True) (hashable, immutable)."""
        r = ScoringResult(status="ok", data=None, provider="ollama")
        with pytest.raises((AttributeError, Exception)):
            r.status = "error"  # type: ignore[misc]

    def test_module_exports_expected_names(self):
        """__all__ declares score_job, scoring_precheck, ScoringResult, JOB_ASSESSMENT_SCHEMA."""
        from jobcannon.engine import job_scorer

        assert "score_job" in job_scorer.__all__
        assert "scoring_precheck" in job_scorer.__all__
        assert "ScoringResult" in job_scorer.__all__
        assert "JOB_ASSESSMENT_SCHEMA" in job_scorer.__all__

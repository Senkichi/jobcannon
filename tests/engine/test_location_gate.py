"""Tests for the P3.2 location completeness gate in score_job (issue #391).

D-7 (Completeness gates, not garbage-in scoring): a job is scored when its
inputs are ready or provably exhausted. The location gate mirrors the existing
jd_full gate — jobs without resolved location facts are deferred to enrichment
rather than scored with degraded inputs.

Two directions (per the task spec):
  - job with sufficient location → scores normally (LLM called)
  - job with missing/insufficient location → gated, status='skipped',
    reason='awaiting_location', no LLM call

Pass-through conditions (gate does NOT fire):
  - terminal enrichment tier (exhausted / agentic / agentic_exhausted) —
    location is as good as it will get; let the LLM judge from JD prose.
  - row carries "location_missing" in unresolved_reasons — explicitly flagged
    as unresolvable by P2.3/P2.4; do not gate forever.
  - locations_structured is non-empty (structured facts present).
  - location flat string is non-empty (display string present).

Porting note (Phase 1A Task 4): call_model is now a required keyword-only
parameter of score_job rather than a module-level import, so every call site
below passes a plain MagicMock() via call_model= instead of patching
"job_finder.web.job_scorer.call_model".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from jobcannon.engine.job_scorer import ScoringResult, score_job

from .conftest import FakeModelResult

# Minimal candidate context stub — the gate tests do not exercise rubric
# anchors, just the skip-precondition branch. Any non-empty string suffices.
_TEST_CTX = "## Candidate context\n\n### Targeting\n- Target titles: Test Role"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_response_data() -> dict:
    """Schema-valid v3 response dict."""
    return {
        "title_fit": 4,
        "location_fit": 5,
        "comp_fit": 4,
        "domain_match": 4,
        "seniority_match": 4,
        "skills_match": 4,
        "rationale": {
            "strengths": ["Python"],
            "gaps": [],
            "talking_points": ["Led a team"],
            "resume_priority_skills": ["Python"],
        },
        "legitimacy_note": None,
    }


def _good_model_result() -> FakeModelResult:
    return FakeModelResult(
        data=_good_response_data(),
        cost_usd=0.0,
        input_tokens=100,
        output_tokens=100,
        model="qwen2.5:14b",
        provider="ollama",
        schema_valid=True,
    )


def _base_job(**overrides) -> dict:
    """Job with all required fields for scoring (jd_full + location present)."""
    base = {
        "dedup_key": "acme|data-scientist",
        "title": "Data Scientist",
        "company": "Acme Corp",
        "company_canonical": "acme corp",
        "location": "Remote US",
        "locations_structured": json.dumps(
            [
                {
                    "city": None,
                    "region": None,
                    "region_code": None,
                    "country": "United States",
                    "country_code": "US",
                    "workplace_type": "REMOTE",
                    "raw": "Remote US",
                    "unresolved": False,
                }
            ]
        ),
        "jd_full": "Build ML systems. Python, SQL, AWS required.",
        "salary_min": 150000,
        "salary_max": 220000,
        "enrichment_tier": "free",
        "unresolved_reasons": "[]",
    }
    base.update(overrides)
    return base


@pytest.fixture
def mock_conn():
    return MagicMock()


@pytest.fixture
def config():
    return {"providers": {}}


# ---------------------------------------------------------------------------
# Gate fires: empty location + enrichable tier → skipped/awaiting_location
# ---------------------------------------------------------------------------


class TestLocationGateFires:
    """Location gate returns status='skipped', reason='awaiting_location'
    when location is unresolved and enrichment can still run."""

    def test_skips_when_both_location_fields_empty_free_tier(self, mock_conn, config):
        """Empty locations_structured + empty location + free tier → gate fires."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier="free",
        )
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_location"
        assert result.data is None
        assert result.provider is None
        mock_call.assert_not_called()

    def test_skips_when_locations_structured_null_string(self, mock_conn, config):
        """locations_structured=None + empty location + ddg tier → gate fires."""
        job = _base_job(
            location="",
            locations_structured=None,
            enrichment_tier="ddg",
        )
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_location"
        mock_call.assert_not_called()

    def test_skips_when_locations_structured_missing_key(self, mock_conn, config):
        """locations_structured key absent from job dict → treated as empty."""
        job = _base_job(location="", enrichment_tier="free")
        del job["locations_structured"]
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_location"
        mock_call.assert_not_called()

    def test_skips_when_location_is_whitespace_only(self, mock_conn, config):
        """Whitespace-only location flat string is treated as empty."""
        job = _base_job(
            location="   ",
            locations_structured="[]",
            enrichment_tier="free",
        )
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_location"
        mock_call.assert_not_called()

    def test_skips_on_none_tier(self, mock_conn, config):
        """enrichment_tier=None is not terminal → gate fires on empty location."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier=None,
        )
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_location"
        mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# Gate does NOT fire: complete location → scores normally
# ---------------------------------------------------------------------------


class TestLocationGatePassthrough:
    """Gate must NOT fire when location facts are present — LLM is called."""

    def test_scores_when_locations_structured_non_empty(self, mock_conn, config):
        """Non-empty locations_structured → gate passes, LLM invoked."""
        job = _base_job()  # has both location and locations_structured populated
        mock_call = MagicMock(return_value=_good_model_result())
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "ok"

    def test_scores_when_location_flat_string_set_even_if_structured_empty(self, mock_conn, config):
        """Non-empty location flat string alone is enough to pass the gate."""
        job = _base_job(
            location="Remote",
            locations_structured="[]",
            enrichment_tier="free",
        )
        mock_call = MagicMock(return_value=_good_model_result())
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "ok"

    def test_scores_when_structured_is_non_empty_json_array(self, mock_conn, config):
        """A single-element locations_structured JSON array is sufficient."""
        job = _base_job(
            location="",  # flat string empty — but structured is not
            locations_structured=json.dumps(
                [
                    {
                        "city": "San Francisco",
                        "country_code": "US",
                        "workplace_type": "ONSITE",
                        "raw": "San Francisco, CA",
                        "unresolved": False,
                    }
                ]
            ),
            enrichment_tier="free",
        )
        mock_call = MagicMock(return_value=_good_model_result())
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "ok"


# ---------------------------------------------------------------------------
# Gate does NOT fire: terminal enrichment tier → scores even with empty location
# ---------------------------------------------------------------------------


class TestTerminalTierPassthrough:
    """Jobs at terminal enrichment tiers pass through regardless of location state.

    _TERMINAL_ENRICHMENT_TIERS = {exhausted, agentic, agentic_exhausted} —
    these are the scoring-terminal set from jobcannon.engine.classification,
    NOT the seven-tier backfill-exclusion list from enrichment_states.TERMINAL.
    """

    @pytest.mark.parametrize("tier", ["exhausted", "agentic", "agentic_exhausted"])
    def test_scores_at_terminal_tier_despite_empty_location(self, tier, mock_conn, config):
        """Terminal-tier jobs with empty location pass the gate and score."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier=tier,
        )
        mock_call = MagicMock(return_value=_good_model_result())
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "ok", (
            f"Expected ok for terminal tier={tier!r} but got {result.status!r}"
        )

    @pytest.mark.parametrize(
        "non_terminal_tier",
        ["free", "ddg", "serpapi", "low", "mid", "high", None],
    )
    def test_gate_fires_at_non_terminal_tiers(self, non_terminal_tier, mock_conn, config):
        """Non-terminal tiers with empty location → gate fires (skipped)."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier=non_terminal_tier,
        )
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_location"
        mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# Gate does NOT fire: "location_missing" in unresolved_reasons → scores
# ---------------------------------------------------------------------------


class TestLocationMissingPassthrough:
    """Jobs tagged location_missing in unresolved_reasons are never gated forever.

    "location_missing" is the unresolved_reasons code introduced by P2.3/P2.4
    (issues #388/#389). When present it signals that all available evidence was
    exhausted without resolving a location — blocking would orphan the row.
    """

    def test_scores_when_location_missing_in_unresolved_reasons(self, mock_conn, config):
        """location_missing in unresolved_reasons overrides the gate."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier="free",
            unresolved_reasons=json.dumps(["location_missing"]),
        )
        mock_call = MagicMock(return_value=_good_model_result())
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "ok"

    def test_scores_when_location_missing_alongside_other_reasons(self, mock_conn, config):
        """location_missing overrides even when other reasons are also present."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier="ddg",
            unresolved_reasons=json.dumps(["jd_full_junk", "location_missing"]),
        )
        mock_call = MagicMock(return_value=_good_model_result())
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "ok"

    def test_gate_fires_when_other_reasons_present_but_not_location_missing(
        self, mock_conn, config
    ):
        """Other unresolved_reasons codes do NOT disable the gate."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier="free",
            unresolved_reasons=json.dumps(["jd_full_junk", "title_metadata_blob"]),
        )
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_location"
        mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# Gate ordering: jd_full gate runs first
# ---------------------------------------------------------------------------


class TestGateOrdering:
    """jd_full gate fires before location gate — both gates fire independently."""

    def test_jd_gate_fires_first_when_both_jd_and_location_missing(self, mock_conn, config):
        """When both jd_full AND location are absent, jd gate fires (reason='awaiting_jd')."""
        job = _base_job(
            jd_full="",
            location="",
            locations_structured="[]",
            enrichment_tier="free",
        )
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_jd"
        mock_call.assert_not_called()

    def test_jd_gate_reason_is_awaiting_jd(self, mock_conn, config):
        """Standalone jd_full gate returns reason='awaiting_jd'."""
        job = _base_job(jd_full=None)
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_jd"
        mock_call.assert_not_called()

    def test_location_gate_fires_when_jd_present_but_location_absent(self, mock_conn, config):
        """With jd_full present but location absent → location gate fires."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier="free",
        )
        mock_call = MagicMock()
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=mock_call)

        assert result.status == "skipped"
        assert result.reason == "awaiting_location"
        mock_call.assert_not_called()


# ---------------------------------------------------------------------------
# ScoringResult.reason field
# ---------------------------------------------------------------------------


class TestScoringResultReasonField:
    """ScoringResult has a reason field defaulting to None."""

    def test_reason_defaults_to_none(self):
        r = ScoringResult(status="ok", data=None)
        assert r.reason is None

    def test_reason_set_on_jd_skip(self, mock_conn, config):
        job = _base_job(jd_full="")
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=MagicMock())
        assert result.reason == "awaiting_jd"

    def test_reason_set_on_location_skip(self, mock_conn, config):
        job = _base_job(location="", locations_structured="[]", enrichment_tier="free")
        result = score_job(job, mock_conn, config, _TEST_CTX, call_model=MagicMock())
        assert result.reason == "awaiting_location"

    def test_reason_none_on_ok(self, mock_conn, config):
        result = score_job(
            _base_job(),
            mock_conn,
            config,
            _TEST_CTX,
            call_model=MagicMock(return_value=_good_model_result()),
        )
        assert result.status == "ok"
        assert result.reason is None

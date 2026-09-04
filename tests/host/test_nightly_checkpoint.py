# PORTED from tests/test_nightly_checkpoint.py @ d21f9803c3f6bb9e606b5b7cf7b27bd5f5f06e00 (private job-cannon). Ledger L-0534.
# PORT-SEAM: private's job_finder/web/nightly_monitor/_checkpoint.py was
# split in two for the host: jobcannon/host/nightly/checkpoint_verdict.py
# (the guard chain -- _sanitize_verdict -> _guard_in_band_duration ->
# _validate_reasons -> _guard_non_attributable_db_delta ->
# _guard_new_row_backlog -> _guard_excerpt_absence ->
# _guard_ambiguous_only_evidence -- stays a pure function over a packet
# dict) and jobcannon/host/nightly/checkpoint_packet.py (evidence-packet
# assembly, build_packet/db_delta_summary). Both carry near-verbatim; the
# guard machinery does not depend on a live DB connection at all, so every
# kept test below runs DB-free even though this file sits in tests/host/
# (matching source-tree location, per tests/host/test_nightly_checkpoint_
# {verdict,packet}.py, the native PR #355 suites this file complements
# rather than duplicates -- those cover one example per guard; this file
# carries the private edge-case matrix, e.g. 9 sub-cases on the in-band-
# duration guard alone). Mechanical seam: private module-patched
# `call_model` via unittest.mock.patch; the host's checkpoint_verdict()
# takes call_model as an injected keyword instead, so every test below
# passes call_model=MagicMock(...) directly.
#
# Dropped (6 of 116, all of TestJdContentSameHashInvalidatedCheckpoint):
# assert on jd_content_same_hash_invalidated_pairs(), a DB-querying helper
# that is not among this port's carried top-level functions -- it belongs
# to a different, unscoped subsystem/ledger unit. See this PR's body.
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from jobcannon.host.model_provider import ProviderCascadeExhaustedError
from jobcannon.host.nightly.checkpoint_packet import (
    _db_delta_summary,
    build_packet,
    job_tracks_db_delta_from_history,
)
from jobcannon.host.nightly.checkpoint_verdict import (
    _SYSTEM,
    VERDICT_SCHEMA,
    _guard_new_row_backlog,
    _has_evidence_of_work,
    _has_success_excerpt,
    _is_excerpt_absence_reason,
    _is_improvement_reason,
    _is_no_work_reason,
    checkpoint_verdict,
)
from jobcannon.host.nightly.config import DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS


class _FakeResult:
    def __init__(self, data):
        self.data = data


RUN_END = {
    "event": "run_end",
    "run_id": "ATS scan:123:1",
    "job": "ATS scan",
    "source": "scheduler",
    "disposition": "completed",
    "duration_s": 100.0,
    "db_delta": {"total_jobs": 4},
}
BAND = {"status": "ok", "n": 5, "p10": 90.0, "p90": 120.0}


def _packet(**overrides):
    run_end = {**RUN_END, **overrides.pop("run_end", {})}
    return build_packet(
        run_end,
        hits=overrides.pop("hits", []),
        shared_signature_hits=overrides.pop("shared_signature_hits", []),
        log_excerpt=overrides.pop("log_excerpt", ""),
        band=overrides.pop("band", BAND),
        db_delta_tracked=overrides.pop("db_delta_tracked", None),
    )


class TestBuildPacket:
    def test_fields(self):
        p = _packet()
        assert p["job"] == "ATS scan"
        assert p["disposition"] == "completed"
        assert p["baseline"] == BAND
        assert p["out_of_band"] is None
        assert p["band_assessment"] == "in_band"
        assert p["in_band"] is True
        assert p["db_delta_tracked"] is None
        assert p["db_delta_summary"]["tracked"] is None
        assert p["signature_hits"] == []
        assert p["shared_signature_hits"] == []
        assert p["concurrent_context"] == ""
        assert p["log_excerpt_is_job_scoped"] is False

    def test_db_delta_tracked_none_passed_through(self):
        """No history must not be collapsed to True; None is a valid signal."""
        p = build_packet(RUN_END, hits=[], log_excerpt="", band=BAND)
        assert p["db_delta_tracked"] is None
        assert p["db_delta_summary"]["tracked"] is None

    def test_log_excerpt_capped(self):
        p = build_packet(RUN_END, hits=[], log_excerpt="x" * 10_000, band=BAND)
        assert len(p["log_excerpt"]) == 4000

    def test_concurrent_context_field_defaults_empty(self):
        """build_packet must always emit concurrent_context, defaulting to empty string."""
        p = build_packet(RUN_END, hits=[], log_excerpt="line", band=BAND)
        assert p["concurrent_context"] == ""

    def test_concurrent_context_capped(self):
        """concurrent_context is capped the same as log_excerpt."""
        p = build_packet(
            RUN_END,
            hits=[],
            log_excerpt="",
            concurrent_context="y" * 10_000,
            band=BAND,
        )
        assert len(p["concurrent_context"]) == 4000

    def test_out_of_band_slow(self):
        p = _packet(run_end={"duration_s": 500.0})
        assert p["out_of_band"] == "slow"
        assert p["band_assessment"] == "out_of_band"
        assert p["in_band"] is False

    def test_out_of_band_fast(self):
        p = _packet(run_end={"duration_s": 50.0})
        assert p["out_of_band"] == "fast"
        assert p["band_assessment"] == "out_of_band"
        assert p["in_band"] is False

    def test_tolerance_and_floor_args_passed_through(self):
        """build_packet must forward tolerance/floor to out_of_band so config values matter."""
        p = build_packet(
            {**RUN_END, "duration_s": 60.0},
            hits=[],
            log_excerpt="",
            band=BAND,
            tolerance=0.5,
            absolute_floor_s=1.0,
        )
        # With BAND p10=90 and default tolerance 0.25, 60.0 would be flagged "fast".
        # The custom tolerance 0.5 widens the lower bound to 45.0, so 60.0 is in band.
        assert p["out_of_band"] is None
        assert p["band_assessment"] == "in_band"
        assert p["in_band"] is True


class TestBandAssessment:
    def test_insufficient_history_when_band_not_ok(self):
        p = _packet(
            run_end={"duration_s": 1142.95},
            band={"status": "insufficient_history", "n": 2},
        )
        assert p["out_of_band"] is None
        assert p["band_assessment"] == "insufficient_history"
        assert p["in_band"] is False

    def test_insufficient_history_when_duration_missing(self):
        p = _packet(run_end={"duration_s": None}, band=BAND)
        assert p["out_of_band"] is None
        assert p["band_assessment"] == "insufficient_history"
        assert p["in_band"] is False


class TestForcedFail:
    def test_failed_disposition_forces_fail_without_model_call(self):
        p = _packet(run_end={"disposition": "failed", "error": "RuntimeError"})
        mock_cm = MagicMock()
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_not_called()
        assert v["verdict"] == "FAIL"
        assert v["forced"] is True
        assert any("RuntimeError" in r for r in v["reasons"])

    def test_fail_signature_is_model_adjudicated_not_forced(self):
        """A tick-global fail signature must NOT force-FAIL a job pre-model: hits
        carry no run_id, so force-FAILing would blame every job whose run_end
        shares the tick with another job's failure line. The attribution-aware
        model decides. Regression guard for the cross-job false-FAIL the
        two-family review caught."""
        p = _packet(hits=[{"pattern": "database is locked", "severity": "fail", "line": "x"}])
        result = _FakeResult({"verdict": "PASS", "reasons": ["another job's line"]})
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()  # model consulted, not bypassed
        assert v["verdict"] == "PASS"
        assert v["forced"] is False

    def test_fail_signature_with_model_down_floors_at_verdict_unavailable(self):
        """Model unavailable + fail signature => "VERDICT_UNAVAILABLE" floor:
        surfaced for the morning review as an infra failure, but never a false
        critical FAIL for a co-running job."""
        p = _packet(hits=[{"pattern": "database is locked", "severity": "fail", "line": "x"}])
        v = checkpoint_verdict(p, call_model=MagicMock(side_effect=RuntimeError("cascade down")))
        assert v["verdict"] == "VERDICT_UNAVAILABLE"
        assert v["forced"] is False

    def test_anomaly_signature_does_not_force(self):
        p = _packet(hits=[{"pattern": "HTTP 429", "severity": "anomaly", "line": "x"}])
        result = _FakeResult({"verdict": "PASS", "reasons": []})
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        assert v["verdict"] == "PASS"
        assert v["forced"] is False


class TestModelVerdict:
    def test_quick_tier_and_schema(self):
        result = _FakeResult(
            {"verdict": "ANOMALY", "reasons": ["unexplained db_delta for job-table counters"]}
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(_packet(), call_model=mock_cm)
        args, kwargs = mock_cm.call_args
        assert kwargs.get("tier", args[0] if args else None) == "quick"
        assert kwargs["output_schema"] == VERDICT_SCHEMA
        assert kwargs["purpose"] == "nightly_checkpoint"
        assert kwargs["max_tokens"] == 512
        assert v == {
            "verdict": "ANOMALY",
            "reasons": ["unexplained db_delta for job-table counters"],
            "forced": False,
            "rejected_reasons": 0,
        }

    def test_model_exception_yields_verdict_unavailable(self):
        v = checkpoint_verdict(
            _packet(), call_model=MagicMock(side_effect=RuntimeError("cascade down"))
        )
        assert v["verdict"] == "VERDICT_UNAVAILABLE"
        assert v["forced"] is False
        assert any("RuntimeError" in r for r in v["reasons"])

    def test_provider_cascade_exhausted_yields_verdict_unavailable_not_anomaly(self):
        """Regression for #1402: infra failures must not masquerade as job ANOMALY."""
        v = checkpoint_verdict(
            _packet(),
            call_model=MagicMock(
                side_effect=ProviderCascadeExhaustedError("ollama/gemini/anthropic exhausted")
            ),
        )
        assert v["verdict"] == "VERDICT_UNAVAILABLE"
        assert v["forced"] is False
        assert any("ProviderCascadeExhaustedError" in r for r in v["reasons"])
        assert v["reasons"][0].endswith("ollama/gemini/anthropic exhausted")

    def test_unrecognized_verdict_yields_anomaly(self):
        result = _FakeResult({"verdict": "MAYBE", "reasons": []})
        v = checkpoint_verdict(_packet(), call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"

    def test_ai_nav_failure_does_not_force_fail_when_degraded(self):
        """A degraded careers_crawl run still surfaces ai_nav counts but lets the model adjudicate.

        Issue #1367: the run is now ``degraded`` rather than ``failed`` so partial
        crawl results are preserved and the checkpoint is not forced into FAIL.
        """
        error_msg = (
            "ai_nav: discovery call failed for 10/10 attempts (100%) — exceeds 50% threshold"
        )
        p = _packet(run_end={"disposition": "degraded", "error": error_msg})
        assert "10/10" in p["error"]
        result = _FakeResult({"verdict": "ANOMALY", "reasons": ["ai_nav failure rate high"]})
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        assert v["verdict"] == "ANOMALY"
        assert v["forced"] is False


class TestBuildPacketInBand:
    def test_in_band_boolean_in_prompt_packet(self):
        p = _packet()
        assert p["in_band"] is True
        assert p["band_assessment"] == "in_band"
        assert p["out_of_band"] is None
        assert "db_delta_summary" in p
        assert p["db_delta_tracked"] is None

    def test_in_band_boolean_reaches_model_prompt(self):
        p = _packet()
        mock_cm = MagicMock(return_value=_FakeResult({"verdict": "PASS", "reasons": []}))
        checkpoint_verdict(p, call_model=mock_cm)
        kwargs = mock_cm.call_args[1]
        assert "band_assessment" in kwargs["system"]
        assert "in_band" in kwargs["system"].lower()
        assert "only sanctioned duration signal" in kwargs["system"]
        assert "db_delta" in kwargs["system"]
        assert "tracks job-table counters" in kwargs["system"]
        assert "db_delta_summary" in kwargs["system"]
        assert "db_delta_tracked" in kwargs["system"]
        packet = json.loads(kwargs["messages"][0]["content"])
        assert packet["in_band"] is True
        assert packet["band_assessment"] == "in_band"
        assert "db_delta" not in packet
        assert "db_delta_summary" in packet


class TestInBandDurationGuard:
    def test_in_band_duration_anomaly_downgraded_to_pass(self):
        p = _packet(
            run_end={"duration_s": 1142.95},
            band={"status": "ok", "n": 10, "p10": 1044.282, "p90": 1906.478},
        )
        assert p["in_band"] is True
        assert p["band_assessment"] == "in_band"
        assert p["out_of_band"] is None
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "Duration of 1142.95 seconds is longer than the baseline p90 duration of 1906.478 seconds."
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == ["duration in band; out_of_band is null"]
        assert v["forced"] is False

    def test_in_band_duration_anomaly_with_other_reason_keeps_anomaly(self):
        p = _packet(
            run_end={"duration_s": 4.27},
            band={"status": "ok", "n": 10, "p10": 3.669, "p90": 7.954},
        )
        assert p["in_band"] is True
        assert p["band_assessment"] == "in_band"
        assert p["out_of_band"] is None
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "Duration is shorter than expected (4.27s vs baseline p90 of 7.95s).",
                    "HTTP 429 in signature_hits",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert len(v["reasons"]) == 1
        assert "HTTP 429" in v["reasons"][0]
        assert "Duration is shorter" not in v["reasons"][0]
        assert "4.27s" not in v["reasons"][0]
        assert v["forced"] is False

    def test_out_of_band_duration_anomaly_not_suppressed(self):
        p = _packet(
            run_end={"duration_s": 500.0},
            band={"status": "ok", "n": 10, "p10": 90.0, "p90": 120.0},
        )
        assert p["in_band"] is False
        assert p["band_assessment"] == "out_of_band"
        assert p["out_of_band"] == "slow"
        result = _FakeResult({"verdict": "ANOMALY", "reasons": ["duration 500s is slow"]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == ["duration 500s is slow"]
        assert v["forced"] is False

    def test_in_band_fail_with_duration_only_downgraded_to_pass(self):
        p = _packet(run_end={"duration_s": 100.0}, band=BAND)
        assert p["in_band"] is True
        assert p["band_assessment"] == "in_band"
        result = _FakeResult({"verdict": "FAIL", "reasons": ["run was fast"]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == ["duration in band; out_of_band is null"]
        assert v["forced"] is False

    def test_insufficient_history_duration_anomaly_not_downgraded(self):
        """A newly degrading job with no good-run history must not get a false PASS.

        Regression for the three-state review: duration-citing reasons are only
        stripped when the band has actually cleared the run.
        """
        p = _packet(
            run_end={"duration_s": 1142.95},
            band={"status": "insufficient_history", "n": 2},
        )
        assert p["in_band"] is False
        assert p["band_assessment"] == "insufficient_history"
        assert p["out_of_band"] is None
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["Duration of 1142.95 seconds is longer than expected for a new job."],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == [
            "Duration of 1142.95 seconds is longer than expected for a new job."
        ]
        assert v["forced"] is False

    def test_in_band_no_longer_reason_survives(self):
        """'no longer ...' is not a duration comparison and must not be stripped."""
        p = _packet(run_end={"duration_s": 100.0}, band=BAND)
        assert p["in_band"] is True
        assert p["band_assessment"] == "in_band"
        result = _FakeResult({"verdict": "ANOMALY", "reasons": ["no longer emitting events"]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == ["no longer emitting events"]
        assert v["forced"] is False

    def test_in_band_http_429s_reason_survives(self):
        """HTTP status plurals like 'HTTP 429s' must not be stripped as a duration."""
        p = _packet(run_end={"duration_s": 100.0}, band=BAND)
        assert p["in_band"] is True
        assert p["band_assessment"] == "in_band"
        result = _FakeResult({"verdict": "ANOMALY", "reasons": ["HTTP 429s in signature_hits"]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == ["HTTP 429s in signature_hits"]
        assert v["forced"] is False

    def test_in_band_pass_with_only_duration_reasons_sanitizes(self):
        """PASS with only duration-citing reasons must drop them, not cite a cleared anomaly."""
        p = _packet(run_end={"duration_s": 100.0}, band=BAND)
        assert p["in_band"] is True
        assert p["band_assessment"] == "in_band"
        result = _FakeResult(
            {
                "verdict": "PASS",
                "reasons": ["Duration of 4.27s is shorter than baseline p90 of 7.95s"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == ["duration in band; out_of_band is null"]
        assert v["forced"] is False


class TestOutOfBandDurationContradiction:
    def test_rejects_short_when_slow(self):
        """A run slower than the upper band must not be called 'short'."""
        p = _packet(run_end={"duration_s": 500.0})
        result = _FakeResult(
            {"verdict": "ANOMALY", "reasons": ["Duration is extremely short (3.34 seconds)"]}
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 1

    def test_rejects_shorter_than_p90_when_slow(self):
        """A slow run cannot be 'shorter than p90'."""
        p = _packet(run_end={"duration_s": 500.0})
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["significantly shorter than the baseline p90 of 120.0"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 1


class TestNoWorkValidation:
    def test_zero_total_jobs_with_result_does_not_anomaly(self):
        """A zero total_jobs delta on a job that produces non-table work is not 'no work'."""
        p = _packet(
            run_end={
                "duration_s": 100.0,
                "db_delta": {"total_jobs": 0},
                "result": {"reformatted": 164},
            }
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["No jobs or backlog changes in db_delta, suggesting no work was done"],
            }
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 1


class TestStringifiedResultEvidence:
    def test_stringified_all_zero_dict_is_not_evidence(self):
        """A stringified all-zero result dict is NOT evidence of work."""
        packet = {
            "db_delta": {"total_jobs": 0},
            "result": "{'jobs_found': 0, 'jobs_new': 0}",
        }
        assert _has_evidence_of_work(packet) is False

    def test_stringified_nonzero_dict_is_evidence(self):
        """A stringified result dict with a non-zero value IS evidence of work."""
        packet = {
            "db_delta": {"total_jobs": 0},
            "result": "{'jobs_found': 5, 'jobs_new': 3}",
        }
        assert _has_evidence_of_work(packet) is True

    def test_truncated_dict_shaped_repr_is_not_evidence(self):
        """A dict-shaped repr that fails literal_eval (truncated by _RESULT_CLIP)
        must NOT fall back to the bare-string heuristic.

        The whole point of parsing is to avoid misreading an all-zero result as
        work. A truncated dict could be all-zeros — we cannot verify it carries
        non-zero values, so it must be treated as no evidence rather than as
        truthy. Falling back to bare-string truthiness here would over-reject a
        genuine no-work reason, the exact fabrication class this issue targets.
        """
        # All-zero prefix, but the repr is truncated mid-value so literal_eval
        # raises SyntaxError (unterminated string literal).
        packet = {
            "db_delta": {"total_jobs": 0},
            "result": "{'jobs_found': 0, 'jobs_new': 0, 'long_key': 'cut off val",
        }
        assert _has_evidence_of_work(packet) is False

    def test_stringified_all_zero_result_keeps_no_work_reason(self):
        """End-to-end: a stringified all-zero result must NOT contradict a no-work
        reason — the reason is kept and the verdict is not downgraded by it."""
        p = _packet(
            run_end={
                "duration_s": 100.0,
                "db_delta": {"total_jobs": 0},
                "result": "{'jobs_found': 0, 'jobs_new': 0}",
            }
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["No jobs or backlog changes in db_delta, suggesting no work was done"],
            }
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        # The no-work reason is NOT contradicted (all-zero result is not evidence),
        # so it survives and the ANOMALY verdict stands.
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == [
            "No jobs or backlog changes in db_delta, suggesting no work was done"
        ]
        assert v["rejected_reasons"] == 0

    def test_stringified_nonzero_result_rejects_no_work_reason(self):
        """End-to-end: a stringified non-zero result DOES contradict a no-work
        reason — the reason is rejected and the verdict downgrades to PASS."""
        p = _packet(
            run_end={
                "duration_s": 100.0,
                "db_delta": {"total_jobs": 0},
                "result": "{'jobs_found': 5, 'jobs_new': 3}",
            }
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["No jobs or backlog changes in db_delta, suggesting no work was done"],
            }
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 1


class TestNegativeProgressValidation:
    def test_negative_backlog_decrease_is_not_unusual(self):
        """A decrease in a progress counter is expected, not anomalous."""
        p = _packet(
            run_end={
                "duration_s": 100.0,
                "db_delta": {"missing_jd_full": -2},
                "result": {"auto_updated": 2},
            }
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["decrease in missing_jd_full by -2, which is unusual"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 1


class TestFailVerdictDowngrade:
    def test_fail_with_sole_contradicted_no_work_reason_downgrades_to_pass(self):
        """FAIL whose only reason is a fabricated no-work claim must downgrade to PASS."""
        p = _packet(
            run_end={
                "duration_s": 100.0,
                "db_delta": {"total_jobs": 4},
            }
        )
        result = _FakeResult(
            {
                "verdict": "FAIL",
                "reasons": ["No jobs or backlog changes in db_delta, suggesting no work was done"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["forced"] is False
        assert v["rejected_reasons"] == 1

    def test_fail_with_sole_contradicted_duration_reason_downgrades_to_pass(self):
        """FAIL whose only reason contradicts the deterministic band must downgrade to PASS."""
        p = _packet(run_end={"duration_s": 500.0})
        assert p["out_of_band"] == "slow"
        result = _FakeResult(
            {
                "verdict": "FAIL",
                "reasons": ["Duration is extremely short (3.34 seconds)"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["forced"] is False
        assert v["rejected_reasons"] == 1

    def test_fail_with_surviving_reason_is_not_downgraded(self):
        """FAIL with at least one non-contradicted reason must stay FAIL."""
        p = _packet(
            run_end={
                "duration_s": 100.0,
                "db_delta": {"total_jobs": 4},
            }
        )
        result = _FakeResult(
            {
                "verdict": "FAIL",
                "reasons": [
                    "No jobs or backlog changes in db_delta, suggesting no work was done",
                    "database is locked in log_excerpt",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "FAIL"
        assert v["reasons"] == ["database is locked in log_excerpt"]
        assert v["forced"] is False
        assert v["rejected_reasons"] == 1


class TestOutOfBandAssertionContradiction:
    def test_rejects_fast_assertion_when_packet_is_slow(self):
        """A reason asserting 'out_of_band: fast' when the packet says slow is fabricated."""
        p = _packet(run_end={"duration_s": 500.0})
        assert p["out_of_band"] == "slow"
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["out_of_band: fast — run completed too quickly"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 1

    def test_rejects_slow_assertion_when_packet_is_fast(self):
        """A reason asserting 'out_of_band: slow' when the packet says fast is fabricated."""
        p = _packet(run_end={"duration_s": 50.0})
        assert p["out_of_band"] == "fast"
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["out_of_band: slow — run took unusually long"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 1

    def test_rejects_null_assertion_when_packet_is_out_of_band(self):
        """A reason asserting 'out_of_band: null' when the packet is out-of-band is fabricated."""
        p = _packet(run_end={"duration_s": 500.0})
        assert p["out_of_band"] == "slow"
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["out_of_band: null — duration within normal band"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 1

    def test_correct_out_of_band_assertion_survives(self):
        """A reason asserting the correct out_of_band value must NOT be rejected."""
        p = _packet(run_end={"duration_s": 500.0})
        assert p["out_of_band"] == "slow"
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["out_of_band: slow — run exceeded the upper band"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == ["out_of_band: slow — run exceeded the upper band"]


class TestAmbiguousOnlyEvidenceGuard:
    def test_ambiguous_only_anomaly_downgraded_to_pass(self):
        p = _packet(
            run_end={"duration_s": 100.0},
            band=BAND,
            shared_signature_hits=[
                {
                    "pattern": "enrichment worker timed out",
                    "severity": "anomaly",
                    "line": "2026-08-11 02:17:21 WARNING enrichment: worker timed out",
                    "ts": "2026-08-11 02:17:21",
                    "attribution": "ambiguous",
                }
            ],
        )
        assert p["in_band"] is True
        assert p["band_assessment"] == "in_band"
        assert p["signature_hits"] == []
        assert p["disposition"] == "completed"
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "Ambiguous attribution of log lines suggests overlapping "
                    "concurrent runs or interleaved logs."
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["forced"] is False
        assert "ambiguous" in v["reasons"][0].lower()

    def test_own_signature_hit_keeps_anomaly(self):
        """A run with its own signature hit is not rescued by the ambiguous guard."""
        p = _packet(
            run_end={"duration_s": 100.0},
            band=BAND,
            hits=[
                {
                    "pattern": "database is locked",
                    "severity": "fail",
                    "line": "2026-08-11 02:00:00 ERROR db: database is locked",
                    "ts": "2026-08-11 02:00:00",
                }
            ],
            shared_signature_hits=[
                {
                    "pattern": "enrichment worker timed out",
                    "severity": "anomaly",
                    "line": "2026-08-11 02:17:21 WARNING enrichment: worker timed out",
                    "ts": "2026-08-11 02:17:21",
                    "attribution": "ambiguous",
                }
            ],
        )
        result = _FakeResult({"verdict": "ANOMALY", "reasons": ["own signature hit"]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"

    def test_worsened_db_delta_keeps_anomaly(self):
        """A real db_delta worsening is independent anomaly evidence; guard does not fire."""
        p = _packet(
            run_end={"duration_s": 100.0, "db_delta": {"scoring_backlog": 10}},
            band=BAND,
            shared_signature_hits=[
                {
                    "pattern": "enrichment worker timed out",
                    "severity": "anomaly",
                    "line": "2026-08-11 02:17:21 WARNING enrichment: worker timed out",
                    "ts": "2026-08-11 02:17:21",
                    "attribution": "ambiguous",
                }
            ],
        )
        assert p["signature_hits"] == []
        result = _FakeResult({"verdict": "ANOMALY", "reasons": ["scoring_backlog worsened"]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"

    def test_non_ambiguous_shared_hit_keeps_anomaly(self):
        """A shared hit without the ambiguous marker is not sole-ambiguous evidence."""
        p = _packet(
            run_end={"duration_s": 100.0},
            band=BAND,
            shared_signature_hits=[
                {
                    "pattern": "database is locked",
                    "severity": "fail",
                    "line": "2026-08-11 02:00:00 ERROR db: database is locked",
                    "ts": "2026-08-11 02:00:00",
                }
            ],
        )
        result = _FakeResult({"verdict": "ANOMALY", "reasons": ["shared hit"]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"

    def test_failed_disposition_not_downgraded(self):
        """A failed run is never rescued by the ambiguous guard."""
        # disposition=failed forces FAIL before the model is even called.
        p = _packet(
            run_end={"duration_s": 100.0, "disposition": "failed", "error": "boom"},
            band=BAND,
            shared_signature_hits=[
                {
                    "pattern": "enrichment worker timed out",
                    "severity": "anomaly",
                    "line": "2026-08-11 02:17:21 WARNING enrichment: worker timed out",
                    "ts": "2026-08-11 02:17:21",
                    "attribution": "ambiguous",
                }
            ],
        )
        v = checkpoint_verdict(p)
        assert v["verdict"] == "FAIL"
        assert v["forced"] is True

    def test_out_of_band_not_downgraded(self):
        """An out-of-band run is not rescued; the band itself is anomaly signal."""
        p = _packet(
            run_end={"duration_s": 500.0},
            band={"status": "ok", "n": 10, "p10": 90.0, "p90": 120.0},
            shared_signature_hits=[
                {
                    "pattern": "enrichment worker timed out",
                    "severity": "anomaly",
                    "line": "2026-08-11 02:17:21 WARNING enrichment: worker timed out",
                    "ts": "2026-08-11 02:17:21",
                    "attribution": "ambiguous",
                }
            ],
        )
        assert p["band_assessment"] == "out_of_band"
        result = _FakeResult({"verdict": "ANOMALY", "reasons": ["duration slow"]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"


class TestDbDeltaMisread:
    def test_build_packet_semantic_summary_improvement_label(self):
        p = build_packet(
            {**RUN_END, "db_delta": {"missing_jd_full": -1, "scoring_backlog": -3}},
            hits=[],
            log_excerpt="",
            band=BAND,
            db_delta_tracked=True,
        )
        assert p["db_delta_summary"]["by_counter"]["missing_jd_full"]["label"] == "improved_by_1"
        assert p["db_delta_summary"]["by_counter"]["scoring_backlog"]["label"] == "improved_by_3"
        assert p["db_delta_summary"]["by_counter"]["total_jobs"]["label"] == "unchanged"

    def test_negative_missing_jd_full_suppressed(self):
        p = _packet(
            run_end={
                "job": "Backup",
                "disposition": "completed",
                "error": None,
                "duration_s": 60.0,
                "db_delta": {
                    "total_jobs": 0,
                    "scoring_backlog": 0,
                    "classification_null": 0,
                    "missing_jd_full": -1,
                    "first_seen_today": 0,
                },
                "result": "{'status': 'success', 'rotated': 1, 'db_backup': 'backups/jobs.db.2026-07-31'}",
            },
            log_excerpt="2026-07-31 01:00:01 INFO Backup: {'status': 'success', 'rotated': 1}",
            db_delta_tracked=False,
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "db_delta indicates a missing jd_full (-1), suggesting data inconsistency."
                ],
            }
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        assert v["verdict"] != "ANOMALY"
        assert not any("jd_full" in r.lower() for r in v["reasons"])

    def test_all_zero_untracked_company_linkage_suppressed(self):
        p = _packet(
            run_end={
                "job": "Company linkage",
                "disposition": "completed",
                "error": None,
                "duration_s": 90.0,
                "db_delta": {
                    "total_jobs": 0,
                    "scoring_backlog": 0,
                    "classification_null": 0,
                    "missing_jd_full": 0,
                    "first_seen_today": 0,
                },
                "result": "{'linked': 10, 'new_companies': 0, 'matched': 10, 'skipped': 8}",
            },
            log_excerpt="2026-07-31 05:00:45 INFO link_jobs_to_companies complete: linked=10, new_companies=0, matched=10, skipped=8",
            db_delta_tracked=False,
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "db_delta shows no changes, confirming no work was performed",
                    "Log excerpt indicates no activity",
                ],
            }
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        assert v["verdict"] != "ANOMALY"
        assert not any("no work" in r.lower() or "no changes" in r.lower() for r in v["reasons"])

    def test_tracked_all_zero_with_success_not_flagged_as_no_work(self):
        p = _packet(
            run_end={
                "job": "Ingestion",
                "disposition": "completed",
                "error": None,
                "duration_s": 120.0,
                "db_delta": {
                    "total_jobs": 0,
                    "scoring_backlog": 0,
                    "classification_null": 0,
                    "missing_jd_full": 0,
                    "first_seen_today": 0,
                },
                "result": "{'jobs_found': 0, 'jobs_new': 0}",
            },
            log_excerpt="2026-07-31 08:00:00 INFO ingestion complete: jobs_found=0, jobs_new=0",
            db_delta_tracked=True,
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["db_delta is all zero, indicating no work performed"],
            }
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        # For a tracked job with all-zero and no positive success token, the
        # post-return guard does not suppress the no-work reason.
        assert v["verdict"] == "ANOMALY"

    def test_first_run_unknown_all_zero_with_success_suppressed(self):
        """A no-history job must not have db_delta_tracked collapsed to True.

        With no historical tracking signal, an all-zero db_delta and a no-work
        reason should still be contradicted by a success token in the log.
        """
        p = _packet(
            run_end={
                "job": "First-run untracked",
                "disposition": "completed",
                "error": None,
                "duration_s": 30.0,
                "db_delta": {
                    "total_jobs": 0,
                    "scoring_backlog": 0,
                    "classification_null": 0,
                    "missing_jd_full": 0,
                    "first_seen_today": 0,
                },
                "result": None,
            },
            log_excerpt="2026-07-31 09:00:00 INFO link_jobs_to_companies complete: linked=2, matched=1",
            db_delta_tracked=None,
        )
        assert p["db_delta_tracked"] is None
        assert p["db_delta_summary"]["tracked"] is None
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["db_delta is all zero, indicating no work performed"],
            }
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        assert v["verdict"] == "PASS"
        assert not any("no work" in r.lower() for r in v["reasons"])

    def test_increase_counter_anomaly_not_suppressed(self):
        """A total_jobs/first_seen_today increase with anomalous magnitude is not a sign misread."""
        p = _packet(
            run_end={
                "job": "Ingestion",
                "disposition": "completed",
                "error": None,
                "duration_s": 120.0,
                "db_delta": {
                    "total_jobs": 100,
                    "scoring_backlog": 0,
                    "classification_null": 0,
                    "missing_jd_full": 0,
                    "first_seen_today": 0,
                },
                "result": None,
            },
            log_excerpt="",
            db_delta_tracked=True,
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["total_jobs increased by 100, an anomalously large spike"],
            }
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        assert v["verdict"] == "ANOMALY"
        assert any("total_jobs" in r.lower() for r in v["reasons"])

    def test_success_branch_does_not_suppress_worsened_counter_reasons(self):
        """A success token must not suppress a legitimate worsened-counter concern."""
        p = _packet(
            run_end={
                "job": "Ingestion",
                "disposition": "completed",
                "error": None,
                "duration_s": 120.0,
                "db_delta": {
                    "total_jobs": 0,
                    "scoring_backlog": 0,
                    # classification_null moves with missing_jd_full so this is
                    # a sanctioned-clear shape (bodies emptied AND scores
                    # invalidated) rather than the #1951 defect signature (bodies
                    # emptied WITHOUT invalidation, which the deterministic
                    # jd_full-loss guard now catches before the model runs). This
                    # test targets the _sanitize_verdict success-branch, not the
                    # invariant guard.
                    "classification_null": 2,
                    "missing_jd_full": 2,
                    "first_seen_today": 0,
                },
                "result": "{'linked': 2, 'matched': 1}",
            },
            log_excerpt="2026-07-31 09:00:00 INFO link_jobs_to_companies complete: linked=2, matched=1",
            db_delta_tracked=None,
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["db_delta for missing_jd_full worsened by 2, indicating data loss"],
            }
        )
        mock_cm = MagicMock(return_value=result)
        v = checkpoint_verdict(p, call_model=mock_cm)
        mock_cm.assert_called_once()
        # Worsened counter is real damage; do not downgrade to PASS.
        assert v["verdict"] == "ANOMALY"
        assert any("missing_jd_full" in r.lower() for r in v["reasons"])


class TestSuppressionHeuristics:
    def test_is_no_work_reason_word_boundaries_and_generic_phrases(self):
        assert _is_no_work_reason("The run did no work") is True
        assert _is_no_work_reason("The run was a no-op") is True
        assert _is_no_work_reason("Workflow completed normally") is False
        assert _is_no_work_reason("The system was flat") is False
        assert _is_no_work_reason("Log excerpt indicates minimal activity") is False
        assert _is_no_work_reason("The run was unchanged") is False
        assert _is_no_work_reason("No changes observed") is False
        # Generic phrases only count when they co-occur with a db_delta reference.
        assert _is_no_work_reason("db_delta is flat") is True
        assert _is_no_work_reason("db_delta shows no changes") is True

    def test_is_improvement_reason_only_suppresses_decrease_counters(self):
        summary = {
            "by_counter": {
                "missing_jd_full": {
                    "raw_delta": -1,
                    "label": "improved_by_1",
                    "improvement_direction": "decrease",
                },
                "total_jobs": {
                    "raw_delta": 100,
                    "label": "improved_by_100",
                    "improvement_direction": "increase",
                },
            }
        }
        # Decrease counter framed as a defect: suppress.
        assert (
            _is_improvement_reason(
                "db_delta indicates a missing jd_full (-1), suggesting data inconsistency.",
                summary,
            )
            is True
        )
        # Increase counter with anomalous magnitude: keep.
        assert (
            _is_improvement_reason(
                "total_jobs increased by 100, an anomalously large spike",
                summary,
            )
            is False
        )

    def test_negative_framing_word_boundaries(self):
        """'error' must not match inside 'errorless' when checking counter framing."""
        summary = {
            "by_counter": {
                "missing_jd_full": {
                    "raw_delta": -1,
                    "label": "improved_by_1",
                    "improvement_direction": "decrease",
                }
            }
        }
        # The improved counter is mentioned, but the only negative token is
        # 'error' as a substring of 'errorless' — that must not count as a
        # defect-frame and trigger suppression.
        assert (
            _is_improvement_reason("errorless execution still saw missing_jd_full improve", summary)
            is False
        )

    def test_has_success_excerpt_requires_success_key_after_complete(self):
        # Positive failure/skip counts are not success.
        assert (
            _has_success_excerpt(
                "batch complete: failed=1, skipped=2",
                DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS,
            )
            is False
        )
        # Positive success key after complete: success.
        assert (
            _has_success_excerpt(
                "link_jobs_to_companies complete: linked=10, skipped=8",
                DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS,
            )
            is True
        )
        # Success key without a complete marker is not counted.
        assert (
            _has_success_excerpt(
                "linked=10 without a complete marker",
                DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS,
            )
            is False
        )

    def test_empty_success_keys_falls_back_to_default(self):
        """A degenerate empty success key set must not match every =N token."""
        # Empty config (frozenset()) used to match any key=N after complete:.
        assert _has_success_excerpt("batch complete: failed=1, skipped=2", frozenset()) is False
        assert _has_success_excerpt("batch complete: linked=1", frozenset()) is True


class TestJobTracksDbDelta:
    def test_history_with_nonzero_delta_returns_true(self):
        events = [
            {"event": "run_end", "job": "ATS scan", "db_delta": {"total_jobs": 5}},
            {"event": "run_end", "job": "ATS scan", "db_delta": {"total_jobs": 0}},
        ]
        assert job_tracks_db_delta_from_history("ATS scan", events) is True

    def test_history_all_zero_returns_false(self):
        events = [
            {"event": "run_end", "job": "Backup", "db_delta": {"total_jobs": 0}},
            {"event": "run_end", "job": "Backup", "db_delta": {"missing_jd_full": 0}},
        ]
        assert job_tracks_db_delta_from_history("Backup", events) is False

    def test_no_history_returns_none(self):
        assert job_tracks_db_delta_from_history("Backup", []) is None

    def test_non_attributable_run_end_ignored(self):
        """A contaminated overlap (concurrent_run_ids non-empty) must not flip
        a non-writer job to tracked. Issue #1734: the helper used to return
        True on the first non-zero counter in any historical run_end."""
        events = [
            {
                "event": "run_end",
                "job": "Backup",
                "db_delta": {"total_jobs": 1, "classification_null": 1},
                "concurrent_run_ids": ["Careers crawl:1:1"],
            },
        ]
        assert job_tracks_db_delta_from_history("Backup", events) is None

    def test_non_attributable_does_not_mask_attributable_true(self):
        """An attributable run_end with a real non-zero delta still yields True
        even when a non-attributable run_end is also present."""
        events = [
            {
                "event": "run_end",
                "job": "ATS scan",
                "db_delta": {"total_jobs": 5},
                "concurrent_run_ids": ["other:1:1"],
            },
            {
                "event": "run_end",
                "job": "ATS scan",
                "db_delta": {"total_jobs": 3},
                "concurrent_run_ids": [],
            },
        ]
        assert job_tracks_db_delta_from_history("ATS scan", events) is True

    def test_only_non_attributable_nonzero_yields_none_not_true(self):
        """A job whose only non-zero deltas are non-attributable yields None."""
        events = [
            {
                "event": "run_end",
                "job": "Backup",
                "db_delta": {"total_jobs": 0},
                "concurrent_run_ids": [],
            },
            {
                "event": "run_end",
                "job": "Backup",
                "db_delta": {"total_jobs": 1},
                "concurrent_run_ids": ["Careers crawl:1:1"],
            },
        ]
        assert job_tracks_db_delta_from_history("Backup", events) is False

    def test_legacy_run_end_without_concurrent_run_ids_treated_attributable(self):
        """Old run_ends without the concurrent_run_ids field are attributable."""
        events = [
            {"event": "run_end", "job": "ATS scan", "db_delta": {"total_jobs": 5}},
        ]
        assert job_tracks_db_delta_from_history("ATS scan", events) is True


class TestDbDeltaAttributable:
    def test_solo_run_is_attributable(self):
        p = _packet(run_end={"db_delta": {"total_jobs": 4}})
        assert p["db_delta_attributable"] is True
        assert p["concurrent_run_ids"] == []
        assert p["db_delta_summary"]["attributable"] is True

    def test_overlapping_run_is_not_attributable(self):
        p = _packet(
            run_end={
                "db_delta": {"total_jobs": 1, "classification_null": 1},
                "concurrent_run_ids": ["Careers crawl:37252:1787034014"],
            }
        )
        assert p["db_delta_attributable"] is False
        assert p["concurrent_run_ids"] == ["Careers crawl:37252:1787034014"]
        assert p["db_delta_summary"]["attributable"] is False

    def test_non_attributable_labels_counters_not_attributable(self):
        """Every non-zero counter is labelled not_attributable; no
        improved_by_N / worsened_by_N label is emitted."""
        p = _packet(
            run_end={
                "db_delta": {
                    "total_jobs": 124,
                    "classification_null": 118,
                    "missing_jd_full": 118,
                    "scoring_backlog": -41,
                },
                "concurrent_run_ids": ["other:1:1"],
            }
        )
        by_counter = p["db_delta_summary"]["by_counter"]
        for key in ("total_jobs", "classification_null", "missing_jd_full", "scoring_backlog"):
            assert by_counter[key]["label"] == "not_attributable", key
        # Zero counters are also not_attributable (a zero is not evidence of
        # "no work" any more than a non-zero is evidence of this run's work).
        assert by_counter["first_seen_today"]["label"] == "not_attributable"

    def test_attributable_run_keeps_improved_worsened_labels(self):
        p = _packet(
            run_end={
                "db_delta": {"total_jobs": 4, "classification_null": -2},
                "concurrent_run_ids": [],
            }
        )
        by_counter = p["db_delta_summary"]["by_counter"]
        assert by_counter["total_jobs"]["label"] == "improved_by_4"
        assert by_counter["classification_null"]["label"] == "improved_by_2"

    def test_guard_drops_db_delta_reasons_when_not_attributable(self):
        """An ANOMALY whose reasons all cite db_delta counters is downgraded to
        PASS when the delta is not attributable."""
        p = _packet(
            run_end={
                "db_delta": {"classification_null": 1, "missing_jd_full": 1},
                "concurrent_run_ids": ["Careers crawl:37252:1787034014"],
            }
        )
        assert p["db_delta_attributable"] is False
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "classification_null worsened by 1",
                    "missing_jd_full worsened by 1",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 2

    def test_guard_preserves_non_db_delta_reasons_when_not_attributable(self):
        """A non-db_delta reason survives the guard; only db_delta reasons drop."""
        p = _packet(
            run_end={
                "db_delta": {"classification_null": 1},
                "concurrent_run_ids": ["other:1:1"],
            }
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "classification_null worsened by 1",
                    "signature hit: database is locked",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == ["signature hit: database is locked"]
        assert v["rejected_reasons"] == 1

    def test_guard_noop_when_attributable(self):
        """A solo run with a genuine non-zero delta keeps its db_delta reasons."""
        p = _packet(
            run_end={
                "db_delta": {"classification_null": 5, "missing_jd_full": 3},
                "concurrent_run_ids": [],
            }
        )
        assert p["db_delta_attributable"] is True
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "classification_null worsened by 5",
                    "missing_jd_full worsened by 3",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == [
            "classification_null worsened by 5",
            "missing_jd_full worsened by 3",
        ]
        assert v["rejected_reasons"] == 0

    def test_guard_drops_db_delta_field_reference(self):
        """A reason referencing 'db_delta' generically is also dropped."""
        p = _packet(
            run_end={
                "db_delta": {"total_jobs": 1},
                "concurrent_run_ids": ["other:1:1"],
            }
        )
        result = _FakeResult({"verdict": "ANOMALY", "reasons": ["unexplained db_delta movement"]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["rejected_reasons"] == 1

    def test_prompt_states_db_delta_attributable(self):
        assert "db_delta_attributable" in _SYSTEM
        assert "not_attributable" in _SYSTEM
        assert "concurrent_run_ids" in _SYSTEM


class TestLogExcerptNonAttribution:
    def test_prompt_declares_excerpt_scoping(self):
        # The prompt must NOT claim the excerpt is unconditionally scoped.
        assert "log_excerpt is scoped to this run's window" not in _SYSTEM
        # It must describe the structural split.
        assert "concurrent_context" in _SYSTEM
        assert "log_excerpt_is_job_scoped" in _SYSTEM
        assert "log_excerpt_status" in _SYSTEM

    def test_prompt_prohibits_excerpt_absence_as_anomaly(self):
        """The prompt must tell the model not to cite excerpt absence as anomaly (#2013)."""
        assert "capture_unavailable" in _SYSTEM
        assert "captured_empty" in _SYSTEM
        assert "MUST NOT issue an" in _SYSTEM
        assert "evidence-availability caveat" in _SYSTEM

    def test_prompt_carries_non_attribution_instruction(self):
        """The model must be told not to treat unscoped or concurrent lines as evidence."""
        assert "treat them as cross-job noise" in _SYSTEM
        assert "never as evidence about this job" in _SYSTEM
        assert "another job" in _SYSTEM
        # Concrete cross-job log sources observed in the false-verdict packets
        # are named so the model recognizes the attribution trap.
        assert "stale_detector" in _SYSTEM
        assert "expiry_checker" in _SYSTEM

    def test_prompt_says_absent_log_content_is_not_anomaly(self):
        assert "Absent or unrelated log content is NOT evidence of anomaly" in _SYSTEM

    def test_packet_carries_job_scoped_flag_defaulting_false(self):
        """The packet must make the scoped/unscoped nature explicit (#1488)."""
        p = _packet()
        assert "log_excerpt_is_job_scoped" in p
        # Default is False: callers that do not perform the split get an unscoped tail.
        assert p["log_excerpt_is_job_scoped"] is False
        assert p["log_excerpt_status"] == "capture_unavailable"

    def test_packet_carries_job_scoped_flag_when_set(self):
        p = build_packet(
            RUN_END,
            hits=[],
            log_excerpt="",
            band=BAND,
            log_excerpt_status="captured_empty",
        )
        assert p["log_excerpt_is_job_scoped"] is True
        assert p["log_excerpt_status"] == "captured_empty"

    def test_packet_carries_status_captured_non_empty(self):
        p = build_packet(
            RUN_END,
            hits=[],
            log_excerpt="some log line",
            band=BAND,
            log_excerpt_status="captured_non_empty",
        )
        assert p["log_excerpt_is_job_scoped"] is True
        assert p["log_excerpt_status"] == "captured_non_empty"

    def test_packet_carries_concurrent_context_default_empty(self):
        p = build_packet(RUN_END, hits=[], log_excerpt="line", band=BAND)
        assert "concurrent_context" in p
        assert p["concurrent_context"] == ""

    def test_verdict_passes_packet_with_scoping_to_model(self):
        """The scoping flag and concurrent_context reach the model in the prompt JSON."""
        captured: dict = {}

        def fake_call_model(*args, **kwargs):
            captured["messages"] = kwargs.get("messages") or (args[1] if len(args) > 1 else [])
            return _FakeResult({"verdict": "PASS", "reasons": []})

        p = _packet()
        p["log_excerpt_is_job_scoped"] = True
        p["log_excerpt_status"] = "captured_non_empty"
        p["concurrent_context"] = "stale_detector warning"
        checkpoint_verdict(p, call_model=fake_call_model)

        body = json.loads(captured["messages"][-1]["content"])
        assert body["log_excerpt_is_job_scoped"] is True
        assert body["log_excerpt_status"] == "captured_non_empty"
        assert body["concurrent_context"] == "stale_detector warning"


class TestLogExcerptStatusGuard:
    def test_drops_excerpt_absence_reason_on_capture_unavailable(self):
        """An ANOMALY reason solely about the empty excerpt is dropped when
        capture is unavailable; the verdict is downgraded to PASS with a
        caveat note."""
        p = _packet()
        p["log_excerpt_status"] = "capture_unavailable"
        p["log_excerpt_is_job_scoped"] = False
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "Log excerpt is empty, unable to verify job-specific log evidence for health."
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["rejected_reasons"] == 1
        assert any("capture unavailable" in r.lower() for r in v["reasons"])

    def test_drops_excerpt_absence_reason_on_captured_empty(self):
        """An ANOMALY reason solely about the empty excerpt is dropped when
        capture is captured_empty (the job genuinely emitted no lines)."""
        p = _packet()
        p["log_excerpt_status"] = "captured_empty"
        p["log_excerpt_is_job_scoped"] = True
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["No log excerpt provided for analysis"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["rejected_reasons"] == 1

    def test_drops_issue_quoted_absence_reason_with_hyphenated_qualifier(self):
        """Regression for the regex gap in _EXCERPT_ABSENCE_RE (#2013 review).

        The issue's own quoted manufactured-verdict example —
        "No job-scoped log content to confirm expected activity" (from
        checkpoint_Scheduled-ingestion_53992_1788015600.json) — has a
        multi-word, hyphenated qualifier ("job-scoped log") between "no"
        and the absence noun ("content"). The prior single-\\w* form only
        admitted one bare word between "no" and the noun, so this string
        was NOT classified as an excerpt-absence reason and survived the
        guard as a false escalation. It must now be dropped.
        """
        literal = "No job-scoped log content to confirm expected activity"
        assert _is_excerpt_absence_reason(literal) is True
        p = _packet()
        p["log_excerpt_status"] = "captured_empty"
        p["log_excerpt_is_job_scoped"] = True
        result = _FakeResult({"verdict": "ANOMALY", "reasons": [literal]})
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["rejected_reasons"] == 1
        assert literal not in v["reasons"]

    def test_keeps_content_citing_reason_on_captured_non_empty(self):
        """A reason that cites specific content found in the excerpt (e.g.
        'database is locked in log_excerpt') is NOT dropped — it references a
        present string, not an absence."""
        p = build_packet(
            RUN_END,
            hits=[],
            log_excerpt="2026-08-30 03:00:00 ERROR database is locked",
            band=BAND,
            log_excerpt_status="captured_non_empty",
        )
        result = _FakeResult(
            {
                "verdict": "FAIL",
                "reasons": ["database is locked in log_excerpt"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        # The content-citing reason survives — it is not an excerpt-absence reason.
        assert "database is locked in log_excerpt" in v["reasons"]

    def test_keeps_combined_reason_with_other_evidence(self):
        """When an excerpt-absence reason is dropped but other reasons
        survive, the verdict is not downgraded — only the absence reason is
        removed.

        The surviving reason must be one no other guard independently
        strips. The prior form used "duration is unusually long", but the
        default packet is in_band (duration_s=100 vs p10=90/p90=120), so
        _guard_in_band_duration dropped it too — emptying the verdict and
        downgrading to PASS, which the weak ``rejected_reasons >= 1``
        assertion could not detect. "HTTP 429s in signature_hits" cites a
        non-duration, non-db_delta, non-excerpt signal that every guard
        passes through (proven by test_in_band_http_429s_reason_survives),
        so it isolates the excerpt-absence guard as the only dropper.
        """
        p = _packet()
        p["log_excerpt_status"] = "capture_unavailable"
        p["log_excerpt_is_job_scoped"] = False
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "Log excerpt is empty, indicating no job-scoped log lines",
                    "HTTP 429s in signature_hits",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        # The verdict stays ANOMALY: the surviving HTTP-429 reason keeps it
        # escalated; only the excerpt-absence reason is dropped.
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == ["HTTP 429s in signature_hits"]
        assert v["rejected_reasons"] == 1


# The nine Scheduled-ingestion checkpoint packets from the 2026-08-22 ->
# 2026-08-23 05:30 window tabulated in issue #1893. Each row is
# (total_jobs, classification_null, missing_jd_full); first_seen_today equals
# total_jobs for every solo ingestion run (the rows just inserted).
_ISSUE_1893_PACKETS = [
    (3, 3, 3),
    (19, 19, 19),
    (1, 1, 1),
    (37, 37, 34),
    (7, 7, 6),
    (37, 37, 33),
    (11, 11, 10),
    (2, 2, 1),
    (10, 10, 10),
]


def _ingestion_packet(total_jobs, classification_null, missing_jd_full):
    """Build a Scheduled-ingestion checkpoint packet matching the issue table."""
    return _packet(
        run_end={
            "job": "Scheduled ingestion",
            "run_id": f"Scheduled ingestion:1:{total_jobs}",
            "disposition": "completed",
            "error": None,
            "duration_s": 100.0,
            "db_delta": {
                "total_jobs": total_jobs,
                "scoring_backlog": 0,
                "classification_null": classification_null,
                "missing_jd_full": missing_jd_full,
                "first_seen_today": total_jobs,
            },
            "result": f"{{'jobs_found': {total_jobs}, 'jobs_new': {total_jobs}}}",
            "concurrent_run_ids": [],
        },
        log_excerpt=f"ingestion complete: jobs_found={total_jobs}, jobs_new={total_jobs}",
        db_delta_tracked=True,
    )


class TestNewRowBacklogLabelling:
    def test_fully_bounded_increase_labelled_pending(self):
        summary = _db_delta_summary(
            {
                "total_jobs": 10,
                "first_seen_today": 10,
                "classification_null": 10,
                "missing_jd_full": 10,
                "scoring_backlog": 0,
            },
            tracked=True,
            attributable=True,
        )
        by = summary["by_counter"]
        assert by["classification_null"]["label"] == "pending_from_new_rows_10"
        assert by["missing_jd_full"]["label"] == "pending_from_new_rows_10"
        assert by["total_jobs"]["label"] == "improved_by_10"
        assert by["first_seen_today"]["label"] == "improved_by_10"
        assert by["scoring_backlog"]["label"] == "unchanged"

    def test_partial_excess_labelled_worsened_by_excess_only(self):
        """Growth exceeding new-row count keeps escalating on the excess."""
        summary = _db_delta_summary(
            {
                "total_jobs": 10,
                "first_seen_today": 10,
                "classification_null": 15,
                "missing_jd_full": 10,
                "scoring_backlog": 0,
            },
            tracked=True,
            attributable=True,
        )
        by = summary["by_counter"]
        # 15 - 10 new rows = 5 genuine backlog.
        assert by["classification_null"]["label"] == "worsened_by_5"
        assert by["classification_null"]["raw_delta"] == 15
        assert by["missing_jd_full"]["label"] == "pending_from_new_rows_10"

    def test_no_new_rows_keeps_worsened(self):
        """When total_jobs and first_seen_today are both 0, an increase is
        genuine backlog accumulation and must still escalate."""
        summary = _db_delta_summary(
            {
                "total_jobs": 0,
                "first_seen_today": 0,
                "classification_null": 4,
                "missing_jd_full": 0,
                "scoring_backlog": 0,
            },
            tracked=True,
            attributable=True,
        )
        assert summary["by_counter"]["classification_null"]["label"] == "worsened_by_4"

    def test_non_attributable_unaffected_by_new_row_bounding(self):
        """Non-attributable deltas are all not_attributable regardless of new
        rows; the new-row bound does not apply (the movement is not this
        run's work to begin with)."""
        summary = _db_delta_summary(
            {
                "total_jobs": 10,
                "first_seen_today": 10,
                "classification_null": 10,
                "missing_jd_full": 10,
                "scoring_backlog": 0,
            },
            tracked=True,
            attributable=False,
        )
        for key in ("total_jobs", "classification_null", "missing_jd_full"):
            assert summary["by_counter"][key]["label"] == "not_attributable", key

    def test_first_seen_today_alone_bounds_growth(self):
        """first_seen_today is the direct new-row measure; it bounds growth
        even when total_jobs did not rise (e.g. deletions in-window)."""
        summary = _db_delta_summary(
            {
                "total_jobs": 0,
                "first_seen_today": 8,
                "classification_null": 8,
                "missing_jd_full": 8,
                "scoring_backlog": 0,
            },
            tracked=True,
            attributable=True,
        )
        assert summary["by_counter"]["classification_null"]["label"] == "pending_from_new_rows_8"
        assert summary["by_counter"]["missing_jd_full"]["label"] == "pending_from_new_rows_8"

    def test_scoring_backlog_not_bounded(self):
        """Sabotage tripwire: scoring_backlog is NOT new-row-bounded.

        db_counters() (run_events.py) defines scoring_backlog as rows WITH
        jd_full and a NULL classification; a freshly ingested row is
        jd_full-missing by construction, so it can never enter scoring_backlog
        on the run that inserted it. A scoring_backlog rise must therefore
        still escalate as genuine backlog even when total_jobs /
        first_seen_today also rose in the same delta — if scoring_backlog is
        ever re-added to `_NEW_ROW_BOUNDED_COUNTERS`, this assertion flips to
        `pending_from_new_rows_5` and fails."""
        summary = _db_delta_summary(
            {
                "total_jobs": 5,
                "first_seen_today": 5,
                "classification_null": 5,
                "missing_jd_full": 5,
                "scoring_backlog": 5,
            },
            tracked=True,
            attributable=True,
        )
        assert summary["by_counter"]["scoring_backlog"]["label"] == "worsened_by_5"


class TestNewRowBacklogGuard:
    def test_fully_bounded_packet_does_not_escalate(self):
        """Acceptance: total_jobs +10 / first_seen_today +10 /
        classification_null +10 / missing_jd_full +10 does not produce an
        ANOMALY on its own, even when the model returns ANOMALY citing the
        counters as worsened."""
        p = _ingestion_packet(10, 10, 10)
        assert p["db_delta_summary"]["by_counter"]["classification_null"]["label"] == (
            "pending_from_new_rows_10"
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "The classification_null count worsened by 10, indicating issues with job classification.",
                    "The missing_jd_full count also worsened by 10, suggesting problems with job descriptions.",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "PASS"
        assert v["reasons"] == []
        assert v["rejected_reasons"] == 2

    def test_excess_growth_still_escalates(self):
        """Acceptance: classification_null growing by more than the new-row
        count still labels the excess as worsened and still escalates."""
        p = _ingestion_packet(10, 15, 10)
        assert (
            p["db_delta_summary"]["by_counter"]["classification_null"]["label"] == "worsened_by_5"
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["classification_null worsened by 5, backlog accumulation"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["rejected_reasons"] == 0

    def test_zero_new_rows_still_escalates(self):
        """Acceptance: classification_null growing while total_jobs and
        first_seen_today are both 0 still escalates."""
        p = _packet(
            run_end={
                "job": "Scheduled ingestion",
                "disposition": "completed",
                "error": None,
                "duration_s": 100.0,
                "db_delta": {
                    "total_jobs": 0,
                    "scoring_backlog": 0,
                    "classification_null": 4,
                    "missing_jd_full": 0,
                    "first_seen_today": 0,
                },
                "result": None,
                "concurrent_run_ids": [],
            },
            db_delta_tracked=True,
        )
        assert (
            p["db_delta_summary"]["by_counter"]["classification_null"]["label"] == "worsened_by_4"
        )
        result = _FakeResult(
            {"verdict": "ANOMALY", "reasons": ["classification_null worsened by 4"]}
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["rejected_reasons"] == 0

    def test_guard_preserves_non_db_delta_reason(self):
        """A non-db_delta reason (e.g. a signature hit) survives the guard even
        when a bounded counter is also cited alongside it as a separate
        reason; the bounded-counter reason drops, the signature reason keeps
        the verdict escalated."""
        p = _ingestion_packet(10, 10, 10)
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "classification_null worsened by 10",
                    "signature hit: database is locked",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == ["signature hit: database is locked"]
        assert v["rejected_reasons"] == 1

    def test_guard_keeps_reason_citing_mixed_pending_and_worsened(self):
        """A single reason citing both a pending and a genuinely-worsened
        counter is kept (the worsened part is a legitimate basis)."""
        p = _ingestion_packet(10, 15, 10)
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["classification_null and missing_jd_full worsened, backlog growing"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["rejected_reasons"] == 0

    def test_guard_noop_when_no_pending_counters(self):
        """The guard is a no-op (0 dropped) when no counter is pending."""
        p = _packet(
            run_end={
                "db_delta": {"classification_null": 5, "missing_jd_full": 3},
                "concurrent_run_ids": [],
            },
            db_delta_tracked=True,
        )
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "classification_null worsened by 5",
                    "missing_jd_full worsened by 3",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["rejected_reasons"] == 0

    def test_scoring_backlog_reason_survives_guard_alongside_bounded_counters(self):
        """Sabotage tripwire at the guard layer (the layer where #1893's
        false-negative actually fired): a reason citing only scoring_backlog
        must survive `_guard_new_row_backlog` even in the same delta where
        classification_null / missing_jd_full are fully new-row-bounded.
        Pre-fix (scoring_backlog in `_NEW_ROW_BOUNDED_COUNTERS`), scoring_backlog
        would also be in `pending`, the reason's citation would be a subset of
        `pending`, and the guard would drop it — downgrading ANOMALY to PASS
        and silently suppressing genuine backlog growth."""
        p = _packet(
            run_end={
                "job": "Scheduled ingestion",
                "disposition": "completed",
                "error": None,
                "duration_s": 100.0,
                "db_delta": {
                    "total_jobs": 10,
                    "first_seen_today": 10,
                    "classification_null": 10,
                    "missing_jd_full": 10,
                    "scoring_backlog": 3,
                },
                "result": None,
                "concurrent_run_ids": [],
            },
            db_delta_tracked=True,
        )
        assert p["db_delta_summary"]["by_counter"]["scoring_backlog"]["label"] == "worsened_by_3"
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": ["scoring_backlog worsened by 3, genuine backlog growth"],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        assert v["verdict"] == "ANOMALY"
        assert v["reasons"] == ["scoring_backlog worsened by 3, genuine backlog growth"]
        assert v["rejected_reasons"] == 0


class TestIssue1893NamedPacketsDeterministic:
    @pytest.mark.parametrize("total_jobs,classification_null,missing_jd_full", _ISSUE_1893_PACKETS)
    def test_named_packet_passes_regardless_of_model_wording(
        self, total_jobs, classification_null, missing_jd_full
    ):
        p = _ingestion_packet(total_jobs, classification_null, missing_jd_full)
        # Every bounded counter is pending, never worsened.
        for key in ("classification_null", "missing_jd_full"):
            assert p["db_delta_summary"]["by_counter"][key]["label"].startswith(
                "pending_from_new_rows_"
            ), key
        # Two distinct model-wording shapes that both produced ANOMALY in
        # production; the guard must downgrade both deterministically. Both
        # cite only the pending counters (classification_null / missing_jd_full).
        for reasons in (
            [
                f"The classification_null count worsened by {classification_null}, indicating issues with job classification.",
                f"The missing_jd_full count also worsened by {missing_jd_full}, suggesting problems with job descriptions.",
            ],
            ["classification_null and missing_jd_full worsened, suggesting problems"],
        ):
            result = _FakeResult({"verdict": "ANOMALY", "reasons": reasons})
            v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
            assert v["verdict"] == "PASS", (total_jobs, reasons)


class TestIssue1893Sabotage:
    def test_sabotage_relabelling_pending_as_worsened_re_escalates(self):
        p = _ingestion_packet(10, 10, 10)
        # Simulate the pre-fix labelling: pending counters become worsened.
        for key in ("classification_null", "missing_jd_full"):
            entry = p["db_delta_summary"]["by_counter"][key]
            entry["label"] = f"worsened_by_{entry['raw_delta']}"
        result = _FakeResult(
            {
                "verdict": "ANOMALY",
                "reasons": [
                    "classification_null worsened by 10",
                    "missing_jd_full worsened by 10",
                ],
            }
        )
        v = checkpoint_verdict(p, call_model=MagicMock(return_value=result))
        # With the pre-fix labels, no guard drops the reasons -> ANOMALY stands.
        assert v["verdict"] == "ANOMALY"

    def test_guard_directly_noop_when_no_pending_label(self):
        """Direct unit check: _guard_new_row_backlog drops nothing when the
        summary has no pending_from_new_rows label (the pre-fix shape)."""
        p = _ingestion_packet(10, 10, 10)
        for key in ("classification_null", "missing_jd_full"):
            entry = p["db_delta_summary"]["by_counter"][key]
            entry["label"] = f"worsened_by_{entry['raw_delta']}"
        verdict, reasons, dropped = _guard_new_row_backlog(p, "ANOMALY", ["counter worsened"])
        assert verdict == "ANOMALY"
        assert dropped == 0


class TestPromptDescribesPendingLabel:
    def test_prompt_mentions_pending_from_new_rows(self):
        assert "pending_from_new_rows_N" in _SYSTEM
        assert "MUST NOT be cited as an anomaly" in _SYSTEM

"""Tests for jobcannon/host/nightly/checkpoint_verdict.py (ledger L-0471).

This module's own docstring names its guard chain (_sanitize_verdict ->
_guard_in_band_duration -> _validate_reasons -> _guard_non_attributable_db_delta
-> _guard_new_row_backlog -> _guard_excerpt_absence -> _guard_ambiguous_only_evidence)
a fidelity anchor: a set of pure functions over whatever the packet carries.
These tests exercise the top-level checkpoint_verdict() entry point end to
end (forced FAIL, call_model injection fail-safe, unparseable verdict, and
each guard firing in isolation), plus a couple of the guard-internal
predicates directly.
"""

from __future__ import annotations

from jobcannon.host.nightly.checkpoint_packet import (
    LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE,
    LOG_EXCERPT_STATUS_CAPTURED_EMPTY,
)
from jobcannon.host.nightly.checkpoint_verdict import (
    VERDICT_UNAVAILABLE,
    _AMBIGUOUS_ONLY_NOTE,
    _CAPTURE_UNAVAILABLE_NOTE,
    _PASS_NOTE,
    _guard_excerpt_absence,
    _guard_new_row_backlog,
    _guard_non_attributable_db_delta,
    _is_excerpt_absence_reason,
    _reason_contradicts,
    _sanitize_verdict,
    _validate_reasons,
    checkpoint_verdict,
)


def _packet(**overrides):
    base = {
        "job": "scan",
        "run_id": "r1",
        "disposition": "completed",
        "duration_s": 10.0,
        "error": None,
        "result": None,
        "log_excerpt": "",
        "log_excerpt_status": None,
        "band_assessment": "insufficient_history",
        "out_of_band": None,
        "baseline": {"status": "insufficient_history", "n": 0},
        "signature_hits": [],
        "shared_signature_hits": [],
        "db_delta": None,
        "db_delta_tracked": None,
        "db_delta_attributable": True,
    }
    base.update(overrides)
    return base


class _FakeResult:
    def __init__(self, data):
        self.data = data


def _model(verdict, reasons):
    def _call(**kwargs):
        return _FakeResult({"verdict": verdict, "reasons": reasons})

    return _call


def _raising_model(exc):
    def _call(**kwargs):
        raise exc

    return _call


# --- Top-level checkpoint_verdict() ------------------------------------


def test_forced_fail_on_disposition_failed_skips_model_call():
    packet = _packet(disposition="failed", error="boom")
    result = checkpoint_verdict(packet)  # no call_model needed on this path
    assert result == {
        "verdict": "FAIL",
        "reasons": ["disposition=failed (error=boom)"],
        "forced": True,
        "rejected_reasons": 0,
    }


def test_call_model_default_none_yields_verdict_unavailable():
    packet = _packet()
    result = checkpoint_verdict(packet)
    assert result["verdict"] == VERDICT_UNAVAILABLE
    assert result["forced"] is False
    assert result["rejected_reasons"] == 0
    assert "TypeError" in result["reasons"][0]


def test_call_model_raising_yields_verdict_unavailable_with_exception_detail():
    packet = _packet()
    result = checkpoint_verdict(packet, call_model=_raising_model(RuntimeError("cascade exhausted")))
    assert result["verdict"] == VERDICT_UNAVAILABLE
    assert result["reasons"] == ["verdict call failed: RuntimeError: cascade exhausted"]


def test_unparseable_model_verdict_yields_anomaly():
    packet = _packet()
    result = checkpoint_verdict(packet, call_model=_model("MAYBE", []))
    assert result == {
        "verdict": "ANOMALY",
        "reasons": ["unparseable model verdict"],
        "forced": False,
        "rejected_reasons": 0,
    }


def test_happy_path_pass_with_no_reasons():
    packet = _packet()
    result = checkpoint_verdict(packet, call_model=_model("PASS", []))
    assert result == {
        "verdict": "PASS",
        "reasons": [],
        "forced": False,
        "rejected_reasons": 0,
    }


def test_in_band_duration_guard_strips_duration_reason_and_downgrades():
    packet = _packet(band_assessment="in_band", out_of_band=None)
    result = checkpoint_verdict(
        packet, call_model=_model("ANOMALY", ["duration exceeded the p90 baseline"])
    )
    assert result["verdict"] == "PASS"
    assert result["reasons"] == _PASS_NOTE
    assert result["rejected_reasons"] == 1


def test_validate_reasons_guard_drops_contradicted_out_of_band_claim():
    packet = _packet(band_assessment="out_of_band", out_of_band="slow")
    result = checkpoint_verdict(packet, call_model=_model("ANOMALY", ["run is out_of_band: fast"]))
    assert result["verdict"] == "PASS"
    assert result["reasons"] == []
    assert result["rejected_reasons"] == 1


def test_non_attributable_db_delta_guard_drops_counter_citing_reason():
    packet = _packet(
        db_delta_attributable=False,
        db_delta_summary={
            "tracked": True,
            "attributable": False,
            "by_counter": {"classification_null": {"raw_delta": 4, "label": "not_attributable"}},
        },
    )
    result = checkpoint_verdict(
        packet, call_model=_model("ANOMALY", ["classification_null worsened this run"])
    )
    assert result["verdict"] == "PASS"
    assert result["reasons"] == []
    assert result["rejected_reasons"] == 1


def test_new_row_backlog_guard_drops_reason_citing_only_pending_counter():
    packet = _packet(
        db_delta_summary={
            "tracked": True,
            "attributable": True,
            "by_counter": {
                "classification_null": {"raw_delta": 3, "label": "pending_from_new_rows_3"}
            },
        }
    )
    result = checkpoint_verdict(
        packet, call_model=_model("ANOMALY", ["classification_null rose due to new rows"])
    )
    assert result["verdict"] == "PASS"
    assert result["reasons"] == []
    assert result["rejected_reasons"] == 1


def test_excerpt_absence_guard_drops_reason_and_downgrades_with_note():
    packet = _packet(log_excerpt_status=LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE)
    result = checkpoint_verdict(
        packet, call_model=_model("ANOMALY", ["log excerpt is empty and missing"])
    )
    assert result["verdict"] == "PASS"
    assert result["reasons"] == _CAPTURE_UNAVAILABLE_NOTE
    assert result["rejected_reasons"] == 1


def test_ambiguous_only_evidence_guard_downgrades_without_incrementing_rejected_count():
    packet = _packet(
        disposition="completed",
        band_assessment="in_band",
        signature_hits=[],
        error=None,
        shared_signature_hits=[{"field": "status", "attribution": "ambiguous"}],
        db_delta_summary={"tracked": None, "attributable": True, "by_counter": {}},
    )
    result = checkpoint_verdict(
        packet, call_model=_model("ANOMALY", ["ambiguous shared hit suggests an issue"])
    )
    assert result["verdict"] == "PASS"
    assert result["reasons"] == _AMBIGUOUS_ONLY_NOTE
    # The note substitution is a structural downgrade, not a falsified-reason
    # rejection, per the module docstring -- it must not inflate the count.
    assert result["rejected_reasons"] == 0


def test_sanitize_verdict_guard_drops_no_work_reason_and_downgrades():
    packet = _packet(
        db_delta_tracked=False,
        db_delta_summary={
            "tracked": False,
            "attributable": True,
            "by_counter": {"total_jobs": {"raw_delta": 0, "label": "unchanged"}},
        },
    )
    result = checkpoint_verdict(
        packet, call_model=_model("ANOMALY", ["db_delta shows no work was performed"])
    )
    assert result["verdict"] == "PASS"
    assert result["reasons"] == []
    assert result["rejected_reasons"] == 1


# --- Guard-internal predicates, exercised directly ----------------------


def test_reason_contradicts_out_of_band_mismatch():
    packet = _packet(band_assessment="out_of_band", out_of_band="slow")
    assert _reason_contradicts(packet, "the run was fast, out_of_band=fast") is True
    assert _reason_contradicts(packet, "the run was slow, out_of_band=slow") is False


def test_reason_contradicts_no_work_claim_refuted_by_result_evidence():
    packet = _packet(result={"jobs_found": 3})
    assert _reason_contradicts(packet, "db_delta suggests no work was done") is True


def test_reason_contradicts_leaves_unrelated_reason_alone():
    packet = _packet()
    assert _reason_contradicts(packet, "signature hit indicates a startup warning") is False


def test_validate_reasons_keeps_uncontradicted_reason():
    packet = _packet()
    verdict, reasons, rejected = _validate_reasons(packet, "ANOMALY", ["startup warning observed"])
    assert verdict == "ANOMALY"
    assert reasons == ["startup warning observed"]
    assert rejected == 0


def test_is_excerpt_absence_reason_matches_absence_not_content():
    assert _is_excerpt_absence_reason("No log excerpt provided for analysis") is True
    assert _is_excerpt_absence_reason("Log excerpt is empty, unable to verify activity") is True
    assert _is_excerpt_absence_reason("database is locked, per log_excerpt content") is False
    assert _is_excerpt_absence_reason("classification_null worsened this run") is False


def test_guard_excerpt_absence_is_noop_when_captured_non_empty():
    packet = _packet(log_excerpt_status="captured_non_empty")
    verdict, reasons, dropped = _guard_excerpt_absence(packet, "ANOMALY", ["log excerpt is empty"])
    assert (verdict, reasons, dropped) == ("ANOMALY", ["log excerpt is empty"], 0)


def test_guard_excerpt_absence_keeps_non_absence_reason_alongside_dropped_one():
    packet = _packet(log_excerpt_status=LOG_EXCERPT_STATUS_CAPTURED_EMPTY)
    verdict, reasons, dropped = _guard_excerpt_absence(
        packet, "ANOMALY", ["no log excerpt found", "signature hit: db locked"]
    )
    assert verdict == "ANOMALY"
    assert reasons == ["signature hit: db locked"]
    assert dropped == 1


def test_guard_non_attributable_db_delta_is_noop_when_attributable():
    packet = _packet(db_delta_attributable=True)
    verdict, reasons, dropped = _guard_non_attributable_db_delta(
        packet, "ANOMALY", ["classification_null worsened"]
    )
    assert (verdict, reasons, dropped) == ("ANOMALY", ["classification_null worsened"], 0)


def test_guard_new_row_backlog_is_noop_when_no_pending_counters():
    packet = _packet(
        db_delta_summary={
            "tracked": True,
            "attributable": True,
            "by_counter": {"classification_null": {"raw_delta": 4, "label": "worsened_by_4"}},
        }
    )
    verdict, reasons, dropped = _guard_new_row_backlog(
        packet, "ANOMALY", ["classification_null worsened"]
    )
    assert (verdict, reasons, dropped) == ("ANOMALY", ["classification_null worsened"], 0)


def test_sanitize_verdict_downgrades_to_pass_when_success_contradicts_no_work_reason():
    packet = _packet(
        disposition="completed",
        error=None,
        result="status: success",
        db_delta_summary={
            "tracked": None,
            "attributable": True,
            "by_counter": {"total_jobs": {"raw_delta": 0, "label": "unchanged"}},
        },
    )
    verdict, reasons = _sanitize_verdict(packet, "ANOMALY", ["no activity in db_delta"])
    assert verdict == "PASS"
    assert reasons == []

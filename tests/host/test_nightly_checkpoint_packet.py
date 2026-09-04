"""Tests for jobcannon/host/nightly/checkpoint_packet.py (ledger L-0471).

Covers db_delta_summary's four label kinds (unchanged/improved/worsened/
pending_from_new_rows), the not_attributable degrade-path, and build_packet's
band_assessment / log_excerpt_status normalization -- the packet-assembly
half of the checkpoint_verdict.py fidelity anchor.
"""

from __future__ import annotations

from jobcannon.host.nightly.checkpoint_packet import (
    LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE,
    LOG_EXCERPT_STATUS_CAPTURED_EMPTY,
    LOG_EXCERPT_STATUS_CAPTURED_NON_EMPTY,
    _db_delta_summary,
    _new_row_count,
    build_packet,
    job_tracks_db_delta_from_history,
)


def test_new_row_count_picks_max_of_increase_counters():
    assert _new_row_count({"total_jobs": 3, "first_seen_today": 5}) == 5
    assert _new_row_count({"total_jobs": -1, "first_seen_today": 0}) == 0
    assert _new_row_count({}) == 0


def test_db_delta_summary_none_delta_is_all_unchanged():
    summary = _db_delta_summary(None, tracked=None)
    for info in summary["by_counter"].values():
        assert info["label"] == "unchanged"
        assert info["raw_delta"] == 0
    assert summary["tracked"] is None
    assert summary["attributable"] is True


def test_db_delta_summary_improved_and_worsened_directions():
    raw = {
        "total_jobs": 5,  # increase-direction, positive -> improved
        "scoring_backlog": -3,  # decrease-direction, negative -> improved
        "classification_null": 4,  # decrease-direction, positive, but no new
        "missing_jd_full": 0,  # rows this run -> not bounded, so worsened
        "first_seen_today": 0,
    }
    summary = _db_delta_summary(raw, tracked=True)
    by = summary["by_counter"]
    assert by["total_jobs"]["label"] == "improved_by_5"
    assert by["scoring_backlog"]["label"] == "improved_by_3"
    # total_jobs rose (new_rows=5) so classification_null's rise of 4 is
    # fully within the new-row bound -> pending, not worsened.
    assert by["classification_null"]["label"] == "pending_from_new_rows_4"
    assert by["missing_jd_full"]["label"] == "unchanged"


def test_db_delta_summary_worsened_when_no_new_rows_this_run():
    # No new rows this run (total_jobs/first_seen_today both 0) -> any rise
    # in a new-row-bounded counter is genuine backlog, not pending.
    raw = {"total_jobs": 0, "first_seen_today": 0, "classification_null": 4}
    summary = _db_delta_summary(raw, tracked=True)
    assert summary["by_counter"]["classification_null"]["label"] == "worsened_by_4"


def test_db_delta_summary_pending_from_new_rows_bounded_and_excess():
    # 3 new rows this run; classification_null rose by 2 (fully bounded) and
    # missing_jd_full rose by 5 (3 bounded + 2 excess -> worsened_by_2).
    raw = {
        "total_jobs": 3,
        "first_seen_today": 3,
        "classification_null": 2,
        "missing_jd_full": 5,
        "scoring_backlog": 0,
    }
    summary = _db_delta_summary(raw, tracked=True)
    by = summary["by_counter"]
    assert by["classification_null"]["label"] == "pending_from_new_rows_2"
    assert by["missing_jd_full"]["label"] == "worsened_by_2"


def test_db_delta_summary_not_attributable_labels_everything():
    raw = {"total_jobs": 5, "scoring_backlog": -3, "classification_null": 0}
    summary = _db_delta_summary(raw, tracked=True, attributable=False)
    assert summary["attributable"] is False
    for info in summary["by_counter"].values():
        assert info["label"] == "not_attributable"


def test_db_delta_summary_preserves_unknown_counters():
    summary = _db_delta_summary({"weird_counter": 7}, tracked=None)
    assert summary["by_counter"]["weird_counter"]["raw_delta"] == 7
    assert summary["by_counter"]["weird_counter"]["improvement_direction"] is None


def test_job_tracks_db_delta_from_history():
    events = [
        {"event": "run_end", "job": "scan", "db_delta": {"total_jobs": 0}},
        {"event": "run_end", "job": "scan", "db_delta": {"total_jobs": 3}},
    ]
    assert job_tracks_db_delta_from_history("scan", events) is True

    all_zero = [{"event": "run_end", "job": "backup", "db_delta": {"total_jobs": 0}}]
    assert job_tracks_db_delta_from_history("backup", all_zero) is False

    assert job_tracks_db_delta_from_history("unknown_job", events) is None


def test_job_tracks_db_delta_from_history_ignores_non_attributable():
    # A non-empty delta on a run with concurrent_run_ids must not establish
    # tracking -- it may belong to a sibling run.
    events = [
        {
            "event": "run_end",
            "job": "scan",
            "db_delta": {"total_jobs": 5},
            "concurrent_run_ids": ["other-run"],
        }
    ]
    assert job_tracks_db_delta_from_history("scan", events) is None


def _band_ok(p10=10.0, p90=20.0):
    return {"status": "ok", "n": 5, "p10": p10, "p90": p90}


def test_build_packet_band_assessment_states():
    insufficient = build_packet(
        {"job": "scan", "run_id": "r1", "disposition": "completed", "duration_s": 15.0},
        hits=[],
        log_excerpt="",
        band={"status": "insufficient_history", "n": 2},
    )
    assert insufficient["band_assessment"] == "insufficient_history"
    assert insufficient["in_band"] is False

    in_band = build_packet(
        {"job": "scan", "run_id": "r2", "disposition": "completed", "duration_s": 15.0},
        hits=[],
        log_excerpt="",
        band=_band_ok(),
    )
    assert in_band["band_assessment"] == "in_band"
    assert in_band["out_of_band"] is None
    assert in_band["in_band"] is True

    out_of_band_pkt = build_packet(
        {"job": "scan", "run_id": "r3", "disposition": "completed", "duration_s": 999.0},
        hits=[],
        log_excerpt="",
        band=_band_ok(),
    )
    assert out_of_band_pkt["band_assessment"] == "out_of_band"
    assert out_of_band_pkt["out_of_band"] == "slow"
    assert out_of_band_pkt["in_band"] is False


def test_build_packet_log_excerpt_status_normalizes_unknown_to_unavailable():
    pkt = build_packet(
        {"job": "scan", "run_id": "r1", "disposition": "completed", "duration_s": 1.0},
        hits=[],
        log_excerpt="",
        band={"status": "insufficient_history", "n": 0},
        log_excerpt_status="not_a_real_status",
    )
    assert pkt["log_excerpt_status"] == LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE
    assert pkt["log_excerpt_is_job_scoped"] is False


def test_build_packet_log_excerpt_status_scoped_states():
    for status, scoped in (
        (LOG_EXCERPT_STATUS_CAPTURED_NON_EMPTY, True),
        (LOG_EXCERPT_STATUS_CAPTURED_EMPTY, True),
        (LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE, False),
    ):
        pkt = build_packet(
            {"job": "scan", "run_id": "r1", "disposition": "completed", "duration_s": 1.0},
            hits=[],
            log_excerpt="",
            band={"status": "insufficient_history", "n": 0},
            log_excerpt_status=status,
        )
        assert pkt["log_excerpt_is_job_scoped"] is scoped


def test_build_packet_concurrency_fields_default_empty_and_attributable():
    pkt = build_packet(
        {"job": "scan", "run_id": "r1", "disposition": "completed", "duration_s": 1.0},
        hits=[],
        log_excerpt="",
        band={"status": "insufficient_history", "n": 0},
    )
    assert pkt["concurrent_run_ids"] == []
    assert pkt["shared_signature_hits"] == []
    assert pkt["concurrent_context"] == ""
    assert pkt["db_delta_attributable"] is True


def test_build_packet_non_empty_concurrent_run_ids_sets_not_attributable():
    pkt = build_packet(
        {
            "job": "scan",
            "run_id": "r1",
            "disposition": "completed",
            "duration_s": 1.0,
            "concurrent_run_ids": ["r2"],
        },
        hits=[],
        log_excerpt="",
        band={"status": "insufficient_history", "n": 0},
    )
    assert pkt["db_delta_attributable"] is False
    assert pkt["db_delta_summary"]["attributable"] is False

"""Tests for jobcannon/host/nightly/baselines.py (ledger L-0471).

baselines.py's own module docstring names this a fidelity anchor: percentile
derivation must diff clean against the private original, and the port
changes only the import line. These tests exercise duration_band/
out_of_band/_percentile directly against the DEFAULT_NIGHTLY_* constants
this port carries byte-identical from private job_finder/config.py.
"""

from __future__ import annotations

from jobcannon.host.nightly.baselines import (
    DEFAULT_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S,
    DEFAULT_NIGHTLY_OUT_OF_BAND_TOLERANCE,
    _percentile,
    duration_band,
    out_of_band,
)


def _run_end(job: str, duration_s: float, disposition: str = "completed") -> dict:
    return {"event": "run_end", "job": job, "disposition": disposition, "duration_s": duration_s}


def test_percentile_linear_interpolation():
    assert _percentile([10.0], 0.5) == 10.0
    assert _percentile([10.0, 20.0], 0.5) == 15.0
    assert _percentile([10.0, 20.0, 30.0, 40.0, 50.0], 0.10) == 14.0
    assert _percentile([10.0, 20.0, 30.0, 40.0, 50.0], 0.90) == 46.0


def test_duration_band_insufficient_history_below_min():
    events = [_run_end("scan", 10.0 + i) for i in range(3)]
    band = duration_band("scan", events, window_runs=10, min_history=5)
    assert band == {"status": "insufficient_history", "n": 3}


def test_duration_band_ok_with_enough_history():
    events = [_run_end("scan", 10.0 + i) for i in range(6)]
    band = duration_band("scan", events, window_runs=10, min_history=5)
    assert band["status"] == "ok"
    assert band["n"] == 6
    assert band["p10"] < band["p90"]


def test_duration_band_filters_by_job_name_and_terminal_disposition():
    events = [
        _run_end("scan", 100.0),
        _run_end("scan", 110.0),
        _run_end("scan", 120.0),
        _run_end("scan", 130.0),
        _run_end("scan", 140.0),
        _run_end("other_job", 999.0),
        _run_end("scan", 5.0, disposition="failed"),  # not in _TERMINAL_OK
        {"event": "run_start", "job": "scan", "duration_s": 1.0},  # wrong event
    ]
    band = duration_band("scan", events, window_runs=10, min_history=5)
    assert band["status"] == "ok"
    assert band["n"] == 5


def test_duration_band_window_runs_takes_most_recent():
    events = [_run_end("scan", float(i)) for i in range(20)]
    band = duration_band("scan", events, window_runs=5, min_history=1)
    assert band["n"] == 5
    # Most recent 5 durations are 15..19 -> p10/p90 must fall in that range.
    assert 15.0 <= band["p10"] <= 19.0
    assert 15.0 <= band["p90"] <= 19.0


def test_duration_band_non_positive_window_clamps_to_one():
    events = [_run_end("scan", float(i)) for i in range(20)]
    band = duration_band("scan", events, window_runs=0, min_history=1)
    assert band["n"] == 1


def test_out_of_band_none_when_band_not_ok_or_duration_missing():
    assert out_of_band(10.0, {"status": "insufficient_history", "n": 1}) is None
    assert out_of_band(None, {"status": "ok", "n": 5, "p10": 5.0, "p90": 15.0}) is None


def test_out_of_band_fast_and_slow_directions():
    band = {"status": "ok", "n": 5, "p10": 100.0, "p90": 200.0}
    # Default tolerance 0.25, absolute floor 5.0 -> lower = 100 - 25 = 75, upper = 200 + 50 = 250
    assert out_of_band(74.0, band) == "fast"
    assert out_of_band(76.0, band) is None
    assert out_of_band(251.0, band) == "slow"
    assert out_of_band(249.0, band) is None


def test_out_of_band_absolute_floor_protects_short_bands():
    # p10/p90 close together: relative tolerance alone would flag tiny noise;
    # the absolute floor should dominate.
    band = {"status": "ok", "n": 5, "p10": 2.0, "p90": 3.0}
    lower = 2.0 - DEFAULT_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S
    upper = 3.0 + DEFAULT_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S
    assert out_of_band(lower + 0.01, band) is None
    assert out_of_band(lower - 0.01, band) == "fast"
    assert out_of_band(upper - 0.01, band) is None
    assert out_of_band(upper + 0.01, band) == "slow"


def test_out_of_band_custom_tolerance_and_floor_override_defaults():
    band = {"status": "ok", "n": 5, "p10": 100.0, "p90": 100.0}
    assert out_of_band(100.0, band, tolerance=0.0, absolute_floor_s=0.0) is None
    assert out_of_band(100.01, band, tolerance=0.0, absolute_floor_s=0.0) == "slow"
    assert out_of_band(99.99, band, tolerance=0.0, absolute_floor_s=0.0) == "fast"
    assert DEFAULT_NIGHTLY_OUT_OF_BAND_TOLERANCE == 0.25

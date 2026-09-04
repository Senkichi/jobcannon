"""Tests for jobcannon/host/nightly/disagreement_baseline.py (ledger L-0387).

PORTED from private's tests/test_disagreement_baseline.py
@ bace4081c06da6393adc26e191b228004d6a2f42 (private job-cannon). Ledger
L-0581 (wave-3B seed adjudication, retired into this unit's coverage).

Pure-function unit tests for the disagreement-rate rolling baseline
(record_rate, rate_band, is_anomalous) used by the nightly disagreement-rate
alarm (private #1619) -- no DB, no I/O; the only changes from the private
original are this header and the import path (module moved from
job_finder.web.nightly_monitor._disagreement_baseline to
jobcannon.host.nightly.disagreement_baseline; the caller-side state.json ->
Postgres wiring is exercised separately, matching the private original's own
scope of testing this module in isolation from state persistence).
"""

from __future__ import annotations

from jobcannon.host.nightly.disagreement_baseline import (
    is_anomalous,
    rate_band,
    record_rate,
)


class TestRecordRate:
    def test_qualifying_sample_is_appended(self):
        history = record_rate([0.1, 0.2], 0.3, 25, min_sample=20, window_runs=10)
        assert history == [0.1, 0.2, 0.3]

    def test_sub_min_sample_night_is_dropped(self):
        """The exact 08-19 shape: n=4 must not enter the baseline at all."""
        history = record_rate([0.1, 0.2], 0.5, 4, min_sample=20, window_runs=10)
        assert history == [0.1, 0.2]

    def test_none_rate_is_dropped(self):
        history = record_rate([0.1], None, 30, min_sample=20, window_runs=10)
        assert history == [0.1]

    def test_window_truncates_to_most_recent(self):
        history = record_rate([0.1, 0.2, 0.3], 0.4, 25, min_sample=20, window_runs=3)
        assert history == [0.2, 0.3, 0.4]

    def test_none_history_starts_fresh(self):
        assert record_rate(None, 0.3, 25, min_sample=20, window_runs=10) == [0.3]

    def test_non_numeric_entries_in_prior_history_are_dropped(self):
        history = record_rate([0.1, "corrupt", None], 0.4, 25, min_sample=20, window_runs=10)
        assert history == [0.1, 0.4]


class TestRateBand:
    def test_below_min_history_is_insufficient(self):
        band = rate_band([0.1, 0.2, 0.3], min_history=5)
        assert band == {"status": "insufficient_history", "n": 3}

    def test_meets_min_history_returns_p90(self):
        band = rate_band([0.45, 0.52, 0.48, 0.55, 0.59], min_history=5)
        assert band["status"] == "ok"
        assert band["n"] == 5
        assert 0.55 < band["p90"] <= 0.59

    def test_empty_history_is_insufficient(self):
        assert rate_band([], min_history=1) == {"status": "insufficient_history", "n": 0}


class TestIsAnomalous:
    def test_insufficient_history_never_flags(self):
        band = {"status": "insufficient_history", "n": 1}
        assert is_anomalous(0.99, band, tolerance=0.15, absolute_floor=0.05) is False

    def test_within_tolerance_of_p90_is_not_anomalous(self):
        band = {"status": "ok", "n": 5, "p90": 0.59}
        # 0.6 is barely above p90 but within the tolerance/floor margin.
        assert is_anomalous(0.6, band, tolerance=0.15, absolute_floor=0.05) is False

    def test_far_above_p90_is_anomalous(self):
        band = {"status": "ok", "n": 5, "p90": 0.59}
        assert is_anomalous(0.9, band, tolerance=0.15, absolute_floor=0.05) is True

    def test_absolute_floor_applies_when_p90_is_near_zero(self):
        """A tiny baseline (near-zero disagreement rate) must not make the
        relative tolerance vanishingly small -- the absolute floor covers it."""
        band = {"status": "ok", "n": 5, "p90": 0.01}
        # relative tolerance alone (0.15 * 0.01 = 0.0015) would flag this;
        # the absolute floor (0.05) must keep it silent.
        assert is_anomalous(0.05, band, tolerance=0.15, absolute_floor=0.05) is False
        assert is_anomalous(0.2, band, tolerance=0.15, absolute_floor=0.05) is True

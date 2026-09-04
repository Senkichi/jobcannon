"""Tests for jobcannon/host/nightly/config.py (ledger L-0471).

Covers the env-var-backed tunables that replace private's config.yaml
``nightly_monitor:`` block, plus DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS's
byte-identity to job_finder/config.py's set of the same name (verified by
hand against the private source at e1f47695b07f928e6c91cc64767c97a99645d68f
-- pinned here so a future edit that drifts the set fails a test).
"""

from __future__ import annotations

import pytest

from jobcannon.host.nightly import config as nightly_config

# Exact set from private job_finder/config.py's DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS
# @ e1f47695b07f928e6c91cc64767c97a99645d68f.
_PRIVATE_DEFAULT_SUCCESS_COUNT_KEYS = frozenset(
    {
        "linked",
        "matched",
        "probed",
        "hits",
        "rescored",
        "rotated",
        "processed",
        "succeeded",
        "completed",
        "done",
        "created",
        "inserted",
        "updated",
        "written",
        "fetched",
        "new",
        "scored",
        "found",
        "discovered",
        "crawled",
        "enriched",
        "resolved",
        "checked",
        "new_companies",
        "companies_scanned",
        "jobs_found",
        "jobs_new",
        "jobs_discovered",
        "jobs_scored",
        "homepages_found",
    }
)


def test_default_success_count_keys_byte_identical_to_private():
    assert nightly_config.DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS == _PRIVATE_DEFAULT_SUCCESS_COUNT_KEYS


def test_nightly_monitor_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("JC_NIGHTLY_MONITOR_ENABLED", raising=False)
    assert nightly_config.nightly_monitor_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on", "ON"])
def test_nightly_monitor_enabled_truthy_values(monkeypatch, value):
    monkeypatch.setenv("JC_NIGHTLY_MONITOR_ENABLED", value)
    assert nightly_config.nightly_monitor_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_nightly_monitor_enabled_falsy_values(monkeypatch, value):
    monkeypatch.setenv("JC_NIGHTLY_MONITOR_ENABLED", value)
    assert nightly_config.nightly_monitor_enabled() is False


def test_nightly_monitor_config_defaults(monkeypatch):
    for name in (
        "JC_NIGHTLY_BASELINE_MIN_HISTORY",
        "JC_NIGHTLY_BASELINE_WINDOW_RUNS",
        "JC_NIGHTLY_OUT_OF_BAND_TOLERANCE",
        "JC_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S",
        "JC_NIGHTLY_SUCCESS_COUNT_KEYS",
        "JC_NIGHTLY_MAX_EVENTS_PER_TICK",
        "JC_NIGHTLY_TICK_BUDGET_SECONDS",
        "JC_NIGHTLY_MORNING_HOUR",
        "JC_NIGHTLY_MORNING_MINUTE",
        "JC_NIGHTLY_COVERAGE_GAP_THRESHOLD_S",
        "JC_NIGHTLY_AUDIT_SCORE_THRESHOLD",
        "JC_NIGHTLY_AUDIT_LOOKBACK_DAYS",
        "JC_NIGHTLY_AUDIT_MAX_JOBS",
        "JC_NIGHTLY_AUDIT_BATCH_SIZE",
        "JC_NIGHTLY_AUDIT_MAX_BATCH_INPUT_CHARS",
        "JC_NIGHTLY_AUDIT_MAX_SKIP_ATTEMPTS",
        "JC_NIGHTLY_AUDIT_MAX_BATCH_RETRIES",
        "JC_NIGHTLY_AUDIT_PARALLEL",
        "JC_NIGHTLY_AUDIT_COVERAGE_ALARM_THRESHOLD",
        "JC_NIGHTLY_AUDIT_FAILED_BATCH_FRACTION_ALARM_THRESHOLD",
        "JC_NIGHTLY_MIN_SAMPLE_SIZE_FOR_RATE",
        "JC_NIGHTLY_DISAGREEMENT_ALARM_RATE",
        "JC_NIGHTLY_DISAGREEMENT_ALARM_MIN_SAMPLE",
        "JC_NIGHTLY_DISAGREEMENT_ALARM_ABSOLUTE_CEILING",
        "JC_NIGHTLY_DISAGREEMENT_BASELINE_WINDOW_NIGHTS",
        "JC_NIGHTLY_DISAGREEMENT_BASELINE_MIN_NIGHTS",
        "JC_NIGHTLY_DISAGREEMENT_BASELINE_TOLERANCE",
        "JC_NIGHTLY_DISAGREEMENT_BASELINE_ABSOLUTE_FLOOR",
        "JC_NIGHTLY_ISSUE_REPO",
        "JC_NIGHTLY_GH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = nightly_config.nightly_monitor_config()
    assert cfg == {
        "baseline_min_history": nightly_config.DEFAULT_NIGHTLY_BASELINE_MIN_HISTORY,
        "baseline_window_runs": nightly_config.DEFAULT_NIGHTLY_BASELINE_WINDOW_RUNS,
        "out_of_band_tolerance": nightly_config.DEFAULT_NIGHTLY_OUT_OF_BAND_TOLERANCE,
        "out_of_band_absolute_floor_s": nightly_config.DEFAULT_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S,
        "success_count_keys": nightly_config.DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS,
        "max_events_per_tick": nightly_config.DEFAULT_NIGHTLY_MAX_EVENTS_PER_TICK,
        "tick_budget_seconds": nightly_config.DEFAULT_NIGHTLY_TICK_BUDGET_SECONDS,
        "morning_hour": nightly_config.DEFAULT_NIGHTLY_MORNING_HOUR,
        "morning_minute": nightly_config.DEFAULT_NIGHTLY_MORNING_MINUTE,
        "coverage_gap_threshold_s": nightly_config.DEFAULT_NIGHTLY_COVERAGE_GAP_THRESHOLD_S,
        "audit": {
            "score_threshold": nightly_config.DEFAULT_NIGHTLY_AUDIT_SCORE_THRESHOLD,
            "lookback_days": nightly_config.DEFAULT_NIGHTLY_AUDIT_LOOKBACK_DAYS,
            "max_jobs": nightly_config.DEFAULT_NIGHTLY_AUDIT_MAX_JOBS,
            "batch_size": nightly_config.DEFAULT_NIGHTLY_AUDIT_BATCH_SIZE,
            "max_batch_input_chars": nightly_config.DEFAULT_NIGHTLY_AUDIT_MAX_BATCH_INPUT_CHARS,
            "max_skip_attempts": nightly_config.DEFAULT_NIGHTLY_AUDIT_MAX_SKIP_ATTEMPTS,
            "max_batch_retries": nightly_config.DEFAULT_NIGHTLY_AUDIT_MAX_BATCH_RETRIES,
            "parallel": nightly_config.DEFAULT_NIGHTLY_AUDIT_PARALLEL,
            "coverage_alarm_threshold": nightly_config.DEFAULT_NIGHTLY_AUDIT_COVERAGE_ALARM_THRESHOLD,
            "failed_batch_fraction_alarm_threshold": (
                nightly_config.DEFAULT_NIGHTLY_AUDIT_FAILED_BATCH_FRACTION_ALARM_THRESHOLD
            ),
            "min_sample_size_for_rate": nightly_config.DEFAULT_NIGHTLY_MIN_SAMPLE_SIZE_FOR_RATE,
            "disagreement_alarm_rate": nightly_config.DEFAULT_NIGHTLY_DISAGREEMENT_ALARM_RATE,
            "disagreement_alarm_min_sample": (
                nightly_config.DEFAULT_NIGHTLY_DISAGREEMENT_ALARM_MIN_SAMPLE
            ),
            "disagreement_alarm_absolute_ceiling": (
                nightly_config.DEFAULT_NIGHTLY_DISAGREEMENT_ALARM_ABSOLUTE_CEILING
            ),
            "disagreement_baseline_window_nights": (
                nightly_config.DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_WINDOW_NIGHTS
            ),
            "disagreement_baseline_min_nights": (
                nightly_config.DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_MIN_NIGHTS
            ),
            "disagreement_baseline_tolerance": (
                nightly_config.DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_TOLERANCE
            ),
            "disagreement_baseline_absolute_floor": (
                nightly_config.DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_ABSOLUTE_FLOOR
            ),
        },
        "review": {"repo": None, "token": None},
    }


def test_nightly_monitor_config_env_overrides(monkeypatch):
    monkeypatch.setenv("JC_NIGHTLY_BASELINE_MIN_HISTORY", "3")
    monkeypatch.setenv("JC_NIGHTLY_MAX_EVENTS_PER_TICK", "10")
    monkeypatch.setenv("JC_NIGHTLY_TICK_BUDGET_SECONDS", "30.5")
    monkeypatch.setenv("JC_NIGHTLY_SUCCESS_COUNT_KEYS", "done, shipped ,done")
    cfg = nightly_config.nightly_monitor_config()
    assert cfg["baseline_min_history"] == 3
    assert cfg["max_events_per_tick"] == 10
    assert cfg["tick_budget_seconds"] == 30.5
    assert cfg["success_count_keys"] == frozenset({"done", "shipped"})


def test_nightly_monitor_config_clamps_below_floor(monkeypatch):
    monkeypatch.setenv("JC_NIGHTLY_BASELINE_MIN_HISTORY", "0")
    monkeypatch.setenv("JC_NIGHTLY_MAX_EVENTS_PER_TICK", "-5")
    monkeypatch.setenv("JC_NIGHTLY_OUT_OF_BAND_TOLERANCE", "-1")
    monkeypatch.setenv("JC_NIGHTLY_TICK_BUDGET_SECONDS", "-10")
    cfg = nightly_config.nightly_monitor_config()
    assert cfg["baseline_min_history"] == 1
    assert cfg["max_events_per_tick"] == 1
    assert cfg["out_of_band_tolerance"] == 0.0
    assert cfg["tick_budget_seconds"] == 0.0


def test_nightly_monitor_config_invalid_int_raises(monkeypatch):
    monkeypatch.setenv("JC_NIGHTLY_MAX_EVENTS_PER_TICK", "not-a-number")
    with pytest.raises(RuntimeError):
        nightly_config.nightly_monitor_config()


def test_nightly_monitor_config_empty_csv_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JC_NIGHTLY_SUCCESS_COUNT_KEYS", "   ,  ,")
    cfg = nightly_config.nightly_monitor_config()
    assert cfg["success_count_keys"] == nightly_config.DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS

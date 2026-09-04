"""Nightly-monitor tunables. Ledger L-0471.

# PORT-SEAM: private's ``get_nightly_monitor_config(config: dict)`` reads a
# ``nightly_monitor:`` block out of ``config.yaml``. Hosted has no
# config.yaml at all (jobcannon.host.config's HostConfig module docstring:
# "hosted has no config.yaml"), so every knob here is env-var-backed
# instead, read directly with ``os.environ.get`` -- matching every other
# per-tick tunable in jobcannon.host.tasks (JC_SCAN_INTERVAL_HOURS,
# JC_DB_STORAGE_LIMIT_MB, ...), not routed through the startup-only
# HostConfig dataclass. Numeric literal defaults are carried byte-identical
# from job_finder/config.py's DEFAULT_NIGHTLY_* constants so a hosted
# deployment with everything unset behaves the same as the private
# defaults did.

The sampler/signatures/baselines/checkpoint subset above landed first (#355,
L-0471, dark). L-0387 (morning audit/review/issue-filer/deadman) adds the
``audit`` and ``review`` sub-blocks below, plus the top-level
``morning_hour``/``morning_minute``/``coverage_gap_threshold_s`` keys their
callers (jobcannon.host.nightly.morning_driver, .error_budget, .deadman)
need. ``bash_rats`` has no port target -- design note Q4: no ``charlie``
subprocess on this host, hand-off is ``issue_filer.py``'s ``automated-ready``
label only, so the whole config block DIES rather than landing unwired.

design note Q6 (random-sample reproducibility, JC_NIGHTLY_AUDIT_MAX_JOBS'
lower default) and Q2 (store-UTC/render-local: morning_hour/morning_minute
are UTC integers, not local-clock -- see state.morning_deadline) are the two
places this block's numeric defaults deliberately diverge from private's.
"""

from __future__ import annotations

import os

# Byte-identical to job_finder/config.py's DEFAULT_NIGHTLY_BASELINE_MIN_HISTORY /
# DEFAULT_NIGHTLY_OUT_OF_BAND_TOLERANCE / DEFAULT_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S /
# DEFAULT_NIGHTLY_BASELINE_WINDOW_RUNS / DEFAULT_NIGHTLY_SAMPLER_MAX_EVENTS_PER_TICK /
# DEFAULT_NIGHTLY_SAMPLER_TICK_BUDGET_SECONDS.
DEFAULT_NIGHTLY_BASELINE_MIN_HISTORY = 5
DEFAULT_NIGHTLY_OUT_OF_BAND_TOLERANCE = 0.25
DEFAULT_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S = 5.0
DEFAULT_NIGHTLY_BASELINE_WINDOW_RUNS = 10
DEFAULT_NIGHTLY_MAX_EVENTS_PER_TICK = 50
DEFAULT_NIGHTLY_TICK_BUDGET_SECONDS = 210.0

# L-0387 additions. Byte-identical numeric defaults to job_finder/config.py's
# DEFAULT_NIGHTLY_MORNING_HOUR/MINUTE/COVERAGE_GAP_THRESHOLD_S/AUDIT_*/
# DISAGREEMENT_*/MIN_SAMPLE_SIZE_FOR_RATE, EXCEPT DEFAULT_NIGHTLY_AUDIT_MAX_JOBS
# (design note Sec2/Q6: private's 60 absorbed a first-night backlog that a
# fresh hosted score_audits table has no analog of; hosted default is 15) and
# DEFAULT_NIGHTLY_AUDIT_PARALLEL (design note Sec2: hosted call_model is a
# network call dispatched sequentially by audit_stage.py, not a subprocess
# pool -- private's parallel_sessions=2 has no meaning here, so this key is
# kept only as a documented no-op default of 1, not read by any caller).
DEFAULT_NIGHTLY_MORNING_HOUR = 5
DEFAULT_NIGHTLY_MORNING_MINUTE = 30
DEFAULT_NIGHTLY_COVERAGE_GAP_THRESHOLD_S = 900
DEFAULT_NIGHTLY_AUDIT_SCORE_THRESHOLD = 20
DEFAULT_NIGHTLY_AUDIT_LOOKBACK_DAYS = 3
DEFAULT_NIGHTLY_AUDIT_MAX_JOBS = 15
DEFAULT_NIGHTLY_AUDIT_BATCH_SIZE = 5
DEFAULT_NIGHTLY_AUDIT_MAX_BATCH_INPUT_CHARS = 40_000
DEFAULT_NIGHTLY_AUDIT_MAX_SKIP_ATTEMPTS = 2
DEFAULT_NIGHTLY_AUDIT_MAX_BATCH_RETRIES = 1
DEFAULT_NIGHTLY_AUDIT_PARALLEL = 1
DEFAULT_NIGHTLY_AUDIT_COVERAGE_ALARM_THRESHOLD = 0.80
DEFAULT_NIGHTLY_AUDIT_FAILED_BATCH_FRACTION_ALARM_THRESHOLD = 0.75
DEFAULT_NIGHTLY_MIN_SAMPLE_SIZE_FOR_RATE = 5
DEFAULT_NIGHTLY_DISAGREEMENT_ALARM_RATE = 0.25
DEFAULT_NIGHTLY_DISAGREEMENT_ALARM_MIN_SAMPLE = 20
DEFAULT_NIGHTLY_DISAGREEMENT_ALARM_ABSOLUTE_CEILING = 0.8
DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_WINDOW_NIGHTS = 10
DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_MIN_NIGHTS = 5
DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_TOLERANCE = 0.15
DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_ABSOLUTE_FLOOR = 0.05

# Byte-identical to job_finder/config.py's DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS.
DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS = frozenset(
    {
        "linked", "matched", "probed", "hits", "rescored", "rotated",
        "processed", "succeeded", "completed", "done", "created", "inserted",
        "updated", "written", "fetched", "new", "scored", "found",
        "discovered", "crawled", "enriched", "resolved", "checked",
        "new_companies", "companies_scanned", "jobs_found", "jobs_new",
        "jobs_discovered", "jobs_scored", "homepages_found",
    }
)  # fmt: skip


def nightly_monitor_enabled() -> bool:
    """The master kill switch: JC_NIGHTLY_MONITOR_ENABLED.

    Defaults OFF. Every nightly periodic's body calls this first and
    early-returns ``{"skipped": "disabled"}`` when false -- the periodic
    still *registers* (same shape as jobcannon.host.tasks.db_storage_check),
    so enabling is a pure env change with no redeploy.
    """
    return (os.environ.get("JC_NIGHTLY_MONITOR_ENABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _int_env(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    try:
        return int(val)
    except ValueError as exc:
        raise RuntimeError(f"Invalid value for {name}: {val!r} (expected integer)") from exc


def _float_env(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    try:
        return float(val)
    except ValueError as exc:
        raise RuntimeError(f"Invalid value for {name}: {val!r} (expected float)") from exc


def _csv_env(name: str, default: frozenset) -> frozenset:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    keys = frozenset(k.strip() for k in val.split(",") if k.strip())
    return keys or default


def nightly_monitor_config() -> dict:
    """Fully-defaulted nightly-monitor tunables for the sampler/checkpoint callers.

    # PORT-SEAM: private's ``get_nightly_monitor_config`` took a ``config``
    # dict and merged/coerced a ``nightly_monitor:`` sub-block. This env-var
    # equivalent takes no argument -- there is no per-call config dict to
    # merge against on a host with no config.yaml.
    """
    return {
        "baseline_min_history": max(
            1, _int_env("JC_NIGHTLY_BASELINE_MIN_HISTORY", DEFAULT_NIGHTLY_BASELINE_MIN_HISTORY)
        ),
        "baseline_window_runs": _int_env(
            "JC_NIGHTLY_BASELINE_WINDOW_RUNS", DEFAULT_NIGHTLY_BASELINE_WINDOW_RUNS
        ),
        "out_of_band_tolerance": max(
            0.0,
            _float_env("JC_NIGHTLY_OUT_OF_BAND_TOLERANCE", DEFAULT_NIGHTLY_OUT_OF_BAND_TOLERANCE),
        ),
        "out_of_band_absolute_floor_s": max(
            0.0,
            _float_env(
                "JC_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S",
                DEFAULT_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S,
            ),
        ),
        "success_count_keys": _csv_env(
            "JC_NIGHTLY_SUCCESS_COUNT_KEYS", DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS
        ),
        "max_events_per_tick": max(
            1, _int_env("JC_NIGHTLY_MAX_EVENTS_PER_TICK", DEFAULT_NIGHTLY_MAX_EVENTS_PER_TICK)
        ),
        "tick_budget_seconds": max(
            0.0,
            _float_env("JC_NIGHTLY_TICK_BUDGET_SECONDS", DEFAULT_NIGHTLY_TICK_BUDGET_SECONDS),
        ),
        "morning_hour": _int_env("JC_NIGHTLY_MORNING_HOUR", DEFAULT_NIGHTLY_MORNING_HOUR),
        "morning_minute": _int_env("JC_NIGHTLY_MORNING_MINUTE", DEFAULT_NIGHTLY_MORNING_MINUTE),
        "coverage_gap_threshold_s": max(
            0,
            _int_env(
                "JC_NIGHTLY_COVERAGE_GAP_THRESHOLD_S", DEFAULT_NIGHTLY_COVERAGE_GAP_THRESHOLD_S
            ),
        ),
        "audit": {
            "score_threshold": _int_env(
                "JC_NIGHTLY_AUDIT_SCORE_THRESHOLD", DEFAULT_NIGHTLY_AUDIT_SCORE_THRESHOLD
            ),
            "lookback_days": _int_env(
                "JC_NIGHTLY_AUDIT_LOOKBACK_DAYS", DEFAULT_NIGHTLY_AUDIT_LOOKBACK_DAYS
            ),
            "max_jobs": max(
                0, _int_env("JC_NIGHTLY_AUDIT_MAX_JOBS", DEFAULT_NIGHTLY_AUDIT_MAX_JOBS)
            ),
            "batch_size": max(
                1, _int_env("JC_NIGHTLY_AUDIT_BATCH_SIZE", DEFAULT_NIGHTLY_AUDIT_BATCH_SIZE)
            ),
            "max_batch_input_chars": max(
                1,
                _int_env(
                    "JC_NIGHTLY_AUDIT_MAX_BATCH_INPUT_CHARS",
                    DEFAULT_NIGHTLY_AUDIT_MAX_BATCH_INPUT_CHARS,
                ),
            ),
            "max_skip_attempts": max(
                0,
                _int_env(
                    "JC_NIGHTLY_AUDIT_MAX_SKIP_ATTEMPTS", DEFAULT_NIGHTLY_AUDIT_MAX_SKIP_ATTEMPTS
                ),
            ),
            "max_batch_retries": max(
                0,
                _int_env(
                    "JC_NIGHTLY_AUDIT_MAX_BATCH_RETRIES", DEFAULT_NIGHTLY_AUDIT_MAX_BATCH_RETRIES
                ),
            ),
            # Documented no-op -- see the DEFAULT_NIGHTLY_AUDIT_PARALLEL comment
            # above. No caller reads this key; kept only so the env var is
            # discoverable and doesn't silently do nothing with no trace.
            "parallel": max(
                1, _int_env("JC_NIGHTLY_AUDIT_PARALLEL", DEFAULT_NIGHTLY_AUDIT_PARALLEL)
            ),
            "coverage_alarm_threshold": max(
                0.0,
                min(
                    1.0,
                    _float_env(
                        "JC_NIGHTLY_AUDIT_COVERAGE_ALARM_THRESHOLD",
                        DEFAULT_NIGHTLY_AUDIT_COVERAGE_ALARM_THRESHOLD,
                    ),
                ),
            ),
            "failed_batch_fraction_alarm_threshold": max(
                0.0,
                min(
                    1.0,
                    _float_env(
                        "JC_NIGHTLY_AUDIT_FAILED_BATCH_FRACTION_ALARM_THRESHOLD",
                        DEFAULT_NIGHTLY_AUDIT_FAILED_BATCH_FRACTION_ALARM_THRESHOLD,
                    ),
                ),
            ),
            "min_sample_size_for_rate": max(
                1,
                _int_env(
                    "JC_NIGHTLY_MIN_SAMPLE_SIZE_FOR_RATE", DEFAULT_NIGHTLY_MIN_SAMPLE_SIZE_FOR_RATE
                ),
            ),
            "disagreement_alarm_rate": max(
                0.0,
                _float_env(
                    "JC_NIGHTLY_DISAGREEMENT_ALARM_RATE", DEFAULT_NIGHTLY_DISAGREEMENT_ALARM_RATE
                ),
            ),
            "disagreement_alarm_min_sample": max(
                0,
                _int_env(
                    "JC_NIGHTLY_DISAGREEMENT_ALARM_MIN_SAMPLE",
                    DEFAULT_NIGHTLY_DISAGREEMENT_ALARM_MIN_SAMPLE,
                ),
            ),
            "disagreement_alarm_absolute_ceiling": max(
                0.0,
                _float_env(
                    "JC_NIGHTLY_DISAGREEMENT_ALARM_ABSOLUTE_CEILING",
                    DEFAULT_NIGHTLY_DISAGREEMENT_ALARM_ABSOLUTE_CEILING,
                ),
            ),
            "disagreement_baseline_window_nights": max(
                1,
                _int_env(
                    "JC_NIGHTLY_DISAGREEMENT_BASELINE_WINDOW_NIGHTS",
                    DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_WINDOW_NIGHTS,
                ),
            ),
            "disagreement_baseline_min_nights": max(
                1,
                _int_env(
                    "JC_NIGHTLY_DISAGREEMENT_BASELINE_MIN_NIGHTS",
                    DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_MIN_NIGHTS,
                ),
            ),
            "disagreement_baseline_tolerance": max(
                0.0,
                _float_env(
                    "JC_NIGHTLY_DISAGREEMENT_BASELINE_TOLERANCE",
                    DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_TOLERANCE,
                ),
            ),
            "disagreement_baseline_absolute_floor": max(
                0.0,
                _float_env(
                    "JC_NIGHTLY_DISAGREEMENT_BASELINE_ABSOLUTE_FLOOR",
                    DEFAULT_NIGHTLY_DISAGREEMENT_BASELINE_ABSOLUTE_FLOOR,
                ),
            ),
        },
        "review": {
            # No default: an owner-identity-shaped repo slug must never be a
            # hardcoded literal in a public repo (private's
            # DEFAULT_NIGHTLY_REVIEW_REPO="Senkichi/job-cannon" has no port
            # target for exactly this reason). Required only when review/
            # issue-filing actually runs -- callers read this lazily, not at
            # import time, so an unset value never breaks a disabled deployment
            # or the boundary/import-all tests.
            "repo": (os.environ.get("JC_NIGHTLY_ISSUE_REPO") or "").strip() or None,
        },
    }


def require_issue_repo() -> str:
    """The configured issue-filing repo, or raise if unset.

    Split from ``nightly_monitor_config()["review"]["repo"]`` so a caller
    that needs to fail loudly (morning_driver.py, once review/issue-filing
    is enabled) gets a clear message instead of a downstream KeyError/None
    silently disabling filing.
    """
    repo = nightly_monitor_config()["review"]["repo"]
    if not repo:
        raise RuntimeError(
            "JC_NIGHTLY_ISSUE_REPO is not set -- required to file nightly review issues"
        )
    return repo

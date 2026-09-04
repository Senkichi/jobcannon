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

Only the subset of private's ``nightly_monitor`` block that this unit's
sampler/signatures/baselines/checkpoint modules actually consume is ported here.
``audit`` (score_threshold, lookback_days, max_jobs_per_night, ...),
``review``, and ``bash_rats`` sub-blocks belong to the morning audit/review
stage, a later ledger unit, and are intentionally
absent -- adding them now would be config for a caller that does not exist
yet on this branch.
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
    }

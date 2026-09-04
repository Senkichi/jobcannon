"""PORTED from job_finder/web/nightly_monitor/_baselines.py
@ e1f47695b07f928e6c91cc64767c97a99645d68f (private job-cannon).
Ledger L-0471.

Duration expectation bands derived from run history.

No hardcoded per-job expectations: per job name, a p10-p90 band over the
most recent K completed/degraded runs. The minimum history, relative
tolerance, and absolute floor are config-driven so callers decide how much
signal is required.

This is a fidelity anchor for this port: baseline percentile derivation
must diff clean against the private original. duration_band / out_of_band /
_percentile are pure functions over a caller-supplied ``events: list[dict]``
-- they were already source-agnostic in the private original (the private
caller built that list from run_events.jsonl; the hosted caller in
jobcannon.host.nightly.sampler builds the same shape from
scan_health_log/procrastinate_jobs rows) -- so the only change below is the
import line.
"""

from __future__ import annotations

from typing import Literal

# PORT-SEAM: job_finder.config's three DEFAULT_NIGHTLY_* constants -> the
# hosted env-var-backed equivalents in jobcannon.host.nightly.config (no
# config.yaml on this host; see that module's docstring).
from jobcannon.host.nightly.config import (
    DEFAULT_NIGHTLY_BASELINE_MIN_HISTORY,
    DEFAULT_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S,
    DEFAULT_NIGHTLY_OUT_OF_BAND_TOLERANCE,
)

_TERMINAL_OK = ("completed", "degraded")


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile over a pre-sorted non-empty list."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def duration_band(
    job_name: str,
    events: list[dict],
    *,
    window_runs: int,
    min_history: int | None = None,
) -> dict:
    """p10-p90 duration band for *job_name* over its last *window_runs* good runs."""
    if min_history is None:
        min_history = DEFAULT_NIGHTLY_BASELINE_MIN_HISTORY
    min_history = max(1, int(min_history))

    durations = [
        float(e["duration_s"])
        for e in events
        if e.get("event") == "run_end"
        and e.get("job") == job_name
        and e.get("disposition") in _TERMINAL_OK
        and isinstance(e.get("duration_s"), (int, float))
    ]
    # Clamp to >=1: durations[-0:] is the WHOLE list (unbounded history), not
    # an empty window. A 0/negative window collapses to the last run -> then
    # insufficient_history, which is the safe read.
    recent = durations[-max(1, int(window_runs)) :]
    if len(recent) < min_history:
        return {"status": "insufficient_history", "n": len(recent)}
    ordered = sorted(recent)
    return {
        "status": "ok",
        "n": len(recent),
        "p10": _percentile(ordered, 0.10),
        "p90": _percentile(ordered, 0.90),
    }


def out_of_band(
    duration_s: float | None,
    band: dict,
    *,
    tolerance: float | None = None,
    absolute_floor_s: float | None = None,
) -> Literal["fast", "slow"] | None:
    """Return the direction of an out-of-band duration, or None if in band.

    A run is flagged only when it deviates by more than the relative tolerance
    *and* by more than the absolute floor (whichever threshold is larger), so
    marginal runs and sub-few-second noise on short jobs are ignored.
    """
    if tolerance is None:
        tolerance = DEFAULT_NIGHTLY_OUT_OF_BAND_TOLERANCE
    if absolute_floor_s is None:
        absolute_floor_s = DEFAULT_NIGHTLY_OUT_OF_BAND_ABSOLUTE_FLOOR_S

    if band.get("status") != "ok" or not isinstance(duration_s, (int, float)):
        return None

    p10 = band["p10"]
    p90 = band["p90"]
    lower = p10 - max(tolerance * p10, absolute_floor_s)
    upper = p90 + max(tolerance * p90, absolute_floor_s)

    if duration_s < lower:
        return "fast"
    if duration_s > upper:
        return "slow"
    return None

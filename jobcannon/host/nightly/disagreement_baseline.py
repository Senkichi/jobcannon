"""PORTED from job_finder/web/nightly_monitor/_disagreement_baseline.py
@ e1f47695b07f928e6c91cc64767c97a99645d68f (private job-cannon).
Ledger L-0471.

Rolling baseline for the nightly disagreement-rate alarm (D13, #1619).

The fixed 25% threshold fired on essentially every real night (08-17
n=29->59%, 08-18 n=31->45%) and even fired on a 2-of-4 fluke (08-19 n=4->50%)
-- a non-signal training the owner to ignore the alert. This module keeps a
bounded per-night history of qualifying rates (nights with enough audited
jobs to be a stable estimate) and lets the caller ask two separate questions:

1. Is *this* night's sample big enough to trust at all? (``record_rate``
   only appends a night that clears ``min_sample`` -- the same floor the
   caller uses to decide whether to consider firing.)
2. Is this night's rate unusual relative to *recent* nights, not a fixed
   constant? (``rate_band`` + ``is_anomalous``, mirroring the p10/p90
   percentile-band + relative-tolerance + absolute-floor pattern in
   ``jobcannon.host.nightly.baselines`` for sampler duration bands -- kept as
   a separate, smaller module here because that one is scoped to per-tick
   sampler runs, an unrelated cadence.)

This is a fidelity anchor for this port (design note Sec7): the disagreement-
rate math must diff clean against the private original. Pure functions, no
I/O -- the only change from the private original is this header; the caller
(jobcannon.host.nightly.report) owns reading/writing state via
jobcannon.host.nightly.state instead of a local state.json.
"""

from __future__ import annotations

# Independent of the configured window: a corrupted/tampered state row
# should never be able to make this list grow unbounded.
_MAX_HISTORY_ENTRIES = 200


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile over a pre-sorted non-empty list."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def record_rate(
    history: list | None,
    rate: float | None,
    audited: int,
    *,
    min_sample: int,
    window_runs: int,
) -> list[float]:
    """Return an updated history with *rate* appended iff the sample qualifies.

    A night with fewer than ``min_sample`` audited jobs is dropped silently
    (same reasoning as the alarm's own min-sample gate: a tiny-n rate is pure
    noise and must not distort what "normal" looks like). The returned list
    is capped to the last ``window_runs`` qualifying nights.
    """
    updated = [float(v) for v in (history or []) if isinstance(v, (int, float))]
    if isinstance(rate, (int, float)) and audited >= max(0, int(min_sample)):
        updated.append(float(rate))
    updated = updated[-max(1, int(window_runs)) :]
    return updated[-_MAX_HISTORY_ENTRIES:]


def rate_band(history: list | None, *, min_history: int) -> dict:
    """p90 of the qualifying-night history, or insufficient_history."""
    vals = [float(v) for v in (history or []) if isinstance(v, (int, float))]
    if len(vals) < max(1, int(min_history)):
        return {"status": "insufficient_history", "n": len(vals)}
    ordered = sorted(vals)
    return {"status": "ok", "n": len(vals), "p90": _percentile(ordered, 0.90)}


def is_anomalous(rate: float, band: dict, *, tolerance: float, absolute_floor: float) -> bool:
    """True when *rate* clears the baseline p90 by more than tolerance/floor.

    Without enough baseline history to judge "unusual" (``insufficient_history``),
    this returns False -- the conservative read, matching ``out_of_band`` in
    ``jobcannon.host.nightly.baselines``: no basis for comparison means no
    flag, not a guess.
    """
    if band.get("status") != "ok":
        return False
    p90 = band["p90"]
    upper = p90 + max(float(tolerance) * p90, float(absolute_floor))
    return rate > upper

# PORTED from job_finder/web/inflight_guard.py @ 406907bfecc5378e4bdcf1b931db151da50f640b (private job-cannon). Ledger L-0187.
"""Short-TTL in-process guard against duplicate concurrent LLM-cost actions.

hx-disabled-elt (client-side) is bypassable — a fast double-click, a second
browser tab, or a manual POST all reach the route before the client sees the
disabled state. Mirrors the cache-check pattern companies.research already
uses (companies.py:953-965) for the same problem, generalized so rescore/
prepare/add-job don't each reinvent it.

Single-process, in-memory ONLY (module-level singleton + threading.Lock, same
shape as live_events.LiveEventBus) -- this is a same-process duplicate-click
guard, not a durable session record. It MUST NOT be backed by a DB table or
migration: unlike batch_score_sessions (multi-minute background work that must
survive page reload, see PollingSessionConfig/render_polling_status), the
actions this guards are single synchronous requests with no in-progress state
to resume -- there is nothing to persist past the request's own lifetime.

NOTE: The background pattern (e.g., prepare_application's resume_prepare pipeline)
supersedes the synchronous-TTL rationale for those routes. The guard now only
prevents duplicate *session creation*; the inflight_guard is released only when
the background thread reaches a terminal state (done/error), not when the POST
returns.
"""

import threading
import time

_TTL_SECONDS = 240  # covers the craft chain (summary competition + bullet craft + prose adjudication + LLM-judge review) with headroom; the real fix is the background-job pattern (F1) — this is belt-and-suspenders only.

_lock = threading.Lock()
_in_flight: dict[tuple[str, str], float] = {}  # (key, action) -> started_at monotonic


def try_acquire(key: str, action: str) -> bool:
    """Claim (key, action). Returns False if already claimed and not expired."""
    now = time.monotonic()
    with _lock:
        started = _in_flight.get((key, action))
        if started is not None and (now - started) < _TTL_SECONDS:
            return False
        _in_flight[(key, action)] = now
        return True


def release(key: str, action: str) -> None:
    """Release (key, action). Safe to call even if never acquired."""
    with _lock:
        _in_flight.pop((key, action), None)

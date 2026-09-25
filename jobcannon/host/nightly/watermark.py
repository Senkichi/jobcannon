"""DB watermark-cursor reads for the nightly monitor (issue #421).

NOT a port -- no private-repo equivalent exists; private tailed app.log /
run_events.jsonl by byte offset, and the hosted replacement (see
sampler.py's PORT-SEAM note) is a bigserial-id cursor over
scan_health_log.id / procrastinate_jobs.id instead.

Every polling loop in this package needs the same read shape: "rows whose
id is greater than the cursor saved last tick, bounded, plus the new
high-water id." sampler.py's _tick ran that shape twice inline
(scan_health_watermark_id and procrastinate_watermark_id); the
error_budget/deadman ports reimplement it next unless it is factored out,
which is why it lives here next to state.py -- the package's
nightly_monitor_state reader/writer that persists the cursors.

``fetch_since_watermark`` is deliberately only the fetch+high-water half:
WHICH watermark a caller persists stays caller policy (sampler._tick
advances the scan_health cursor to every row read but the procrastinate
cursor only through rows actually checkpointed on a drained tick).
"""

from __future__ import annotations

from typing import Any

from jobcannon.db.pool import unwrap_raw


def fetch_since_watermark(
    conn: Any,
    query: str,
    *,
    since_id: int,
    limit: int,
    params: dict | None = None,
) -> tuple[list, int]:
    """Rows newer than ``since_id``, plus the new high-water id.

    ``query`` contract: a parameterised SELECT that filters ``id >
    %(since_id)s``, orders by id ASCENDING, and projects an ``id`` column;
    ``%(limit)s`` bounds the fetch. Any other named placeholders in the
    query are filled from ``params``.

    Returns ``(rows, new_watermark)`` where ``new_watermark`` is the last
    row's ``id`` -- the maximum id read, since the contract orders
    ascending -- or ``since_id`` unchanged when the fetch came back empty,
    so a persisted cursor never rewinds and never skips unprocessed rows.
    """
    rows = (
        unwrap_raw(conn)
        .execute(query, {"since_id": since_id, "limit": limit, **(params or {})})
        .fetchall()
    )
    return list(rows), (rows[-1]["id"] if rows else since_id)

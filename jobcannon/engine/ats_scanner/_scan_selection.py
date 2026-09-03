"""PORTED from job_finder/db/_scan_selection.py @ ec9b1404f684a8f20ad1ec2aa81c3a2f20fc0394
(private job-cannon). Ledger L-0078.
# PORT-SEAM: header inserted above; the private module's own title and body
# below follow verbatim except where another PORT-SEAM note says otherwise.

Selection ledger writer for ``scan_selection_log`` (WI-04, D8, F-3).

The ATS Phase-A scanner writes one row per company per run recording why that
company was ``selected`` or which ``skipped_*`` reason excluded it. This module
holds only the low-level, schema-aware primitives:

* :func:`record_selection_batch` — bulk-insert already-decided rows (used for the
  ``selected`` set, whose rank/order the caller computes from the selector's own
  result set).
* :func:`prune_selection_log` — retention trim.

The precedence-ordered ``INSERT…SELECT`` partitioning of the *skipped* classes
lives in ``ats_scanner/_run.py`` (next to the clause helpers it negates), not
here — this module deliberately knows nothing about the scanner's selection
predicates, so it stays a leaf with no import back-edge into the engine
package.  # PORT-SEAM: "web package" (private) -> "engine package" (this
# repo's callers live in jobcannon.engine, not a web package).
#
# PORT-SEAM: no call site wires this module yet -- wiring the
# precedence-ordered partitioning above to call record_selection_batch is
# out of scope for this ledger row (carried_files: []), mirroring
# _scan_log.py's L-0077 port, which is in the same unwired state for the
# same reason (both writers' intended callers live in ats_scanner/_run.py,
# owned by ledger row L-0450).

Pure writes: neither function commits; the caller owns the transaction boundary
(the scanner batches the whole selection partition into one commit).
"""

from __future__ import annotations

import sqlite3

from jobcannon.engine.json_utils import utc_now_iso


def record_selection_batch(
    conn: sqlite3.Connection,
    run_id: str,
    # PORT-SEAM: private also took a `job_id` positional param (the
    # APScheduler job identifier, e.g. "ats_scan") here, written to a
    # `job_id` column. jobcannon/db/migrations/m0013_scan_selection_log.py
    # drops that column from the hosted table -- "this host's
    # jobcannon.engine.ats_scanner._run has no per-job job_id concept to
    # populate it with" (that migration's docstring) -- so the param is
    # dropped too rather than carried with nowhere to write.
    rows: list[tuple[int, str, int | None, int | None]],
) -> int:
    """Insert one ``scan_selection_log`` row per element of *rows*.

    Each element is ``(company_id, decision, tier, rank)``. ``created_at`` is
    stamped once per call with :func:`utc_now_iso` (naive-UTC-ISO storage
    convention). Returns the number of rows inserted.

    Plain ``INSERT`` (not ``INSERT OR IGNORE``): the ``UNIQUE(run_id,
    company_id)`` constraint is expected to hold by construction — the caller's
    precedence-ordered partition never offers the same company twice — so a
    collision here is a real bug that should surface, not be silently dropped.
    """
    if not rows:
        return 0

    created_at = utc_now_iso()
    conn.executemany(
        """INSERT INTO scan_selection_log
               (run_id, company_id, decision, tier, rank, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",  # PORT-SEAM: job_id column dropped, see signature above
        [
            (
                run_id,
                company_id,
                decision,
                tier,
                rank,
                created_at,
            )  # PORT-SEAM: job_id dropped, see signature above
            for (company_id, decision, tier, rank) in rows
        ],
    )
    return len(rows)


def prune_selection_log(conn: sqlite3.Connection, keep_days: int) -> int:
    """Delete ``scan_selection_log`` rows older than *keep_days* days.

    Uses SQLite's ``datetime('now', '-<n> days')`` (UTC, matching the stored
    naive-UTC-ISO ``created_at``). Returns the number of rows deleted. Pure
    delete; does not commit.
    # PORT-SEAM: private's docstring stops at "does not commit." Adding here:
    # jobcannon/db/compat.py's date-function rewrite translates this exact
    # `datetime('now', '-<n> days')` shape to Postgres's
    # `now() - make_interval(days => ?)` for engine callers, so it needs no
    # further seam adaptation on the hosted path.
    """
    cursor = conn.execute(
        "DELETE FROM scan_selection_log WHERE created_at < datetime('now', '-' || ? || ' days')",
        (int(keep_days),),
    )
    return int(cursor.rowcount)

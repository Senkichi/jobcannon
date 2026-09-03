# PORTED from tests/test_scan_selection.py @ ec9b1404f684a8f20ad1ec2aa81c3a2f20fc0394 (private job-cannon). Ledger L-0509.
"""Unit tests for the low-level selection-ledger writer (WI-04, D8).

Covers ``jobcannon.engine.ats_scanner._scan_selection``:
# PORT-SEAM: renamed from job_finder.db._scan_selection (module moved).
- :func:`record_selection_batch` inserts one row per tuple with a shared
  ``created_at`` and the UNIQUE(run_id, company_id) contract.
- :func:`prune_selection_log` deletes only rows older than the retention window
  (positive control: a fresh row survives the same prune that removes a stale one).

# PORT-SEAM: overlaps tests/engine/test_scan_selection.py, which already
# covers this module against the same bare-sqlite3 convention (that file's
# own scope note explains why: no compat.py translation layer wires this
# module to Postgres yet). Carried anyway per the literal same-relative-path
# carry rule (lands at tests/, not tests/engine/) -- no re-adjudication
# authority over the ledger's PORT verdict; flagging the redundancy here
# rather than silently dropping it.
"""

from __future__ import annotations

import sqlite3

# PORT-SEAM: job_finder.db -> the module's actual location; this module is a
# leaf, not re-exported from jobcannon/db/__init__ (see its own docstring).
from jobcannon.engine.ats_scanner._scan_selection import prune_selection_log, record_selection_batch


def _ledger_conn() -> sqlite3.Connection:
    """An in-memory DB with just the scan_selection_log table (schema mirrors the migration)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE scan_selection_log ("
        "id INTEGER PRIMARY KEY, "
        "run_id TEXT NOT NULL, "
        # PORT-SEAM: job_id column dropped here -- see _scan_selection.py's
        # own PORT-SEAM note on record_selection_batch's signature (this
        # host's ats_scanner._run has no per-job job_id concept).
        "company_id INTEGER NOT NULL, "
        "decision TEXT NOT NULL, "
        "tier INTEGER, "
        "rank INTEGER, "
        "created_at TEXT NOT NULL, "
        "UNIQUE(run_id, company_id))"
    )
    return conn


def test_record_selection_batch_inserts_rows():
    conn = _ledger_conn()
    n = record_selection_batch(
        conn,
        "run-1",
        # PORT-SEAM: "ats_scan" job_id positional arg dropped, see module note.
        [(10, "selected", None, 0), (11, "selected", None, 1)],
    )
    assert n == 2
    rows = conn.execute(
        "SELECT company_id, decision, tier, rank, created_at"  # PORT-SEAM: job_id column dropped from SELECT list
        " FROM scan_selection_log ORDER BY rank"
    ).fetchall()
    assert [r["company_id"] for r in rows] == [10, 11]
    # PORT-SEAM: assert all(r["job_id"] == "ats_scan" for r in rows) dropped -- no column.
    assert all(r["decision"] == "selected" for r in rows)
    assert rows[0]["tier"] is None
    # One call stamps one shared created_at.
    assert rows[0]["created_at"] == rows[1]["created_at"]


def test_record_selection_batch_empty_is_noop():
    conn = _ledger_conn()
    assert (
        record_selection_batch(conn, "run-1", []) == 0
    )  # PORT-SEAM: "ats_scan" job_id arg dropped
    assert conn.execute("SELECT count(*) FROM scan_selection_log").fetchone()[0] == 0


def test_prune_keeps_window():
    """A row inside the retention window survives; an older one is deleted."""
    conn = _ledger_conn()
    # Stale row: 40 days old (outside a 30-day window).
    conn.execute(
        "INSERT INTO scan_selection_log"
        # PORT-SEAM: job_id column dropped from INSERT column list/values below.
        " (run_id, company_id, decision, tier, rank, created_at)"
        " VALUES ('old', 1, 'selected', NULL, 0, datetime('now','-40 days'))"
    )
    # Fresh row: 1 day old (positive control — must survive).
    conn.execute(
        "INSERT INTO scan_selection_log"
        # PORT-SEAM: job_id column dropped from INSERT column list/values below.
        " (run_id, company_id, decision, tier, rank, created_at)"
        " VALUES ('new', 2, 'selected', NULL, 0, datetime('now','-1 days'))"
    )
    conn.commit()

    deleted = prune_selection_log(conn, keep_days=30)
    assert deleted == 1
    survivors = conn.execute("SELECT run_id FROM scan_selection_log ORDER BY run_id").fetchall()
    assert [r["run_id"] for r in survivors] == ["new"]

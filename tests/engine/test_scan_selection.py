"""Unit tests for jobcannon.engine.ats_scanner._scan_selection (ledger L-0078).

Scope note: these tests exercise the module's own functions in isolation
against a minimal bare-sqlite3 schema, matching the tests/engine/ convention
(no compat.py translation layer -- see tests/engine/test_scan_log.py's own
scope note). They do NOT cover wiring the precedence-ordered skipped-class
partitioning in ats_scanner/_run.py to call record_selection_batch -- that
wiring is out of scope here (carried_files: [] for L-0078) and belongs to
ledger row L-0450.
"""

from __future__ import annotations

import sqlite3

import pytest

from jobcannon.engine.ats_scanner._scan_selection import (
    prune_selection_log,
    record_selection_batch,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE scan_selection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            company_id INTEGER NOT NULL,
            decision TEXT NOT NULL,
            tier INTEGER,
            rank INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, company_id)
        )"""
    )
    c.commit()
    return c


def test_record_selection_batch_inserts_one_row_per_element(conn):
    inserted = record_selection_batch(
        conn,
        "run1",
        [
            (1, "selected", 1, 1),
            (2, "skipped_dormant", None, None),
        ],
    )
    assert inserted == 2
    rows = conn.execute(
        "SELECT company_id, decision, tier, rank FROM scan_selection_log ORDER BY company_id"
    ).fetchall()
    assert rows == [
        (1, "selected", 1, 1),
        (2, "skipped_dormant", None, None),
    ]


def test_record_selection_batch_empty_list_is_noop(conn):
    assert record_selection_batch(conn, "run1", []) == 0
    assert conn.execute("SELECT COUNT(*) FROM scan_selection_log").fetchone()[0] == 0


def test_record_selection_batch_stamps_shared_created_at(conn):
    record_selection_batch(conn, "run1", [(1, "selected", 1, 1), (2, "selected", 1, 2)])
    stamps = [r[0] for r in conn.execute("SELECT created_at FROM scan_selection_log").fetchall()]
    assert len(set(stamps)) == 1


def test_record_selection_batch_duplicate_company_in_same_run_raises(conn):
    record_selection_batch(conn, "run1", [(1, "selected", 1, 1)])
    with pytest.raises(sqlite3.IntegrityError):
        record_selection_batch(conn, "run1", [(1, "skipped_dormant", None, None)])


def test_prune_selection_log_deletes_only_stale_rows(conn):
    record_selection_batch(conn, "run1", [(1, "selected", 1, 1), (2, "selected", 1, 2)])
    conn.execute(
        "UPDATE scan_selection_log SET created_at = datetime('now', '-40 days') "
        "WHERE company_id = 1"
    )
    conn.execute("UPDATE scan_selection_log SET created_at = datetime('now') WHERE company_id = 2")
    deleted = prune_selection_log(conn, keep_days=30)
    assert deleted == 1
    remaining = [r[0] for r in conn.execute("SELECT company_id FROM scan_selection_log").fetchall()]
    assert remaining == [2]

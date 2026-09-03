"""Unit tests for jobcannon.engine.ats_scanner._scan_log (ledger L-0077).

Scope note: these tests exercise the module's own functions in isolation
against a minimal bare-sqlite3 schema, matching the tests/engine/ convention
(see tests/engine/helpers/ats_scan_services.py, tests/engine/test_dormancy_
cadence.py) of running engine SQL directly with no compat.py translation
layer. They do NOT cover wiring into ats_scanner/_run.py, _run_html.py, or
_run_playwright.py's call sites -- that wiring, and any single-writer
grep-guard test enforcing it, belongs to those files' own ledger rows
(L-0450, L-0019, L-0020; see _scan_log.py's module docstring) and is out of
scope here (carried_files: [] for L-0077).
"""

from __future__ import annotations

import sqlite3

import pytest

from jobcannon.engine.ats_scanner._scan_log import (
    _scan_log_columns,
    bump_empty_scan_counter,
    prune_title_outcomes,
    record_scan_outcome,
    record_title_outcomes,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE companies (id INTEGER PRIMARY KEY, consecutive_empty_scans INTEGER DEFAULT 0)"
    )
    c.execute(
        """CREATE TABLE company_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            scanned_at TEXT,
            jobs_found INTEGER,
            skipped_title_filter INTEGER,
            error TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE scan_title_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            company_id INTEGER,
            title TEXT,
            disposition TEXT,
            seen_at TEXT
        )"""
    )
    c.execute("INSERT INTO companies (id, consecutive_empty_scans) VALUES (1, 0)")
    c.commit()
    return c


def test_scan_log_columns_reflects_live_sqlite_schema(conn):
    # Minimal fixture schema omits source/run_id/jobs_matched/jobs_new/
    # failure_reason -- the present-column intersection should reflect
    # exactly the six columns actually declared above.
    assert _scan_log_columns(conn) == {
        "id",
        "company_id",
        "scanned_at",
        "jobs_found",
        "skipped_title_filter",
        "error",
    }


def test_record_scan_outcome_writes_only_present_columns(conn):
    row_id = record_scan_outcome(
        conn,
        company_id=1,
        source="requests",  # not a live column on this fixture -- silently dropped
        jobs_found=3,
        run_id="abc",  # not a live column on this fixture -- silently dropped
        jobs_matched=2,  # not a live column on this fixture -- silently dropped
        error=None,
    )
    row = conn.execute(
        "SELECT id, company_id, jobs_found, error FROM company_scan_log WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row == (row_id, 1, 3, None)


def test_record_scan_outcome_null_omission_preserves_default(conn):
    # skipped_title_filter not passed (None) -- must stay unset, not written as NULL
    # over some other value; the omission rule is what makes this a no-op write.
    row_id = record_scan_outcome(conn, company_id=1, source="requests", jobs_found=0)
    columns = [d[0] for d in conn.execute("SELECT * FROM company_scan_log").description]
    row = dict(
        zip(
            columns,
            conn.execute("SELECT * FROM company_scan_log WHERE id = ?", (row_id,)).fetchone(),
        )
    )
    assert row["skipped_title_filter"] is None


def test_record_scan_outcome_defaults_scanned_at_when_omitted(conn):
    row_id = record_scan_outcome(conn, company_id=1, source="requests")
    scanned_at = conn.execute(
        "SELECT scanned_at FROM company_scan_log WHERE id = ?", (row_id,)
    ).fetchone()[0]
    assert scanned_at is not None


def test_bump_empty_scan_counter_increments_on_zero_matched(conn):
    bump_empty_scan_counter(conn, 1, 0)
    assert (
        conn.execute("SELECT consecutive_empty_scans FROM companies WHERE id = 1").fetchone()[0]
        == 1
    )


def test_bump_empty_scan_counter_resets_on_nonzero_matched(conn):
    bump_empty_scan_counter(conn, 1, 0)
    bump_empty_scan_counter(conn, 1, 5)
    assert (
        conn.execute("SELECT consecutive_empty_scans FROM companies WHERE id = 1").fetchone()[0]
        == 0
    )


def test_record_title_outcomes_inserts_one_row_per_pair(conn):
    inserted = record_title_outcomes(
        conn, "run1", 1, [("Engineer", "matched"), ("PM", "title_filtered")]
    )
    assert inserted == 2
    rows = conn.execute(
        "SELECT title, disposition FROM scan_title_outcomes ORDER BY title"
    ).fetchall()
    assert rows == [("Engineer", "matched"), ("PM", "title_filtered")]


def test_record_title_outcomes_empty_list_is_noop(conn):
    assert record_title_outcomes(conn, "run1", 1, []) == 0
    assert conn.execute("SELECT COUNT(*) FROM scan_title_outcomes").fetchone()[0] == 0


def test_prune_title_outcomes_deletes_only_stale_rows(conn):
    record_title_outcomes(conn, "run1", 1, [("Old", "matched"), ("New", "matched")])
    conn.execute(
        "UPDATE scan_title_outcomes SET seen_at = datetime('now', '-40 days') WHERE title = 'Old'"
    )
    conn.execute("UPDATE scan_title_outcomes SET seen_at = datetime('now') WHERE title = 'New'")
    deleted = prune_title_outcomes(conn, keep_days=30)
    assert deleted == 1
    remaining = [r[0] for r in conn.execute("SELECT title FROM scan_title_outcomes").fetchall()]
    assert remaining == ["New"]

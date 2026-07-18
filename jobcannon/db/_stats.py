"""corpus_stats — read-only corpus counts feeding the demo/feed empty-state
shells (1B Wave 3 PR 11, Step 4b). No writer lives here; this module only
reads `postings`/`companies`."""

from __future__ import annotations

from typing import Any


def corpus_stats(conn: Any) -> dict:
    """Read-only corpus counts for the demo/feed shells. String-key row
    access only (Reconciliation Preamble item 12)."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT (SELECT COUNT(*) FROM postings) AS postings, "
        "(SELECT COUNT(*) FROM companies) AS companies, "
        "(SELECT MAX(last_seen) FROM postings) AS freshest_last_seen"
    ).fetchone()
    return {
        "postings": row["postings"],
        "companies": row["companies"],
        "freshest_last_seen": row["freshest_last_seen"],
    }

"""PORTED from job_finder/db/_company_state.py
@ f20c5b927308f288888fd068a1d3e7af64b644be (private job-cannon). Ledger L-0040.

Append-only audit log for company tracked-field state transitions (WI-08).

Every change to a company's ATS-tracked fields is recorded as one row per
changed field in ``company_state_history``, tagged with the ``changed_by``
code path that made it. This answers questions like "was this company ever
demoted, and by which path?" that the point-in-time ``companies`` row cannot.

**Single-writer invariant.** ``record_state_change`` holds the ONLY
``INSERT INTO company_state_history`` statement in the codebase (grep-guarded
by ``tests/host/test_company_state_history.py::test_record_state_change_is_the_only_insert``).
Every other helper here routes through it.

**Tracked fields.** Six columns: the four ATS-identity/state fields
(``ats_platform``, ``ats_slug``, ``ats_probe_status``, ``miss_reason``) plus
the ``ats_scan_enabled`` / ``careers_scan_enabled`` scan-lane flags from the
WI-13 split (jobcannon/db/migrations/m0018_wi13_scan_lane_columns.py). The
legacy aggregate ``scan_enabled`` is deliberately EXCLUDED, matching
private's WI-13/D16 rationale: every instrumented writer that sets
``scan_enabled = false`` also sets ``ats_scan_enabled = false`` in the same
statement (m0018's backfill establishes that invariant at migration time),
so the scan-disable transition is captured by the split flag instead.

**Transaction discipline.** None of these helpers commit. They INSERT on the
caller's connection so the history rows land atomically inside the writer's
own transaction — a writer that rolls back also rolls back its history rows,
and a writer that commits flushes them together. Callers own the transaction
boundary (jobcannon/db/_companies.py's ``commit_unless_nested(raw)``, same as
every other write path in this package).

**Snapshot-diff strategy.** Most instrumented writers use COALESCE,
conditional ``CASE WHEN`` clauses, or dynamically-assembled ``SET`` lists, so
the post-write value is not knowable from the UPDATE parameters. Instrumented
sites therefore snapshot the tracked fields before and after the write and
diff them (``snapshot_tracked`` + ``record_state_diff``); the recorded
transition is always the real one, and paths that end up writing nothing
naturally record zero rows.
"""

from __future__ import annotations

from typing import Any

# Column order is retained from private for parity, though the read below is
# no longer positional (see snapshot_tracked's PORT-SEAM) -- string-keyed
# access works regardless of which row shape the caller's connection uses.
_TRACKED_FIELDS: tuple[str, ...] = (
    "ats_platform",
    "ats_slug",
    "ats_probe_status",
    "miss_reason",
    "ats_scan_enabled",
    "careers_scan_enabled",
)

# PORT-SEAM: ? -> %s
_SNAPSHOT_SQL = "SELECT " + ", ".join(_TRACKED_FIELDS) + " FROM companies WHERE id = %s"


def _as_text(value: Any) -> str | None:
    """Coerce a tracked value to TEXT storage form (NULL preserved)."""
    return None if value is None else str(value)


def record_state_change(
    conn: Any,
    company_id: int,
    field: str,
    old: Any,
    new: Any,
    changed_by: str,
) -> int:
    """Append one history row for a single tracked-field change.

    The SOLE ``INSERT INTO company_state_history`` in the codebase. No-ops
    (``old == new``) are skipped and return 0; a recorded change returns 1.
    Comparison is on the raw values (NULL-aware, type-correct); only storage
    is coerced to TEXT. Does not commit — the row lands in the caller's
    transaction.
    """
    if old == new:
        return 0
    conn.execute(
        # PORT-SEAM: ? -> %s; changed_at column omitted from the INSERT list
        # entirely -- m0019's `DEFAULT now()` fills it, replacing private's
        # Python-computed utc_now_iso() TEXT value (see m0019's docstring).
        """INSERT INTO company_state_history
              (company_id, field, old_value, new_value, changed_by)
           VALUES (%s, %s, %s, %s, %s)""",
        (company_id, field, _as_text(old), _as_text(new), changed_by),
    )
    return 1


def snapshot_tracked(conn: Any, company_id: int) -> dict[str, Any] | None:
    """Return the current tracked-field values for a company, or None if absent."""
    row = conn.execute(_SNAPSHOT_SQL, (company_id,)).fetchone()
    if row is None:
        return None
    # PORT-SEAM: string-key access replaces private's positional
    # `dict(zip(_TRACKED_FIELDS, tuple(row), strict=True))`. This host's row
    # shapes are HybridRow (pooled factory connections — supports both
    # row["col"] and row[0]) and dict_row (tests/host/conftest.py's db_conn
    # — string keys only, per jobcannon/db/_companies.py's "Row access
    # note"); string-keying is the one style both share.
    return {field: row[field] for field in _TRACKED_FIELDS}


def manual_scan_disable_predicate(company_id_sql: str) -> str:
    """SQL fragment that evaluates to TRUE when the company's most recent
    ``ats_scan_enabled`` transition in ``company_state_history`` was a manual
    owner toggle-off (``changed_by='companies.toggle'``, ``new_value='0'``).

    Reuses the WI-08 ``company_state_history`` ledger — no new column, no new
    table. Mirrors private's absorbing-state exit-lane predicate; no
    Postgres call site wires it yet on this host (the absorbing-resweep /
    reprobe lanes this guards are a separate, not-yet-landed ledger group) —
    it is ported as part of this module for a future caller to reuse rather
    than re-derive.

    "Most recent" is determined per company by ``changed_at`` with ``id`` as
    the tiebreaker (a single toggle writes one ``ats_scan_enabled`` row, so
    within a company there is no same-``changed_at`` collision for this
    field, but the tiebreaker keeps the ordering total for correctness).
    ``changed_at`` is a Postgres ``timestamptz`` column (m0019), so ``>``
    comparison is chronological natively — no dialect change from private's
    intent, only from private's ISO-8601-TEXT lexicographic-comparison
    implementation detail.

    Args:
        company_id_sql: The SQL expression identifying the company row in the
            enclosing query. MUST be a table-qualified column reference (e.g.
            ``companies.id``, ``c.id``) — a bare ``id`` is ambiguous inside
            the correlated subquery because ``company_state_history`` also
            has an ``id`` column.

    Returns:
        A self-contained ``EXISTS (...)`` SQL fragment. Callers embed it as
        ``AND NOT (<fragment>)`` to carve out owner-disabled rows.
    """
    return f"""EXISTS (
        SELECT 1 FROM company_state_history h1
        WHERE h1.company_id = {company_id_sql}
          AND h1.field = 'ats_scan_enabled'
          AND h1.changed_by = 'companies.toggle'
          AND h1.new_value = '0'
          AND NOT EXISTS (
              SELECT 1 FROM company_state_history h2
              WHERE h2.company_id = {company_id_sql}
                AND h2.field = 'ats_scan_enabled'
                AND (h2.changed_at > h1.changed_at
                     OR (h2.changed_at = h1.changed_at AND h2.id > h1.id))
          )
    )"""


def record_state_diff(
    conn: Any,
    company_id: int,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    changed_by: str,
) -> int:
    """Diff two tracked-field snapshots and append a row per changed field.

    Records nothing when either snapshot is None (no prior state to diff,
    e.g. a freshly-inserted company, or a row that vanished). Returns the
    number of history rows appended.
    """
    if before is None or after is None:
        return 0
    written = 0
    for field in _TRACKED_FIELDS:
        written += record_state_change(
            conn, company_id, field, before.get(field), after.get(field), changed_by
        )
    return written

"""PORTED from job_finder/db/_company_state.py
@ f20c5b927308f288888fd068a1d3e7af64b644be (private job-cannon). Ledger L-0040.
# PORT-SEAM: conn type hints sqlite3.Connection -> Any (host connections are
# psycopg, not sqlite3); ? placeholders -> %s throughout; import sqlite3 and
# job_finder.json_utils.utc_now_iso both dropped (see record_state_change's
# own PORT-SEAM -- m0022's changed_at DEFAULT now() replaces the Python-
# computed timestamp). Sole-writer test path is class-qualified
# (TestSoleWriter::test_record_state_change_is_the_only_insert) to match
# this repo's grouped test-class layout, not private's flat function.
Append-only audit log for company tracked-field state transitions (WI-08).

Every change to a company's ATS-tracked fields is recorded as one row per
changed field in ``company_state_history``, tagged with the ``changed_by`` code
path that made it. This answers questions like "was this company ever demoted,
and by which path?" that the point-in-time ``companies`` row cannot.

**Single-writer invariant.** ``record_state_change`` holds the ONLY
``INSERT INTO company_state_history`` statement in the codebase (grep-guarded by
# PORT-SEAM: test path class-qualified for this repo's grouped test-class
# layout (private guarded a flat function at the bare module path).
``tests/test_company_state_history.py::TestSoleWriter::test_record_state_change_is_the_only_insert``).
Every other helper here routes through
it.

# PORT-SEAM: private's #1869 issue ref -> the actual migration path (this
# host has no separate issue tracker entry for the split); private's
# WI-13/D16 "no production reads of the legacy column" CI guard
# (tests/test_scan_enabled_split.py) has no host counterpart -- not ported,
# no ledger row covers it. Most importantly: private's "every instrumented
# writer that sets scan_enabled = 0 sets ats_scan_enabled = 0 in the same
# statement" is NOT yet true on this host -- see
# jobcannon/db/migrations/m0021_wi13_scan_lane_columns.py's own "Stragglers:"
# section. m0021's backfill sets the split flags ONCE, at migration time;
# no writer here (jobcannon/db/_company_attribution.py,
# jobcannon/engine/ats_prober.py, jobcannon/engine/ats_scanner/_run.py) has
# been instrumented to co-write them yet, unlike private's per-writer
# discipline. This module still excludes ``scan_enabled`` from
# ``_TRACKED_FIELDS`` (reading it would defeat the split's whole point), but
# until that follow-up lands the split columns can drift stale relative to
# ``scan_enabled`` between writer-instrumentation PRs -- tracked as
# follow-up, not fixed inline here (out of this row's scope).
**Tracked fields.** Six columns: the four ATS-identity/state fields
(``ats_platform``, ``ats_slug``, ``ats_probe_status``, ``miss_reason``) plus the
``ats_scan_enabled`` / ``careers_scan_enabled`` scan-lane flags from the WI-13
# PORT-SEAM: private's #1869 -> the m0021 migration path (see block above
# for the "no signal is lost" claim's honest replacement).
split (jobcannon/db/migrations/m0021_wi13_scan_lane_columns.py). The legacy
aggregate ``scan_enabled`` is deliberately EXCLUDED from the tracked set,
matching private's WI-13/D16 rationale: snapshotting a tracked field means
SELECTing it, and the split flags exist precisely so callers stop reading the
merged bit.

**Transaction discipline.** None of these helpers commit. They INSERT on the
caller's connection so the history rows land atomically inside the writer's own
transaction — a writer that rolls back also rolls back its history rows, and a
writer that commits flushes them together. Callers own the transaction boundary
# PORT-SEAM: this host's boundary is jobcannon/db/_companies.py's
# commit_unless_nested(raw) (ledger L-0040's injection point), not private's
# bare conn.commit() call sites.
(``jobcannon/db/_companies.py``'s ``commit_unless_nested(raw)``, same as every
other write path in this package).

**Snapshot-diff strategy.** Most instrumented writers use COALESCE, conditional
``CASE WHEN`` clauses, or dynamically-assembled ``SET`` lists, so the post-write
value is not knowable from the UPDATE parameters. Instrumented sites therefore
snapshot the tracked fields before and after the write and diff them
(``snapshot_tracked`` + ``record_state_diff``); the recorded transition is always
the real one, and paths that end up writing nothing
# PORT-SEAM: private's parenthetical named its own swallowed-IntegrityError /
# rowcount!=1 guard shapes; this host's collision path
# (jobcannon/db/_companies.py's UniqueViolation handling) is a different
# shape with the same net effect, so the specific mechanism name is dropped
# rather than misdescribed.
naturally record zero rows.
"""

from __future__ import annotations

# PORT-SEAM: import sqlite3 dropped (host connections are psycopg's Any).
from typing import Any

# PORT-SEAM: private's comment cited a positional `zip(_TRACKED_FIELDS,
# row)` over the SELECT; snapshot_tracked below is now string-keyed instead
# (see its own PORT-SEAM), so column ORDER is retained only for readability
# parity with private, not because any reader still depends on it.
# Column order is authoritative: snapshots build dicts positionally via
# ``zip(_TRACKED_FIELDS, row)`` over a SELECT in this exact order, so it works
# regardless of the connection's row_factory (tuple or sqlite3.Row).
_TRACKED_FIELDS: tuple[str, ...] = (
    "ats_platform",
    "ats_slug",
    "ats_probe_status",
    "miss_reason",
    "ats_scan_enabled",
    "careers_scan_enabled",
)

# PORT-SEAM: ? -> %s.
_SNAPSHOT_SQL = "SELECT " + ", ".join(_TRACKED_FIELDS) + " FROM companies WHERE id = %s"


def _as_text(value: Any) -> str | None:
    """Coerce a tracked value to TEXT storage form (NULL preserved)."""
    return None if value is None else str(value)


def record_state_change(
    conn: Any,  # PORT-SEAM: sqlite3.Connection -> Any.
    company_id: int,
    field: str,
    old: Any,
    new: Any,
    changed_by: str,
) -> int:
    """Append one history row for a single tracked-field change.

    The SOLE ``INSERT INTO company_state_history`` in the codebase. No-ops
    (``old == new``) are skipped and return 0; a recorded change returns 1.
    Comparison is on the raw values (NULL-aware, type-correct); only storage is
    coerced to TEXT. Does not commit — the row lands in the caller's transaction.
    """
    if old == new:
        return 0
    conn.execute(
        # PORT-SEAM: ? -> %s; changed_at column omitted from the INSERT list
        # entirely -- m0022's `DEFAULT now()` fills it, replacing private's
        # Python-computed utc_now_iso() TEXT value (see m0022's docstring).
        """INSERT INTO company_state_history
              (company_id, field, old_value, new_value, changed_by)
           VALUES (%s, %s, %s, %s, %s)""",
        (
            company_id,
            field,
            _as_text(old),
            _as_text(new),
            changed_by,
        ),  # PORT-SEAM: changed_at arg dropped.
    )
    return 1


def snapshot_tracked(  # PORT-SEAM: sqlite3.Connection -> Any.
    conn: Any, company_id: int
) -> dict[str, Any] | None:
    """Return the current tracked-field values for a company, or None if absent.

    # PORT-SEAM: string-key access replaces private's positional
    # `dict(zip(_TRACKED_FIELDS, tuple(row), strict=True))`. This host's row
    # shapes are HybridRow (pooled factory connections — supports both
    # row["col"] and row[0]) and dict_row (tests/host/conftest.py's db_conn
    # — string keys only, per jobcannon/db/_companies.py's "Row access
    # note"); string-keying is the one style both share.
    """
    row = conn.execute(_SNAPSHOT_SQL, (company_id,)).fetchone()
    if row is None:
        return None
    return {field: row[field] for field in _TRACKED_FIELDS}  # PORT-SEAM: string-keyed.


def manual_scan_disable_predicate(company_id_sql: str) -> str:
    """SQL fragment that evaluates to TRUE when the company's most recent
    ``ats_scan_enabled`` transition in ``company_state_history`` was a manual
    owner toggle-off (``changed_by='companies.toggle'``, ``new_value='0'``).

    # PORT-SEAM: private named its two live call sites (run_absorbing_resweep,
    # the reprobe UNION cohort) by name; neither is ported to this host yet
    # (a separate, not-yet-landed ledger group), so this function lands
    # unwired -- for a future caller to reuse rather than re-derive, same
    # role as jobcannon/db/_assessment_writer.py's own unwired-on-landing
    # precedent (ledger L-0064). Private's #1875 issue ref is dropped
    # (private-only tracker).
    Used by the absorbing-state exit lanes (``run_absorbing_resweep`` and the
    reprobe UNION cohort) to EXCLUDE companies the owner deliberately disabled,
    so those lanes no longer flip ``ats_scan_enabled`` back on over the owner's
    # PORT-SEAM: #1875 dropped (private-only tracker; see note above).
    explicit off-toggle. Reuses the WI-08 ``company_state_history``
    ledger — no new column, no new table.

    "Most recent" is determined per company by ``changed_at`` with ``id`` as the
    tiebreaker (a single toggle writes one ``ats_scan_enabled`` row, so within a
    company there is no same-``changed_at`` collision for this field, but the
    tiebreaker keeps the ordering total for correctness).
    # PORT-SEAM: private's changed_at is ISO-8601 TEXT (lexicographic
    # comparison); this host's is a Postgres timestamptz (m0022), so `>`
    # comparison is chronological natively -- same intent, different
    # dialect.
    ``changed_at`` is a timestamptz column, so ``>`` comparison is chronological.

    Args:
        company_id_sql: The SQL expression identifying the company row in the
            enclosing query. MUST be a table-qualified column reference (e.g.
            ``companies.id``, ``c.id``) — a bare ``id`` is ambiguous inside the
            correlated subquery because ``company_state_history`` also has an
            ``id`` column,
            # PORT-SEAM: private's clause named SQLite's innermost-scope
            # resolution specifically; Postgres resolves an unqualified
            # column the same ambiguous way inside a correlated subquery, so
            # the guidance carries over unchanged, only the named engine is
            # dropped.
            and an ambiguous reference could silently bind to the wrong table.

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
    conn: Any,  # PORT-SEAM: sqlite3.Connection -> Any.
    company_id: int,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    changed_by: str,
) -> int:
    """Diff two tracked-field snapshots and append a row per changed field.

    Records nothing when either snapshot is None (no prior state to diff, e.g. a
    freshly-inserted company, or a row that vanished). Returns the number of
    history rows appended.
    """
    if before is None or after is None:
        return 0
    written = 0
    for field in _TRACKED_FIELDS:
        written += record_state_change(
            conn, company_id, field, before.get(field), after.get(field), changed_by
        )
    return written

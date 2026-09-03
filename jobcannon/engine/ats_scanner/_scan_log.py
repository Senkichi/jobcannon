"""PORTED from job_finder/db/_scan_log.py @ 6b17d78cb770cf53cb21b5e0b34c2cc7cd203136
(private job-cannon). Ledger L-0077.

Single writer for the ``company_scan_log`` audit table (WI-06, D9).

:func:`record_scan_outcome` is the sanctioned writer for ``company_scan_log``
and :func:`record_title_outcomes` / :func:`prune_title_outcomes` for
``scan_title_outcomes``. Wiring the existing raw
``INSERT INTO company_scan_log`` call sites in ``ats_scanner/_run.py``,
``_run_html.py``, and ``_run_playwright.py`` over to these functions is OUT
OF SCOPE for this module/ledger row (L-0077, ``carried_files: []``) — those
three files are owned by separate, already-adjudicated ledger rows
(L-0450, L-0019, L-0020) being ported concurrently in a sibling worktree
(``port/jobcannon-engine-ats_scanner``). This PR does not add a single-writer
grep-guard test for that reason: it would fail CI against the pre-existing
raw INSERTs in those not-yet-rewired files, which are someone else's diff.
Once that wiring PR lands, a grep-guard test (e.g.
``tests/engine/test_scan_log_single_writer.py``, modeled on
``tests/host/test_user_actions_single_writer.py``) belongs there or as a
fast-follow — concentrating the write in one place is what lets future
schema evolution (columns like ``jobs_new`` / ``failure_reason``) happen
without hunting down scattered INSERTs.

# PORT-SEAM: placed under jobcannon/engine/ats_scanner/ rather than the
# ledger seam's literal jobcannon/db/. jobcannon/db/compat.py's module
# docstring states engine SQL "uses qmark placeholders and reaches this
# layer verbatim through connection_factory connections" and "must NOT
# route through" the psycopg-%s host dialect jobcannon/db/*.py uses. The
# three intended callers (ats_scanner/_run.py, _run_html.py,
# _run_playwright.py, once rewired by their own ledger rows) are engine
# modules passing a sqlite3.Connection-shaped EngineCompatConnection, so
# this module stays in the same qmark dialect and the same package as its
# callers — a jobcannon/db/ placement would force a psycopg-%s rewrite that
# contradicts compat.py's documented dialect split.
# The private module's fourth caller, careers_crawler/_persistence.py, has
# no public counterpart yet (jobcannon/engine/careers_crawler/ carries no
# _persistence.py); record_scan_outcome is ready for it the moment that
# module ports.

Forward compatibility: :func:`record_scan_outcome` accepts ``run_id`` and
``jobs_new`` keyword arguments today even when a given deploy's schema
doesn't carry them yet. The writer inspects the live schema each call via
:func:`_scan_log_columns` and only emits columns that are actually present,
so callers can pass the future kwargs now and the values start persisting
the moment a migration adds the columns — no second edit pass over the call
sites. (``run_id`` already landed via m0013 — see that migration's docstring,
which names this function as the writer that starts persisting it with no
call-site change.)

# PORT-SEAM: wording generalized from private's "today even though those
# columns do NOT yet exist on the table" -- the hosted schema evolves via
# discrete migrations independent of this module's release, so "does not
# carry them yet" is the accurate framing here (private's dev DB is a
# single fixed schema snapshot).

The ``NULL``-omission rule: a candidate column is written only when its
value is not ``None``. This preserves every current column DEFAULT exactly —
error rows keep ``skipped_title_filter`` at its unset/NULL state (rather than
being regressed to NULL from some other value), and success rows keep
``jobs_matched``/``error``/``failure_reason`` at their DEFAULT NULL. Callers
that genuinely want a 0 (e.g. a future careers-crawler caller's
``jobs_matched = company_jobs_found`` where the count can legitimately be 0)
pass 0, which is not ``None`` and is therefore written.

# PORT-SEAM: private's ``skipped_title_filter`` carries ``DEFAULT 0``;
# jobcannon/db/migrations/m0001_initial_schema.py's ``company_scan_log``
# declares it with no DEFAULT (plain NULL-able integer) -- "unset/NULL
# state" is the accurate description of the hosted column, not private's
# DEFAULT 0.
"""

from __future__ import annotations

import sqlite3

from jobcannon.engine.json_utils import utc_now_iso

#: Columns this writer knows how to populate, in a stable canonical order. The
#: intersection of this list with the live table (per :func:`_scan_log_columns`)
#: determines what each INSERT actually writes, so the order here is the column
#: order of every emitted statement.
_CANDIDATE_COLUMNS: tuple[str, ...] = (
    "company_id",
    "scanned_at",
    "source",
    "run_id",
    "jobs_found",
    "jobs_matched",
    "jobs_new",
    "error",
    "failure_reason",
    "skipped_title_filter",
)


def _scan_log_columns(conn: sqlite3.Connection) -> set[str]:
    """Return the set of column names on ``company_scan_log`` for *conn*.

    Runs a live-schema lookup on every call — deliberately NOT cached by
    ``id(conn)``. Connection identity is reused across connections (pooling,
    per-worker standalone connections), and a schema migration can add a
    column mid-process, so a cache keyed on the connection object would serve
    a stale column set to a later caller on a reused id. The lookup is a
    cheap catalog read; running it per call is correct and negligible.

    # PORT-SEAM: private used SQLite's ``PRAGMA table_info(company_scan_log)``
    # unconditionally. Postgres has no PRAGMA statement, and
    # jobcannon/db/compat.py's engine_sql_to_host() does not rewrite PRAGMA —
    # it only translates qmark placeholders, one literal `jobs`->`postings`
    # table reference, and two SQLite datetime() shapes (see that module's
    # docstring). SQLite, in turn, has no `information_schema` catalog
    # (verified empirically: `SELECT ... FROM information_schema.columns`
    # against a real sqlite3 connection raises OperationalError), and
    # tests/engine/ exercises this exact module against bare sqlite3.Connection
    # fixtures with no compat.py translation layer (see
    # jobcannon/db/compat.py's own docstring re: tests/engine/test_dormancy_
    # cadence.py, and tests/engine/helpers/ats_scan_services.py's
    # create_scan_schema). So neither catalog query works unconditionally on
    # both backends -- this dispatches explicitly on connection type instead:
    # a real sqlite3.Connection (test fixtures, matching PORT-SEAM's own
    # dialect) uses PRAGMA; anything else (production's EngineCompatConnection
    # facade over psycopg, per jobcannon/db/pool.py) uses
    # information_schema.columns, going through EngineCompatConnection's
    # qmark translation for the ``?`` parameter.
    """
    if isinstance(conn, sqlite3.Connection):
        rows = conn.execute("PRAGMA table_info(company_scan_log)").fetchall()
        return {row[1] for row in rows}
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
        ("company_scan_log",),
    ).fetchall()
    return {row[0] for row in rows}


def record_scan_outcome(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    source: str,
    run_id: str | None = None,
    jobs_found: int | None = None,
    jobs_matched: int | None = None,
    jobs_new: int | None = None,
    error: str | None = None,
    failure_reason: str | None = None,
    skipped_title_filter: int | None = None,
    scanned_at: str | None = None,
) -> int:
    """Append one row to ``company_scan_log`` and return its id.

    # PORT-SEAM: private's docstring said "return its rowid" (SQLite
    # rowid, aliased by the INTEGER PRIMARY KEY ``id`` column there).
    # Postgres has no rowid concept; the value returned is the
    # ``company_scan_log.id`` column via ``RETURNING id`` (see the
    # function body below).

    Pure INSERT — this function does NOT commit; the caller owns the
    transaction boundary (some call sites batch this with a companies UPDATE
    and a ``consecutive_empty_scans`` bump in one commit).

    Only columns present on the live table AND carrying a non-``None`` value
    are written (see the module docstring's NULL-omission rule). ``company_id``
    and ``source`` are always attempted (# PORT-SEAM: private said
    "written"; ported to "attempted" since the hosted schema is
    versioned by migration and m0001 carries no ``source`` column yet,
    unlike private's one fixed dev schema); ``scanned_at`` defaults to
    :func:`utc_now_iso` when not supplied, matching the naive-UTC-ISO storage
    convention used elsewhere in this codebase's engine layer. Neither is
    guaranteed to land on every deploy — e.g. the hosted schema does not yet
    carry a ``source`` column, so it is silently dropped by the same
    present-column intersection that gates every other candidate.

    # PORT-SEAM: private said "always written" -- private's ``company_id``
    # and ``source`` are both fixed columns on its single dev schema, so
    # the attempt always succeeds there. The hosted schema is versioned by
    # migration (m0001 carries no ``source`` column yet), so "always
    # attempted" is the accurate framing for a schema that can lag.

    ``run_id`` and ``jobs_new`` are accepted for forward compatibility;
    on a schema that doesn't carry them yet, the values are silently dropped
    by the same present-column intersection.

    # PORT-SEAM: run_id/jobs_new phrasing mirrors the module-docstring seam
    # note above (deploy-relative "doesn't carry them yet" vs private's
    # "do not exist yet" against its one fixed dev schema).
    """
    if scanned_at is None:
        scanned_at = utc_now_iso()

    present = _scan_log_columns(conn)

    candidate_values: dict[str, object | None] = {
        "company_id": company_id,
        "scanned_at": scanned_at,
        "source": source,
        "run_id": run_id,
        "jobs_found": jobs_found,
        "jobs_matched": jobs_matched,
        "jobs_new": jobs_new,
        "error": error,
        "failure_reason": failure_reason,
        "skipped_title_filter": skipped_title_filter,
    }

    columns = [
        col for col in _CANDIDATE_COLUMNS if col in present and candidate_values[col] is not None
    ]
    values = [candidate_values[col] for col in columns]
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(columns)

    # PORT-SEAM: private read the new row id off sqlite3's `cursor.lastrowid`
    # (a DB-API extension psycopg3 does not implement). RETURNING id +
    # fetchone() is this codebase's established idiom for the same need —
    # see jobcannon/db/_companies.py's INSERT...RETURNING id sites.
    cursor = conn.execute(
        f"INSERT INTO company_scan_log ({column_list}) VALUES ({placeholders}) RETURNING id",  # PORT-SEAM: no cursor.lastrowid on psycopg3
        tuple(values),
    )
    row = cursor.fetchone()
    return int(
        row[0]
    )  # PORT-SEAM: psycopg3 has no cursor.lastrowid; RETURNING id + fetchone() replaces it


def bump_empty_scan_counter(conn: sqlite3.Connection, company_id: int, jobs_matched: int) -> None:
    """Maintain ``companies.consecutive_empty_scans`` for one scanned company.

    Resets the counter to 0 when *jobs_matched* > 0, otherwise increments it by
    1 — the same ``CASE WHEN ? = 0`` semantics the requests/serial ATS path
    (``ats_scanner/_run.py``) already folds into its post-scan companies
    UPDATE inline (both of its success branches maintain the counter inline
    already -- no gap there). This helper exists for the HTML-fallback and
    Playwright paths, which do NOT maintain the counter (mirroring private's
    #1823) -- ported here as an available function; wiring it into those two
    files' call sites is out of scope for this ledger row (see module
    docstring) and belongs to their own ledger rows (L-0019, L-0020). Pure
    UPDATE; does NOT commit.
    # PORT-SEAM: private's docstring said this helper exists so "the
    # Playwright path" specifically can bump the counter. Ported to name
    # both HTML-fallback and Playwright paths, and to note the requests
    # path's own success branches already maintain the counter inline (no
    # gap there) -- matching this port's actual scope note above.
    """
    conn.execute(
        """UPDATE companies
           SET consecutive_empty_scans = CASE WHEN ? = 0
                   THEN consecutive_empty_scans + 1 ELSE 0 END
           WHERE id = ?""",
        (jobs_matched, company_id),
    )


def record_title_outcomes(
    conn: sqlite3.Connection,
    run_id: str | None,
    company_id: int,
    rows: list[tuple[str, str]],
) -> int:
    """Insert one ``scan_title_outcomes`` row per element of *rows* (WI-09, D20).

    Each element is ``(title, disposition)`` where *disposition* is exactly one
    of ``title_filtered`` (in the raw board but excluded by the title filter,
    determined by dict identity — never by re-running the filter),
    ``dedup_existing`` (survived the filter but the upsert reported the job as
    not new), or ``matched`` (a new row was inserted). ``seen_at`` is stamped
    once per call with :func:`utc_now_iso` (naive-UTC-ISO storage convention).
    Returns the number of rows inserted.

    This is the single authority for writes to ``scan_title_outcomes`` — the
    per-title analogue of :func:`record_scan_outcome` for ``company_scan_log``.
    Pure ``INSERT``; does NOT commit — the caller owns the transaction boundary.

    No call site wires this yet: ``scan_title_outcomes`` landed schema-only
    via m0014 (see that migration's docstring — it explicitly defers the
    writer to this ledger row, L-0077). This PR ports the sole-writer
    function per the ledger's carry_range; wiring a caller is out of scope
    here and tracked separately.

    # PORT-SEAM: private's docstring named the ATS worker as the one caller
    # batching this with record_scan_outcome + the companies UPDATE. No
    # public caller exists yet (see paragraph above), so that batching
    # detail is dropped rather than describing a call site that isn't real.
    """
    if not rows:
        return 0

    seen_at = utc_now_iso()
    conn.executemany(
        """INSERT INTO scan_title_outcomes
               (run_id, company_id, title, disposition, seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        [(run_id, company_id, title, disposition, seen_at) for (title, disposition) in rows],
    )
    return len(rows)


def prune_title_outcomes(conn: sqlite3.Connection, keep_days: int) -> int:
    """Delete ``scan_title_outcomes`` rows older than *keep_days* days (WI-09).

    Uses the ``datetime('now', '-<n> days')`` shape (UTC, matching the stored
    naive-UTC-ISO ``seen_at``) — jobcannon/db/compat.py's date-function
    rewrite translates this exact shape to Postgres's
    ``now() - make_interval(days => ?)`` for engine callers, so this needs no
    seam adaptation. Returns the number of rows deleted. Pure delete; does not
    commit — mirrors ``prune_selection_log`` in ``_scan_selection.py``.

    # PORT-SEAM: "SQLite's datetime(...)" (private's wording) is replaced
    # with "the datetime(...) shape" plus an explicit cite of compat.py's
    # rewrite, since on the hosted path this literal SQL is translated to
    # Postgres before it runs -- it is no longer SQLite-specific in effect.
    """
    cursor = conn.execute(
        "DELETE FROM scan_title_outcomes WHERE seen_at < datetime('now', '-' || ? || ' days')",
        (int(keep_days),),
    )
    return int(cursor.rowcount)

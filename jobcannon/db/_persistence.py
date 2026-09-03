"""PORTED from job_finder/db/_persistence.py @ 546674e0cc6c35e3511e9f7cf66d1f0a65d880ed
(private job-cannon). Ledger L-0073.
# PORT-SEAM: this module ports only ``log_run`` and ``persist_job_expiry_state``
# out of private's five write paths. The other three -- ``persist_job_notes``,
# ``update_pipeline_status``, ``set_job_flag`` -- are NOT ported here; each
# would need to write into ``pipeline_status`` (or a new per-user column),
# and ``jobcannon/db/_user_actions.py`` already declares itself the sole
# writer of both ``watchlists`` and ``pipeline_status`` on this host. Adding
# a second writer to either table from this module would violate that
# stated single-writer invariant, and ``_user_actions.py`` sits outside this
# ledger group's carried_files scope, so it cannot be extended from this PR
# either. See verification.md's L-0073 entry for the full per-function
# reasoning (recorded there, not repeated per-function below).

``log_run`` writes a new ``runs`` table (jobcannon/db/migrations/m0016), a
global (not per-user) operational log -- ingestion runs are system-wide,
matching ``company_scan_log``'s precedent for a global append-only table.
# PORT-SEAM: private's module docstring described a shared sqlite3.Connection
# commit-themselves CLI-era pattern -- this port has no such shared-dialect
# note to make (this module is host/psycopg-dialect only).

``persist_job_expiry_state`` writes ``postings.expiry_status`` / ``last_seen``
/ ``is_stale`` (all already on the table, m0001) plus ``expiry_checked_at``
(new, m0016). Private's SQLite "database is locked" retry loop is dropped
entirely -- that string match is SQLite-dialect-specific (busy_timeout /
single-writer-lock contention has no psycopg/Postgres equivalent in that
shape); a transient write conflict here surfaces as a normal exception to
the caller, same as every other host writer in this package.
# PORT-SEAM: private's persist_job_assessment/update_pipeline_status
# re-export/back-compat paragraph is dropped -- neither symbol is ported
# by this module (persist_job_assessment lives in _assessment_writer.py,
# L-0064; update_pipeline_status is one of the three functions this port
# omits, see the top-of-file PORT-SEAM block above).

Both writers use ``pool.commit_unless_nested`` matching
``_jobs.py``/``_companies.py``/``_assessment_writer.py``'s transaction-
boundary convention.
# PORT-SEAM: private's job_finder.db.__init__ re-export paragraph dropped --
# no back-compat re-export shim exists or is needed on this host.
"""

from __future__ import annotations

import logging  # PORT-SEAM: import json dropped, no json.dumps text-column encoding on this host (Jsonb below)
from typing import Any  # PORT-SEAM: sqlite3 import dropped, no sqlite3 dialect on this host

from psycopg.types.json import (
    Jsonb,
)  # PORT-SEAM: replaces private's json.dumps text column (metadata is jsonb here)

from jobcannon.db.pool import (
    commit_unless_nested,
)  # PORT-SEAM: replaces private's _assessment_writer re-export block (persist_job_assessment/invalidate_job_score are not ported by this module)

_log = logging.getLogger(__name__)


def log_run(
    conn: Any,  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only)
    source: str,
    fetched: int,
    new: int,
    scored: int,
    metadata: dict | None = None,
) -> None:
    """Log a pipeline run for auditing.

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching ``_jobs.py`` / ``_companies.py``'s dispatch.
            (# PORT-SEAM: private said "Open sqlite3 connection".)
        source: Source label (e.g., "gmail", "serpapi").
        fetched: Number of jobs fetched.
        new: Number of new jobs inserted.
        scored: Number of jobs scored.
        metadata: Optional dict for funnel reconciliation identity (issue #587).
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    with raw.transaction():
        raw.execute(
            # PORT-SEAM: run_at avoids a reserved-word column name (timestamp), matching m0001's created_at/last_seen naming convention
            "INSERT INTO runs (run_at, source, jobs_fetched, jobs_new, jobs_scored, metadata) "
            "VALUES (now(), %s, %s, %s, %s, %s)",  # PORT-SEAM: now() server-side timestamp + %s placeholders replace private's utc_now_iso() bind param + sqlite3 ?
            (
                source,
                fetched,
                new,
                scored,
                Jsonb(metadata or {}),
            ),  # PORT-SEAM: Jsonb(...) replaces json.dumps(metadata) if metadata else "{}"
        )
    commit_unless_nested(raw)


def persist_job_expiry_state(
    conn: Any,  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only)
    dedup_key: str,
    expiry_status: str,
    checked_at: str,
) -> None:
    """Persist job expiry verdict and timestamp atomically.

    One of the sanctioned writers for expiry_status and expiry_checked_at
    (the other runtime writers are upsert_job and ats_reconciler, each of
    which normalizes its own timestamp values). Called by the scoring
    preflight (per-job liveness check) and the nightly batch expiry runner.

    A 'live' verdict is positive evidence the posting exists — equivalent
    to a feed re-sighting — so it also refreshes last_seen and clears
    is_stale, mirroring what ats_reconciler does for board-confirmed jobs.
    (# PORT-SEAM: private's "Phase C live-verified jobs were still
    clock-archived" incident-history paragraph is dropped here -- historical
    private-repo incident detail, not load-bearing for this port.)

    expiry_checked_at is only updated for VERIFIED outcomes (live/expired).
    INCONCLUSIVE outcomes (transient timeout/connection error) do NOT advance
    the TTL clock, so the scoring loop re-checks liveness on the next tick
    instead of being suppressed for the full TTL window (issue #1055).
    (# PORT-SEAM: private's "Retries on 'database is locked'" paragraph is
    dropped here -- the retry loop itself is dropped, see module docstring.)

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching ``_jobs.py`` / ``_companies.py``'s dispatch.
            (# PORT-SEAM: private said "Open sqlite3 connection".)
        dedup_key: The job's primary key.
        expiry_status: One of 'expired', 'live', or 'inconclusive'.
        checked_at: ISO 8601 timestamp string of when the check ran.
            (# PORT-SEAM: private normalized this string to naive UTC via
            job_finder.json_utils.normalize_iso_string_to_naive_utc before
            the SQL boundary; this host stores timestamptz and psycopg
            parses the ISO string directly, so that normalization step is
            dropped -- there is no naive/aware ambiguity to guard against
            here the way there was for private's naive-UTC-only sqlite3
            TEXT columns.)
    """
    raw = (
        conn.raw if hasattr(conn, "raw") else conn
    )  # PORT-SEAM: replaces private's naive-UTC normalize_iso_string_to_naive_utc(checked_at) call, see Args above

    if expiry_status == "live":
        sql = (
            "UPDATE postings SET expiry_status = %s, expiry_checked_at = %s, "
            "last_seen = %s, is_stale = false WHERE dedup_key = %s"
        )  # PORT-SEAM: %s placeholders replace sqlite3 ?; is_stale = false replaces is_stale = 0
        params = (expiry_status, checked_at, checked_at, dedup_key)
    elif expiry_status == "expired":
        sql = "UPDATE postings SET expiry_status = %s, expiry_checked_at = %s WHERE dedup_key = %s"  # PORT-SEAM: postings replaces private's jobs table
        params = (expiry_status, checked_at, dedup_key)
    else:
        # INCONCLUSIVE: update expiry_status but NOT expiry_checked_at
        # so the TTL gate in scoring_runner.py does not suppress re-check
        sql = "UPDATE postings SET expiry_status = %s WHERE dedup_key = %s"  # PORT-SEAM: postings replaces private's jobs table
        params = (expiry_status, dedup_key)

    with raw.transaction():
        raw.execute(
            sql, params
        )  # PORT-SEAM: private's 3-attempt "database is locked" retry/backoff loop dropped here, see module docstring
    commit_unless_nested(raw)

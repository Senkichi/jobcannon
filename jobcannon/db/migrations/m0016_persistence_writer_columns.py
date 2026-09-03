"""Migration 16 -- pipeline-run log table + posting expiry-check timestamp (L-0073).

Backs the two ``jobcannon/db/_persistence.py`` write paths this migration's
sibling PR lands (private source: ``job_finder/db/_persistence.py`` @
546674e0cc6c35e3511e9f7cf66d1f0a65d880ed):

- ``log_run`` writes a new ``runs`` table. This is a global (not per-user)
  operational log -- ingestion runs are system-wide, not scoped to a
  ``users`` row, matching ``company_scan_log``'s precedent (m0001) for a
  global append-only audit table. ``metadata`` is ``jsonb`` (dominant
  convention per m0001/m0015), not private's ``text`` column holding
  ``json.dumps`` output.
- ``persist_job_expiry_state`` reuses ``postings.expiry_status`` /
  ``last_seen`` / ``is_stale`` (already in m0001) but the host schema has no
  equivalent of private's ``expiry_checked_at`` -- added here, nullable,
  same shape as private's ``jobs.expiry_checked_at``.

Deliberately narrower than private's full ``_persistence.py`` -- this row's
verification.md documents three private write paths NOT ported this PR
(``persist_job_notes``, ``update_pipeline_status``, ``set_job_flag``): each
would need to write into ``pipeline_status`` or a new per-user column, both
of which ``jobcannon/db/_user_actions.py`` already declares itself the sole
writer of, and that file sits outside this ledger group's carried_files
scope. No schema for those three is added here since no writer for them
lands in this migration's sibling PR.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=16,
    description="runs table (log_run) + postings.expiry_checked_at (persist_job_expiry_state)",
    sql=[
        """
        CREATE TABLE IF NOT EXISTS runs (
            id            bigserial PRIMARY KEY,
            "timestamp"   timestamptz NOT NULL DEFAULT now(),
            source        text NOT NULL,
            jobs_fetched  integer NOT NULL DEFAULT 0,
            jobs_new      integer NOT NULL DEFAULT 0,
            jobs_scored   integer NOT NULL DEFAULT 0,
            metadata      jsonb NOT NULL DEFAULT '{}'
        )
        """,
        'CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs("timestamp")',
        "ALTER TABLE postings ADD COLUMN IF NOT EXISTS expiry_checked_at timestamptz",
    ],
)

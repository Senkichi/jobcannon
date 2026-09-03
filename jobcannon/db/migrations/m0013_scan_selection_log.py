"""PORTED from job_finder/web/migrations/m209595733_scan_selection_log_and_run_id.py
@ ec9b1404f684a8f20ad1ec2aa81c3a2f20fc0394 (private job-cannon). Ledger L-0284.
# PORT-SEAM: header inserted above; the private module's own title and body
# below follow verbatim except where another PORT-SEAM note says otherwise.

Migration 209595733 — scan_selection_log table + run_id on company_scan_log (WI-04, D8, F-3).

Adds the per-run selection ledger the ATS Phase-A scanner writes to so that
"why was this company scanned / not scanned on run X" is answerable from the
database instead of being reconstructed from logs. One row per
``(run_id, company_id)`` records the decision for that company: ``selected`` or
one of the ``skipped_*`` reasons. The partition is exhaustive over the Phase-A
base population (every ``scan_enabled=1`` company with a probe hit or a
retry-eligible error), so ``SELECT decision, count(*) ... WHERE run_id=? GROUP
BY 1`` sums to the base count.

``UNIQUE(run_id, company_id)`` enforces the one-row-per-company invariant that
the precedence-ordered ``INSERT…SELECT`` writer in ``_run.py`` relies on (each
reason's INSERT excludes companies already inserted for the run).

Also adds ``run_id TEXT`` to ``company_scan_log`` so every scan-log write can be
correlated back to its selection-ledger row (WI-06 already routes every write
through ``record_scan_outcome``, which passes ``run_id`` the moment this column
exists — no call-site edit needed for it to start persisting). The deadline
sweep uses membership in ``company_scan_log`` for this ``run_id`` as the
"actually reached" test.

# PORT-SEAM: version renumbered to 13 (this host's sequential-integer
scheme; the private original above is an epoch-second stamp -- see
jobcannon/db/migrations/types.py module docstring for why the schemes
differ). All DDL below is dialect-translated SQLite -> Postgres
(bigserial/bigint/timestamptz, REFERENCES companies(id)) with no
DEFAULT/NOT NULL dropped and no existing column narrowed, so this
migration is expand-safe by shape -- no contract_step needed. The private
table's ``job_id`` column (the APScheduler job identifier, e.g. "ats_scan")
is dropped: this host's jobcannon.engine.ats_scanner._run has no per-job
job_id concept to populate it with; add it back in a future migration if
that changes. Schema-only port -- the WI-04 writer (record_selection_batch,
private job_finder/db/_scan_selection.py) is out of scope for ledger
L-0284, which covers only this migration file; no row lands in
scan_selection_log until that writer is ported separately. Both new
indexes are built on the table this same migration creates, so they are
exempt from the lock_step / CREATE INDEX CONCURRENTLY hazard (issue #219)
regardless of row count.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=13,  # PORT-SEAM: see docstring
    description="scan_selection_log ledger table + run_id column on company_scan_log",
    sql=[
        # PORT-SEAM: dialect-translated from the private SQLite DDL above.
        """
        CREATE TABLE IF NOT EXISTS scan_selection_log (
            id bigserial PRIMARY KEY,
            run_id text NOT NULL,
            company_id bigint NOT NULL REFERENCES companies(id),
            decision text NOT NULL CHECK (decision IN (
                'selected', 'skipped_dormant', 'skipped_deadline',
                'skipped_disabled', 'skipped_identity_null',
                'skipped_playwright_excluded')),
            tier integer,
            rank integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (run_id, company_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_ssl_company_created"
        " ON scan_selection_log(company_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ssl_run ON scan_selection_log(run_id)",
        # PORT-SEAM: Postgres dialect (lowercase type; no IF NOT EXISTS here,
        # matching this repo's other ADD COLUMN migrations, e.g. m0003/m0011).
        "ALTER TABLE company_scan_log ADD COLUMN run_id text",
    ],
)

"""PORTED from job_finder/web/migrations/m209605115_scan_title_outcomes.py
@ 6b17d78cb770cf53cb21b5e0b34c2cc7cd203136 (private job-cannon). Ledger L-0287.
# PORT-SEAM: header inserted above; the private module's own title and body
# below follow verbatim except where another PORT-SEAM note says otherwise.

Migration 209605115 — scan_title_outcomes table (WI-09, D20, REPORT §D-4/§F-3).

Per-title disposition capture for the ATS scan path. One row per element of a
relevant company's ``raw_job_dicts`` records what happened to that title on a
run: ``title_filtered`` (present in the raw board but excluded by the title
filter — computed by dict identity, never by re-running the filter),
``dedup_existing`` (survived the filter but the upsert reported the job as not
new), or ``matched`` (a new row was inserted). This makes "which titles did this
company surface, and why didn't they become jobs" answerable from the database.

Bounded by design (D20): captured only for companies already relevant (a job
classified ``apply``/``consider`` or a target-set member at the run's
``fit_floor``), gated by ``ats.title_outcomes_enabled``, and pruned to
``ats.title_outcomes_keep_days`` (default 14). No location filter exists in the
ATS scan path, so no location disposition is recorded.

The WI specifies the DDL literally with no ``CHECK`` constraint on
``disposition`` (unlike WI-04's ``scan_selection_log.decision``); that asymmetry
is intentional, following the WI's exact scope. The writer
(``record_title_outcomes`` in ``job_finder/db/_scan_log.py``) is the single
authority for INSERTs into this table.

# PORT-SEAM: version renumbered to 14 (this host's sequential-integer
scheme; the private original above is an epoch-second stamp -- see
jobcannon/db/migrations/types.py module docstring). DDL below is
dialect-translated SQLite -> Postgres (bigserial/bigint/timestamptz,
REFERENCES companies(id)); a brand-new table with no existing column
touched, so this migration is expand-safe by shape -- no contract_step
needed, and the one index is built on the table this same migration
creates so lock_step (issue #219) does not apply. Schema-only port -- the
WI-09 writer (record_title_outcomes, private job_finder/db/_scan_log.py)
and the config gate/retention prune are out of scope for ledger L-0287,
which covers only this migration file; no row lands in
scan_title_outcomes until that writer is ported separately.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=14,  # PORT-SEAM: see docstring
    description="scan_title_outcomes per-title disposition capture (bounded)",
    sql=[
        # PORT-SEAM: dialect-translated from the private SQLite DDL above.
        """
        CREATE TABLE IF NOT EXISTS scan_title_outcomes (
            id bigserial PRIMARY KEY,
            run_id text,
            company_id bigint NOT NULL REFERENCES companies(id),
            title text NOT NULL,
            disposition text NOT NULL,
            seen_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_sto_company_seen"
        " ON scan_title_outcomes(company_id, seen_at)",
    ],
)

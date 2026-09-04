"""PORTED from job_finder/web/migrations/m209616158_company_state_history.py
@ f20c5b927308f288888fd068a1d3e7af64b644be (private job-cannon). Ledger L-0040.

Migration 209616158 — company_state_history append-only audit log (WI-08).

Records every change to a company's tracked ATS fields (ats_platform,
ats_slug, ats_probe_status, miss_reason, ats_scan_enabled,
careers_scan_enabled — the legacy ``scan_enabled`` column is deliberately
NOT tracked: reading it would trip the WI-13/D16 production-read guard, and
every ``scan_enabled = 0`` write co-writes ``ats_scan_enabled = 0``, so no
transition is lost) together with the code path (``changed_by``) that made
it. Append-only: writers insert one row per changed field; nothing updates
or deletes. See ``jobcannon/db/_company_state.py`` for the sole writer.

# PORT-SEAM: version renumbered to 19 (this host's sequential-integer
scheme -- see jobcannon/db/migrations/types.py module docstring for why the
schemes differ from private's epoch-second stamp). DDL dialect-translated
SQLite -> Postgres: ``id INTEGER PRIMARY KEY`` -> ``bigserial PRIMARY KEY``;
``company_id INTEGER NOT NULL`` -> ``company_id bigint NOT NULL REFERENCES
companies(id)`` (adds referential integrity SQLite's schema didn't declare,
matching this host's other company-scoped log tables, e.g. m0013's
scan_selection_log); ``changed_at TEXT`` -> ``changed_at timestamptz NOT
NULL DEFAULT now()`` (every public migration uses timestamptz;
jobcannon/db/_company_state.py's ``manual_scan_disable_predicate`` compares
``changed_at`` natively as a timestamp instead of private's ISO-text
lexicographic comparison, and the sole writer,
``record_state_change``, omits ``changed_at`` from its INSERT entirely and
lets this DEFAULT fill it, replacing private's Python-computed
``utc_now_iso()`` value). Depends on
jobcannon/db/migrations/m0018_wi13_scan_lane_columns.py landing first (this
host's ``companies`` table has only the merged ``scan_enabled`` bit until
that migration adds the ``ats_scan_enabled``/``careers_scan_enabled`` split
this table's tracked set snapshots).

New table, FK'd to a pre-existing table it only reads/references, no
constraint narrowed -- no ``contract_step``/``lock_step`` needed
(tests/test_migration_deploy_safety.py's "new-table" known-safe shape; the
index is built on the table this same migration creates).
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=19,
    description="company_state_history append-only audit log (WI-08)",
    sql=[
        """
        CREATE TABLE IF NOT EXISTS company_state_history (
            id bigserial PRIMARY KEY,
            company_id bigint NOT NULL REFERENCES companies(id),
            field text NOT NULL,
            old_value text,
            new_value text,
            changed_at timestamptz NOT NULL DEFAULT now(),
            changed_by text NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_company_state_history_company_changed_at"
        " ON company_state_history (company_id, changed_at)",
    ],
)

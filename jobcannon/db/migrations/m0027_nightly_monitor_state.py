"""PORTED (schema-only) from job_finder/web/nightly_monitor/_state.py's
state.json shape
@ e1f47695b07f928e6c91cc64767c97a99645d68f (private job-cannon).
Ledger L-0471.

Migration 27 -- nightly monitor state.

# PORT-SEAM: private's nightly monitor kept its whole state blob in a single
# state.json file (Win32-locked, atomic os.replace on write). This table
# replaces it: one row per state key, a jsonb ``value`` column holding the
# whole blob for that key (matching state.json's single-blob shape 1:1), and
# ``updated_at`` for observability. jobcannon/host/nightly/state.py is the
# sole writer (single-writer discipline mirrors score_audits/companies/jobs
# -- tests/host/test_nightly_state_single_writer.py AST-scans the repo for
# INSERT/UPDATE literals against this table and fails the build if any turn
# up outside that module). Today there is exactly one row
# (key='nightly_monitor'); the key column exists so a later ledger unit
# (the morning-report / audit-stage state that this unit intentionally does
# not port -- see state.py's module docstring) can add sibling rows without
# a schema change.

A brand-new table with no existing column touched: expand-safe by shape, no
contract_step needed. No separate CREATE INDEX statement -- the primary key
already gives ``key`` a unique index -- so lock_step (issue #219) does not
apply either.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=27,
    description="nightly_monitor_state table (dark; sampler/checkpoint state, unwired until JC_NIGHTLY_MONITOR_ENABLED)",
    sql=[
        """
        CREATE TABLE IF NOT EXISTS nightly_monitor_state (
            key text PRIMARY KEY,
            value jsonb NOT NULL DEFAULT '{}'::jsonb,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """,
    ],
)

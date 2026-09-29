"""Migration 31 -- model_usage_ledger: the per-tenant cost/usage ledger
(issue #333).

The hosted cost and usage table this issue mandates. Columns mirror
jobcannon.host.model_provider.record_cost's event shape exactly
(provider/model/cost_usd/input_tokens/output_tokens/job_id/purpose/
user_id/schema_valid), plus the append-only row's own id and created_at.

- ``user_id`` is ``NOT NULL REFERENCES users(id) ON DELETE CASCADE`` --
  the owner-approved column (issue #333 ruling 2026-09-29). The DDL this
  migration verified against is this schema itself: no earlier migration
  creates a cost/usage table (grepped across jobcannon/db/migrations/ --
  this is the first), so the column lands in the CREATE, not an ALTER.
  ON DELETE CASCADE mirrors every other per-user table (profiles,
  feed_state, watchlists, byo_key_credentials), which is also what makes
  jobcannon.host.user_deletion.cascade_delete_user erase a tenant's
  ledger rows with their users row for free.
- ``cost_usd`` is ``double precision`` -- the ledger stores the
  post-normalization figure jobcannon.host.tenant_usage.record_usage
  writes (0.0 for free providers; that module owns the
  FREE_PROVIDER_NAMES rule), never a raw adapter-notional amount, so
  quota SUMs over this column are truthful by construction.
- ``job_id`` is a bare text attribution label (the caller's dedup_key),
  NOT a FK -- cost events can be recorded for postings never upserted,
  and the ledger must never refuse a write because a corpus row is
  absent. Same reasoning as ``purpose``: free-text attribution.
- ``schema_valid`` is nullable boolean -- ModelResult.schema_valid is
  itself tri-state-able (the dispatch loop records whatever the result
  carried, including None).

RLS: ENABLE + FORCE ROW LEVEL SECURITY with the SAME tenant_isolation
policy m0020 established for byo_key_credentials -- this is the second
tenant-scoped table in the schema, and it reuses that migration's
``app.user_id`` session-var convention verbatim (no new convention to
invent): a row is visible/writable only when its user_id matches
``current_setting('app.user_id', true)``, set transaction-locally via
``SELECT set_config('app.user_id', %s, true)`` by this table's single
reader/writer (jobcannon.host.tenant_usage) immediately before every
query. Default-deny holds identically: unset app.user_id -> NULL ->
predicate never true -> zero rows, for every role including the owner.

Indexes: the quota gate reads ``SUM(cost_usd) WHERE user_id AND
created_at >= <rolling window>`` and the rate limiter reads ``COUNT(*)
WHERE user_id AND provider AND created_at >= <window>`` -- the two
(user_id, created_at) / (user_id, provider, created_at) indexes cover
both exactly. Created on a brand-new empty table, so no lock_step
declaration is needed (types.py's convention: an index on a table the
same migration creates never needs one).
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=31,
    description=(
        "model_usage_ledger: per-tenant cost/usage ledger (user_id-scoped, RLS "
        "tenant_isolation) for the #333 quota gate and rate limiter"
    ),
    sql=[
        """
        CREATE TABLE IF NOT EXISTS model_usage_ledger (
            id            bigserial PRIMARY KEY,
            user_id       text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider      text NOT NULL,
            model         text NOT NULL,
            cost_usd      double precision NOT NULL DEFAULT 0,
            input_tokens  integer NOT NULL DEFAULT 0,
            output_tokens integer NOT NULL DEFAULT 0,
            job_id        text,
            purpose       text NOT NULL DEFAULT '',
            schema_valid  boolean,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_model_usage_ledger_tenant_time "
        "ON model_usage_ledger (user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_model_usage_ledger_tenant_provider_time "
        "ON model_usage_ledger (user_id, provider, created_at)",
        "ALTER TABLE model_usage_ledger ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE model_usage_ledger FORCE ROW LEVEL SECURITY",
        # Same idempotent-policy idiom m0020 uses: DROP IF EXISTS + CREATE
        # (there is no CREATE POLICY IF NOT EXISTS).
        "DROP POLICY IF EXISTS tenant_isolation ON model_usage_ledger",
        """
        CREATE POLICY tenant_isolation ON model_usage_ledger
        FOR ALL
        USING (user_id = current_setting('app.user_id', true))
        WITH CHECK (user_id = current_setting('app.user_id', true))
        """,
    ],
)

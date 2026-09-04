"""Migration 20 -- byo_key_credentials RLS policies.

m0001 created byo_key_credentials with ENABLE + FORCE ROW LEVEL SECURITY and
deliberately zero policies (default-deny for every role, including the
table owner). This migration adds the single carve-out: a tenant may see
and write only their own rows.

Ledger L-0036 (design-providers-byokey.md §3): "the same
current_setting('app.user_id')-style predicate the rest of the per-user
tables use -- verify the exact session-var convention before writing."
Verified (grep across jobcannon/db/migrations/): no other table in this
schema is RLS-scoped -- byo_key_credentials is the FIRST. There is no prior
convention to match; this migration establishes app.user_id as that
convention. jobcannon/db/_byo_key_credentials.py -- the table's sole
reader/writer -- sets it via ``SELECT set_config('app.user_id', %s, true)``
(transaction-local, so a pooled connection can never carry one tenant's
scope into the next checkout's queries) immediately before every query
against this table.

One FOR ALL policy (USING + WITH CHECK, same predicate) covers SELECT,
INSERT, UPDATE, and DELETE: a row is visible/writable only when its
user_id matches the session's app.user_id. current_setting(..., true)
(the `true` = missing_ok) returns NULL when unset, and `user_id = NULL` is
never true in SQL -- so a connection that never called set_config sees
zero rows, exactly the default-deny FORCE RLS already gave every role
before this migration, now with the tenant carve-out layered on top.

DROP POLICY IF EXISTS + CREATE POLICY (rather than a nonexistent
CREATE POLICY IF NOT EXISTS) is this repo's idempotent-DDL idiom for
policies specifically -- CREATE TABLE/INDEX use their own native
IF NOT EXISTS.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=20,
    description="byo_key_credentials RLS policies",
    sql=[
        "DROP POLICY IF EXISTS tenant_isolation ON byo_key_credentials",
        """
        CREATE POLICY tenant_isolation ON byo_key_credentials
        FOR ALL
        USING (user_id = current_setting('app.user_id', true))
        WITH CHECK (user_id = current_setting('app.user_id', true))
        """,
    ],
)

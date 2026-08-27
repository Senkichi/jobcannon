"""Migration 7 — revoked_subjects, the DB-backed session-revocation tombstone
(issue #159: a Clerk JWT minted before an in-app account deletion stayed
valid — and kept authorizing /account/export — for up to its own 60s
lifetime after jobcannon/web/account.py::post_delete had already told Clerk
to delete the account, because jobcannon/web/auth.py verifies that JWT
locally with RS256 and makes no per-request network call).

`clerk_user_id` carries NO foreign key to `users(id)` — deliberately. The
whole point of this table is to keep denying a subject's JWT for a bounded
window AFTER the corresponding `users` row (and everything that cascades
from it) is already gone; an FK here, cascading or not, would either block
the `users` DELETE or erase the tombstone at exactly the moment it starts
mattering. The two tables are intentionally decoupled.

`expires_at` is written by the caller (jobcannon/db/_revoked_subjects.py's
`revoke_subject`, `now() + 15 minutes` — generous versus Clerk's ~60s JWT
lifetime), not derived here, so the retention window is a code-level
constant close to the security invariant it protects rather than a schema
default. The index supports both the auth gate's per-request "is this
still live" lookup and the periodic prune's "which rows are stale" sweep.

Deploy order: as of #197 (issue #196), the former "worker deploys first"
caveat here is OBSOLETE. `jobcannon-web`'s Render `preDeployCommand` now
runs `python -m jobcannon.db.migrate` before the new web code ever starts
serving, so this migration is guaranteed to be applied before
`jobcannon/web/account.py`'s `_write_revocation_tombstone` and
`jobcannon/web/webhooks.py`'s `user.deleted` handler — both of which call
`revoke_subject` against this table — can run against the code that calls
them. The worker's boot-time `run_migrations()` call remains as an
idempotent, lock-serialized belt-and-braces (see docs/deploy-runbook.md
§3), covering only the first-deploy-of-a-brand-new-environment case where
nothing has applied any migration yet. Still true regardless of ordering:
this migration is purely additive (new table + index, no change to any
existing table/column), so it stays expand-compatible with every prior
code version, and `revoke_subject`/`webhooks.py`'s `user.deleted` branch
still self-heal (Svix retries; `post_delete` 502s without ever calling
Clerk) in the narrow window before that first migration run completes.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=7,
    description="revoked_subjects tombstone table (issue #159: close the stale-JWT window after account deletion)",
    sql=[
        """
        CREATE TABLE revoked_subjects (
            clerk_user_id text PRIMARY KEY,
            revoked_at    timestamptz NOT NULL DEFAULT now(),
            expires_at    timestamptz NOT NULL
        )
        """,
        "CREATE INDEX idx_revoked_subjects_expires_at ON revoked_subjects(expires_at)",
    ],
)

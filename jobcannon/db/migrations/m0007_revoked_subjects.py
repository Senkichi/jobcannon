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

Deploy order: run this migration AFTER (or at latest concurrently with)
jobcannon-web rolling out `jobcannon/web/account.py`'s
`_write_revocation_tombstone` and `jobcannon/web/webhooks.py`'s
`user.deleted` handler, both of which call `revoke_subject` against this
table. The web and worker services (render.yaml) deploy independently with
no ordering guarantee. Unlike m0010's benign-either-way case, getting this
one backwards is NOT benign: `revoke_subject` raises on the missing table,
so `webhooks.py`'s `user.deleted` branch calls neither `revoke_subject` nor
`delete_user` (webhooks.py:90 puts the revoke first) until the migration
lands, and `account.py::post_delete` returns 502 without ever calling
Clerk. Both self-heal without data loss — Svix retries a failed webhook
delivery for hours, and a 502 in `post_delete` never proceeds to Clerk — so
the actual failure mode is "account deletion is unavailable for a few
minutes," not silent data loss. `jobcannon/worker/__main__.py` is the
single migration authority (docs/deploy-runbook.md §3) and normally
applies pending migrations before the web service's rollout completes
health checks, so this ordering holds in practice without any manual step.
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

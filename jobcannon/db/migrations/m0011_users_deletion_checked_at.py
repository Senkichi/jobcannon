"""Migration 11 — users.deletion_checked_at (issue #136: reconciliation-sweep
rotation cursor).

Issue #136's periodic sweep asks Clerk's Backend API "does this local
`users` row still exist?" for old, non-anon rows, catching a `user.deleted`
webhook Clerk never delivered. A candidate query ordered only by
`created_at ASC LIMIT row_cap` would return the SAME oldest `row_cap` rows
on every run: rows leave that ordering only by being deleted (the rare
outcome), so every row past the cap would be structurally unreachable
forever — the sweep would never actually cover the table, just re-confirm
the same handful of oldest users daily.

This column is the rotation cursor that fixes that: `deletion_checked_at`
is stamped (see jobcannon.db._users.mark_deletion_checked) after every
CONCLUSIVE lookup outcome (Clerk confirms the user still exists) — never
after a deletion (the row is gone, nothing left to stamp) and never after a
lookup ERROR (an erroring row should float back to the front of the next
sweep for a prompt retry, not be treated as "confirmed present" and pushed
to the back of the queue). jobcannon.db._users.
list_users_pending_deletion_reconciliation orders `deletion_checked_at
NULLS FIRST, created_at ASC` — a never-checked row (NULL) always sorts
first, and once every row has been checked at least once, the least-
recently-checked row rotates to the front next, so the whole table cycles
through the sweep over time instead of `row_cap` rows absorbing it forever.

Nullable, no DEFAULT (mirrors m0008's comp_floor_usd precedent for "no
opinion yet" rather than a fabricated timestamp): every existing row starts
NULL, which is exactly "never checked" — the correct initial state, not a
migration-time backfill artifact to special-case.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=11,
    description="users.deletion_checked_at (reconciliation-sweep rotation cursor, issue #136)",
    sql=[
        "ALTER TABLE users ADD COLUMN deletion_checked_at timestamptz",
    ],
)

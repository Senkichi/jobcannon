"""_users.py — the one `users` table writer.

`users.id` is referenced by `profiles.user_id` (FK, no ON CONFLICT fallback:
tests/host/test_profiles_dal.py::test_upsert_profile_fails_loud_on_missing_user
pins the fail-loud contract), so any code path that wants to attach a profile
to a brand-new visitor must insert the parent `users` row first. This module
is that one insertion point, plus the update and delete paths that used to
live inline in jobcannon/web/webhooks.py.

`mint_anon_user(conn)` provisions a throwaway `users` row for an anonymous
visitor — same `INSERT ... ON CONFLICT (id) DO NOTHING` shape as
scripts/seed_guest_demo.py's guest-sentinel seed — and returns the new id so
the caller can immediately `upsert_profile()` against it without racing the
FK. `ensure_user(conn, user_id, email=...)` is the idempotent upsert the
Clerk webhook needs (at-least-once delivery must not clobber a known email
with a later, unresolvable one — COALESCE keeps the old value).
`delete_user(conn, user_id)` is a hard DELETE relying on every child table's
`ON DELETE CASCADE` FK to erase the user's data structurally; this must never
be softened to a status flag or a `SET NULL` — the cascade is the erasure
mechanism, not an incidental detail of it.

Id namespaces stay disjoint: Clerk-issued ids, the `guest_demo` sentinel
(jobcannon/db/_profiles.py), and `anon_*` (this module, via
`ANON_ID_PREFIX` — `mint_anon_user` is the only producer of that prefix).
`email` is never set for anon rows; nothing in this module collects one.

Row-mapping / transaction-nesting conventions mirror
jobcannon/db/_profiles.py: `conn.raw` is unwrapped when present (the pooled
EngineCompatConnection facade), a bare connection is used as-is (the
rollback-isolated `db_conn` test fixture), and `commit_unless_nested` makes
each function safe to call both through a real pooled commit and inside a
test's ambient transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

from jobcannon.db.pool import commit_unless_nested

ANON_ID_PREFIX = "anon_"


def mint_anon_user(conn: Any) -> str:
    """Insert a fresh anon `users` row and return its id."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    user_id = f"{ANON_ID_PREFIX}{uuid.uuid4().hex}"
    raw.execute(
        "INSERT INTO users (id, plan_tier) VALUES (%s, 'free') ON CONFLICT (id) DO NOTHING",
        (user_id,),
    )
    commit_unless_nested(raw)
    return user_id


def ensure_user(conn: Any, user_id: str, *, email: str | None = None) -> None:
    """Idempotent upsert: insert or refresh email, never NULLing a known one."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "INSERT INTO users (id, email) VALUES (%s, %s) "
        "ON CONFLICT (id) DO UPDATE SET email = COALESCE(EXCLUDED.email, users.email)",
        (user_id, email),
    )
    commit_unless_nested(raw)


def delete_user(conn: Any, user_id: str) -> None:
    """Hard delete; every per-user child table cascades via its own FK."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute("DELETE FROM users WHERE id = %s", (user_id,))
    commit_unless_nested(raw)


def is_anon_id(user_id: str) -> bool:
    return user_id.startswith(ANON_ID_PREFIX)

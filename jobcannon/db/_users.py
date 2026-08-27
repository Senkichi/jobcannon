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

`reap_unconverted_anon_users(conn, retention_days=...)` is a periodic
maintenance DELETE (issue #48), not a request-path write. A visitor who
completes the onboarding picker without ever signing up leaves their anon
`users` row behind forever — nothing else ever revisits it. The predicate is
exactly "anon-namespaced and older than the retention window", not a second
"does it have a profile" check: `jobcannon/web/onboarding.py`'s picker
submit mints the row and upserts its profile in one transaction, so every
anon row has a profile from the moment it exists — a profile is not a
conversion signal here, it is what minting itself creates. Conversion is
`jobcannon/web/handoff.py`'s DB phase, and it does not leave a marker on the
anon row to check for: it re-keys the profile onto the real Clerk id and
hard-deletes the anon `users` row outright. So a surviving `anon_*` row is,
by construction, always unconverted — there is no distinct "converted but
still anon-prefixed" state to filter out. The one extra guard kept is
`NOT EXISTS` against `events`: every authenticated-only route requires a
Clerk identity (`jobcannon/web/__init__.py`'s `PUBLIC_PATHS` gate), so an
anon id should never accumulate an events row either, but that invariant is
flagged as drift-prone in handoff.py's own docstring ("if a pre-signup
surface is ever instrumented..."). The guard costs nothing when it never
matches and fails in the safe direction (skips the row) if that invariant
is ever broken, rather than cascading a real visitor's analytics rows away
silently.

Row-mapping / transaction-nesting conventions mirror
jobcannon/db/_profiles.py: `conn.raw` is unwrapped when present (the pooled
EngineCompatConnection facade), a bare connection is used as-is (the
rollback-isolated `db_conn` test fixture), and `commit_unless_nested` makes
each function safe to call both through a real pooled commit and inside a
test's ambient transaction.

`list_users_pending_deletion_reconciliation` / `mark_deletion_checked`
(issue #136) are the candidate-selection half of the reconciliation sweep
(jobcannon.host.user_deletion.run_reconciliation_sweep) that catches a
`user.deleted` webhook Clerk never delivered. See m0011's docstring for why
the ordering needs a rotation cursor (`deletion_checked_at`) rather than a
plain `created_at ASC` — without it, every row past the sweep's row_cap
would be permanently unreachable.
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


def reap_unconverted_anon_users(conn: Any, *, retention_days: int) -> list[str]:
    """Hard-delete anon `users` rows past the retention window; cascades take
    the profile (and anything else per-user) with them. Returns the reaped
    ids so a caller can log/count them — see this module's docstring for why
    the predicate is exactly namespace + age, plus one `events` safety net."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    # `_` is a LIKE wildcard (matches any single char); escape the literal
    # underscore in ANON_ID_PREFIX so the pattern can't accidentally widen.
    like_pattern = ANON_ID_PREFIX.replace("_", "\\_") + "%"
    rows = raw.execute(
        "DELETE FROM users "
        "WHERE id LIKE %s ESCAPE '\\' "
        "AND created_at < now() - make_interval(days => %s) "
        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.user_id = users.id) "
        "RETURNING id",
        (like_pattern, retention_days),
    ).fetchall()
    commit_unless_nested(raw)
    return [row["id"] for row in rows]


def list_users_pending_deletion_reconciliation(
    conn: Any, *, settle_days: int, limit: int
) -> list[str]:
    """Candidate non-anon `users` ids for issue #136's reconciliation sweep:
    old enough (`settle_days`) to rule out racing a same-day Clerk deletion
    that hasn't propagated yet, ordered `deletion_checked_at NULLS FIRST,
    created_at ASC` so the sweep rotates through the whole table over time
    instead of the same oldest `limit` rows forever (see m0011's docstring).
    Read-only; the caller commits nothing here."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    like_pattern = ANON_ID_PREFIX.replace("_", "\\_") + "%"
    rows = raw.execute(
        "SELECT id FROM users "
        "WHERE id NOT LIKE %s ESCAPE '\\' "
        "AND created_at < now() - make_interval(days => %s) "
        "ORDER BY deletion_checked_at NULLS FIRST, created_at ASC "
        "LIMIT %s",
        (like_pattern, settle_days, limit),
    ).fetchall()
    return [row["id"] for row in rows]


def mark_deletion_checked(conn: Any, user_id: str) -> None:
    """Stamps a row as checked-and-confirmed-present (issue #136) so the
    next sweep rotates past it. Never call this for a row that turned out
    to be deleted (delete_user's DELETE removes the row itself — nothing
    left to stamp) or after a lookup ERROR (an erroring row should float
    back to the front of the next sweep for a prompt retry, not be treated
    as confirmed-present)."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute("UPDATE users SET deletion_checked_at = now() WHERE id = %s", (user_id,))
    commit_unless_nested(raw)

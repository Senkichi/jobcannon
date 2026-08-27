"""save_posting / unsave_posting / dismiss_posting / mark_applied — the
single writer for `watchlists` and `pipeline_status`. Neither table has had
an application writer before this module: `watchlists`' only prior writer is
its own DDL, and `pipeline_status`'s only prior touch is the raw-SQL
constraint/cascade tests in tests/host/test_schema.py.

`_PIPELINE_STATUSES` is the write-boundary vocabulary check `pipeline_status`
needs and does not get from the schema itself: the column carries no CHECK
constraint (adding one would require a migration, out of scope here), so an
unlisted status raises ValueError in Python, before any SQL is issued —
invalid states are unrepresentable through this module even though the
database itself would accept them.

`dismiss_posting` and `mark_applied` share one row on the `pipeline_status`
`(user_id, posting_id)` PRIMARY KEY: applying after a dismiss overwrites
`status` to `'applied'`; dismissing after an apply overwrites `status` to
`'dismissed'` and — because that branch's UPDATE SET clause never lists
`applied_at` — leaves `applied_at` intact. This is the documented
consequence for whichever future view reads `pipeline_status`: it must
exclude `status = 'dismissed'` rather than trust `applied_at`'s
presence/absence alone.

Every write commits its own connection via `commit_unless_nested` (mirrors
`jobcannon/db/_profiles.py`: called BOTH through a bare pooled connection
from a Flask route AND directly against tests/host/conftest.py's
rollback-isolated `db_conn` fixture, where the no-op path applies) — unlike
`jobcannon/db/_events.py`'s `record_consent`, no write here shares a
connection with a second statement that needs the commit deferred to a
caller, so there is no reason to withhold it.

Timestamps are SQL `now()` inside the statements, never a Python
wall-clock read (`datetime.now`/`utcnow`) passed as a parameter.

Row access: STRING-KEY only, matching every other DAL module in this package
(`_profiles.py`, `_feed.py`, `_stats.py`) — `raw = conn.raw if hasattr(conn,
"raw") else conn` so callers can pass either an EngineCompatConnection-wrapped
pooled connection or a bare psycopg connection (tests/host/conftest.py's
`db_conn` fixture).
"""

from __future__ import annotations

from typing import Any

from jobcannon.db.pool import commit_unless_nested

_PIPELINE_STATUSES = frozenset({"dismissed", "applied"})


def save_posting(conn: Any, user_id: str, posting_id: int) -> None:
    """INSERT ... ON CONFLICT DO NOTHING against the `posting_id` branch of
    the partial unique index (`watchlists_user_posting_uq`,
    `m0001_initial_schema.py`) — a repeat save for the same (user, posting)
    is a no-op, making this idempotent under a double-submit."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "INSERT INTO watchlists (user_id, posting_id, created_at) "
        "VALUES (%s, %s, now()) "
        "ON CONFLICT (user_id, posting_id) WHERE posting_id IS NOT NULL DO NOTHING",
        (user_id, posting_id),
    )
    commit_unless_nested(raw)


def unsave_posting(conn: Any, user_id: str, posting_id: int) -> None:
    """Removes a `watchlists` row on the `posting_id` branch. Idempotent: a
    repeat call against an already-removed (or never-saved) row is a no-op.
    No route calls this yet (the shipped save/dismiss/apply surface has no
    un-save control); it exists so this module is the complete single writer
    for the table rather than a partial one a later PR would have to extend
    from a second place."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "DELETE FROM watchlists WHERE user_id = %s AND posting_id = %s",
        (user_id, posting_id),
    )
    commit_unless_nested(raw)


def _set_pipeline_status(
    conn: Any,
    user_id: str,
    posting_id: int,
    status: str,
    *,
    set_applied_at: bool = False,
) -> None:
    if status not in _PIPELINE_STATUSES:
        raise ValueError(
            f"invalid pipeline_status value: {status!r} "
            f"(must be one of {sorted(_PIPELINE_STATUSES)})"
        )
    raw = conn.raw if hasattr(conn, "raw") else conn
    if set_applied_at:
        raw.execute(
            "INSERT INTO pipeline_status "
            "(user_id, posting_id, status, status_changed_at, applied_at) "
            "VALUES (%s, %s, %s, now(), now()) "
            "ON CONFLICT (user_id, posting_id) DO UPDATE SET "
            "status = EXCLUDED.status, status_changed_at = now(), applied_at = now()",
            (user_id, posting_id, status),
        )
    else:
        # applied_at is deliberately absent from this SET clause: a dismiss
        # (the only non-apply caller today) must never clear an applied_at
        # an earlier apply already wrote — see module docstring.
        raw.execute(
            "INSERT INTO pipeline_status (user_id, posting_id, status, status_changed_at) "
            "VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (user_id, posting_id) DO UPDATE SET "
            "status = EXCLUDED.status, status_changed_at = now()",
            (user_id, posting_id, status),
        )
    commit_unless_nested(raw)


def dismiss_posting(conn: Any, user_id: str, posting_id: int) -> None:
    _set_pipeline_status(conn, user_id, posting_id, "dismissed")


def mark_applied(conn: Any, user_id: str, posting_id: int) -> None:
    _set_pipeline_status(conn, user_id, posting_id, "applied", set_applied_at=True)


def unmark_applied(conn: Any, user_id: str, posting_id: int) -> None:
    """Issue #177's Undo: removes the `pipeline_status` row entirely rather
    than flipping `status` to some third "neutral" value -- there is no
    neutral status in `_PIPELINE_STATUSES`; a row's absence IS the neutral
    state a posting starts in before any save/dismiss/apply. Scoped to
    `status = 'applied'` so an Undo can never accidentally delete a
    `'dismissed'` row it wasn't rendered against (the Undo control only ever
    appears on a row whose `entry.applied` is True, but the WHERE clause
    enforces that invariant at the write itself rather than trusting the
    caller). Idempotent: a repeat call against an already-undone (or never-
    applied) posting is a no-op, matching `unsave_posting`'s contract."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "DELETE FROM pipeline_status WHERE user_id = %s AND posting_id = %s AND status = 'applied'",
        (user_id, posting_id),
    )
    commit_unless_nested(raw)


def list_watchlist_entries(conn: Any, user_id: str) -> list[Any]:
    """Read-only: every `watchlists` row for this user (saved postings and/or
    saved companies), oldest first. Unlike `jobcannon.db._feed.list_feed_postings`'s
    LEFT JOIN (which only surfaces a boolean `saved` flag per posting), this
    returns the raw rows themselves — the shape a self-service export needs.
    The account-export route (jobcannon/web/export.py) is the first caller;
    no other route lists this table today."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    return raw.execute(
        "SELECT id, posting_id, company_id, notes, created_at FROM watchlists "
        "WHERE user_id = %s ORDER BY created_at",
        (user_id,),
    ).fetchall()


def list_pipeline_status_entries(conn: Any, user_id: str) -> list[Any]:
    """Read-only: every `pipeline_status` row for this user, including
    dismissed postings — unlike `jobcannon.db._feed.list_feed_postings`,
    which excludes `status = 'dismissed'` rows for feed display, a
    self-service export must include them (a user's own dismissal is still
    their own data). The account-export route (jobcannon/web/export.py) is
    the first caller."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    return raw.execute(
        "SELECT posting_id, status, status_changed_at, applied_at, notes "
        "FROM pipeline_status WHERE user_id = %s ORDER BY status_changed_at",
        (user_id,),
    ).fetchall()

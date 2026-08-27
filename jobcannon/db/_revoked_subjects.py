"""_revoked_subjects.py — the one `revoked_subjects` table reader/writer
(issue #159: close the stale-Clerk-JWT window after an account deletion).

`auth.py` verifies the `__session` JWT locally (RS256, no network call), so
a token minted moments before a deletion stays valid until its own `exp` —
Clerk's session lifetime, observed ~60s. This table is a short-lived
tombstone: once a subject's id is written here, `jobcannon/web/__init__.py`'s
`clerk_auth` gate rejects ANY verified JWT for that subject, on ANY worker
process (this is why the tombstone is a DB row and not in-memory state —
gunicorn workers share no memory), until the row expires.

Two writers, both calling `revoke_subject` — never a bare INSERT/UPDATE
against this table anywhere else:
  - `jobcannon/web/account.py::post_delete`, BEFORE it calls Clerk's
    user-delete management endpoint (the in-app deletion path).
  - `jobcannon/web/webhooks.py`'s `user.deleted` handler (covers a deletion
    started from Clerk's own Account Portal, which never touches
    account.py at all).
Both are expected to eventually fire for the SAME subject on an in-app
deletion (account.py first, synchronously; the webhook later, async, once
Clerk delivers it) — `revoke_subject` is an upsert for exactly that reason:
`ON CONFLICT ... DO UPDATE` re-stamps `expires_at` off the write's own `now()`
rather than `DO NOTHING`, so the later of the two calls EXTENDS the
protection window instead of the second write silently no-opping against
the first. That is deliberate, not an oversight to "simplify" to DO NOTHING.

`REVOCATION_TTL_MINUTES = 15` is a code-level constant (not read from an
env var / HostConfig field) on purpose — mirrors m0007's own docstring: the
retention window is a security invariant ("generous versus Clerk's ~60s JWT
lifetime"), not an operator-tunable knob, so it lives next to the code that
depends on it rather than in render.yaml.

`is_subject_revoked` fails NEITHER open nor closed on its own — it has no
try/except at all. `jobcannon/web/__init__.py`'s `_is_subject_revoked`
wrapper is what decides the failure posture (fail-open on a DB/pool error),
and that decision belongs there because every other authed route already
needs the same pool: a pool outage already fails `/`, `/account/export`,
etc, so this check failing open does not open a new "revoked user gets
served real data" path, only "revoked user gets the same degraded response
every live user gets."

Row-mapping / transaction-nesting conventions mirror jobcannon/db/_events.py
and jobcannon/db/_users.py: `conn.raw` is unwrapped when present, and
`commit_unless_nested` makes each function safe both through a real pooled
commit and inside a test's ambient rollback-isolated transaction.
"""

from __future__ import annotations

from typing import Any

from jobcannon.db.pool import commit_unless_nested

REVOCATION_TTL_MINUTES = 15


def revoke_subject(conn: Any, user_id: str) -> None:
    """Upsert a tombstone for `user_id`, expiring `REVOCATION_TTL_MINUTES`
    from THIS call's own `now()`. Safe to call more than once for the same
    subject (see module docstring for why that happens by design, not by
    accident) — each call re-stamps `revoked_at`/`expires_at`, extending the
    window rather than erroring on the primary-key conflict."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "INSERT INTO revoked_subjects (clerk_user_id, expires_at) "
        "VALUES (%s, now() + make_interval(mins => %s)) "
        "ON CONFLICT (clerk_user_id) DO UPDATE SET "
        "revoked_at = now(), expires_at = EXCLUDED.expires_at",
        (user_id, REVOCATION_TTL_MINUTES),
    )
    commit_unless_nested(raw)


def is_subject_revoked(conn: Any, user_id: str) -> bool:
    """True iff `user_id` carries an unexpired tombstone. A row past its own
    `expires_at` reads as False — pruning it is the periodic sweep's job
    (`jobcannon.host.tasks.reap_revoked_subjects`), not this read path's; an
    unpruned-but-expired row must never keep denying access."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT 1 FROM revoked_subjects WHERE clerk_user_id = %s AND expires_at > now() LIMIT 1",
        (user_id,),
    ).fetchone()
    return row is not None


def prune_expired_revocations(conn: Any) -> list[str]:
    """Hard-delete every tombstone past its own `expires_at`. Returns the
    reaped subject ids so a caller can log/count them — the periodic task
    itself must summarize this to a count before returning (procrastinate
    persists task return values into the same database being reaped),
    mirroring `jobcannon.db._events.delete_expired_events`."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    rows = raw.execute(
        "DELETE FROM revoked_subjects WHERE expires_at <= now() RETURNING clerk_user_id"
    ).fetchall()
    commit_unless_nested(raw)
    return [row["clerk_user_id"] for row in rows]

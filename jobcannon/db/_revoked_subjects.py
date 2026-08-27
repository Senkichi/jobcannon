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

Issue #159 follow-up (post-review, corroborated by two independent
refuters): `account.py::post_delete` writes this tombstone BEFORE calling
Clerk's delete, and on a Clerk-call failure (timeout, transient 5xx) the
tombstone is deliberately left in place (see that module's docstring for
why rolling it back would reopen the race on Clerk's own lost-response
case). Without a freshness signal, that left every still-existing,
never-actually-deleted account hard-locked out of the entire authed
surface for the full TTL with NO recovery path — not even a fresh
relogin, because a new JWT still carries the same `sub`. `is_subject_revoked`
now optionally takes the verified JWT's `iat` (issued-at, seconds since
epoch) and denies only tokens minted at-or-before the tombstone's own
`revoked_at`, so a fresh post-relogin token is allowed through while every
token that could plausibly have existed before the deletion attempt stays
denied. `issued_at=None` (the default, matching every pre-existing call
site) or any value that fails to parse as a Unix timestamp deny
unconditionally — there is no freshness signal to trust, so the safe
default is "treat as pre-revocation."
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jobcannon.db.pool import commit_unless_nested

REVOCATION_TTL_MINUTES = 15

# Widens the DENY band, not the allow band: an iat only a few seconds after
# revoked_at (plausible Clerk-vs-Postgres clock drift, not a real relogin)
# must still be treated as pre-revocation. Real recovery latency here is a
# human reading a 502 and re-navigating/re-authenticating -- orders of
# magnitude larger than a few seconds -- so widening the deny side costs
# nothing observable while keeping the boundary fail-safe. Do not flip the
# sign: `revoked_at - tolerance` would instead let a stale token through on
# skew, reopening #159's own window.
CLOCK_SKEW_TOLERANCE_SECONDS = 5


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


def is_subject_revoked(conn: Any, user_id: str, issued_at: float | int | None = None) -> bool:
    """True iff `user_id` carries an unexpired tombstone whose `revoked_at`
    the caller's token cannot be proven to postdate.

    `issued_at` is the verified JWT's `iat` claim (Unix seconds). When it is
    `None` (every call site before the #159 follow-up, and any production
    JWT payload that somehow lacks the standard claim) or fails to parse as
    a real timestamp, there is no freshness signal to trust — deny
    unconditionally, same as before this parameter existed. When present
    and parseable, a token minted strictly after `revoked_at + tolerance`
    passes even though the row is still within its TTL; see module
    docstring for why this is safe (the tombstone denies the exact token
    that could have existed pre-deletion, not the subject forever) and why
    the tolerance widens the deny side, not the allow side."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT revoked_at FROM revoked_subjects "
        "WHERE clerk_user_id = %s AND expires_at > now() LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None:
        return False
    if issued_at is None or isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)):
        return True
    try:
        issued_at_dt = datetime.fromtimestamp(issued_at, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return True
    revoked_at = row["revoked_at"]
    return issued_at_dt <= revoked_at + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS)


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

"""insert_event / read_consent_state / record_consent / delete_expired_events
— the single sanctioned events-table writer/deleter (1B Wave 2 PR 8).

No other module may write to `events` or `users.analytics_consent` with raw
SQL — tests/host/test_events_single_writer.py AST-scans jobcannon/host,
jobcannon/web, and jobcannon/db (exempting this file and
jobcannon/db/migrations) for INSERT/UPDATE literals against those targets and
fails the build if any turn up outside this file. That scan is a best-effort
static lint (it cannot see runtime-assembled SQL); the payload allowlist is
additionally enforced at the write boundary — insert_event calls
events_schema.validate_payload before every write. jobcannon/host/events.py
(log_event) is the only intended caller for insert_event; record_consent is
the single writer for the users.analytics_consent column and is meant to be
called from a future consent-settings route (not wired to one yet).

Payload values are wrapped in psycopg's Jsonb adapter (matching every other
jsonb write in this codebase — _companies.py / _jobs.py / _jd_full.py /
health_recorder.py) rather than pre-serialized with json.dumps(): a bare
Python str bound to a jsonb column has no implicit assignment cast in
Postgres and raises DatatypeMismatch, whereas Jsonb() does the parameter
binding correctly.

Row access note (matches _companies.py / _jobs.py): `raw = conn.raw if
hasattr(conn, "raw") else conn` so callers can pass either an
EngineCompatConnection-wrapped connection (connection_factory()'s pooled
connections) or a bare psycopg connection (tests/host/conftest.py's db_conn
fixture) — both support `.execute(sql, params)`.

`delete_expired_events(conn, retention_days=...)` is a periodic maintenance
DELETE (the events-retention issue), not a request-path write — the DELETE
lives here rather than jobcannon/host/tasks.py because this module is the
events table's sole sanctioned writer/deleter, same reasoning that keeps
reap_unconverted_anon_users inside jobcannon/db/_users.py rather than tasks.py.
Named distinctly from jobcannon.host.tasks.reap_old_events (the periodic task
that calls it) so importing it at module scope in tasks.py can't shadow the
task function's own name — same split as reap_unconverted_anon_users (DAL)
vs. reap_anon_users (task).
The predicate excludes two things deliberately: anon-namespaced user ids
(mirrors jobcannon/db/_users.py's `ANON_ID_PREFIX` — those rows are expected
to be cascade-deleted when reap_unconverted_anon_users reaps the parent
`users` row, not trimmed in place here) and every type in
events_schema.DURABLE_EVENT_TYPES at any age — not just `consent_recorded`
(the consent audit trail) but also `user_signed_up`, which
has_signed_up_event (below) durably keys off of to avoid re-emitting signup
attribution for a returning user whose cookie jar was cleared; reaping it
would silently re-open that gap on a long enough retention window.
`user_id IS NOT NULL` is spelled out even though `NOT LIKE` on NULL already
evaluates to NULL/excluded — an explicit predicate makes "anonymous-session
rows are left alone" a readable part of the WHERE clause rather than an
accidental side effect of NULL semantics.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from jobcannon.db import events_schema
from jobcannon.db._users import ANON_ID_PREFIX
from jobcannon.db.pool import commit_unless_nested


def insert_event(
    conn: Any,
    *,
    event_type: str,
    user_id: str | None,
    posting_id: int | None = None,
    feed_position: int | None = None,
    ranker_version: str | None = None,
    feed_session_id: str | None = None,
    interleave_experiment_id: str | None = None,
    interleave_team: str | None = None,
    payload: dict | None = None,
) -> None:
    events_schema.validate_payload(event_type, payload)
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "INSERT INTO events (user_id, event_type, posting_id, feed_position, ranker_version, "
        "feed_session_id, interleave_experiment_id, interleave_team, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            user_id,
            event_type,
            posting_id,
            feed_position,
            ranker_version,
            feed_session_id,
            interleave_experiment_id,
            interleave_team,
            Jsonb(payload) if payload is not None else None,
        ),
    )


def read_consent_state(conn: Any, user_id: str, *, current_version: str) -> bool:
    """True only for a GRANT recorded at `current_version` (m0006). A grant
    recorded at any other stored version — including NULL, the value every
    row that predates version tracking carries — reads as not consented,
    forcing a re-grant before analytics resumes. A decline is unaffected by
    `current_version` by construction: `analytics_consent` is already false
    for a decline, so the version comparison is never reached for one.
    `current_version` is keyword-only so it can never be transposed with
    `user_id` at a call site — both are plain strings."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT analytics_consent, analytics_consent_version FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    if not row or not row["analytics_consent"]:
        return False
    return row["analytics_consent_version"] == current_version


def has_signed_up_event(conn: Any, user_id: str) -> bool:
    """True if a `user_signed_up` row already exists for this Clerk user id,
    regardless of which browser session recorded it.

    The durable, per-`user_id` complement to jobcannon/web/handoff.py's
    per-session `_HANDOFF_DONE_KEY` cookie marker. The session marker alone
    only prevents re-emission *within one cookie jar*; a second device, an
    incognito window, a cleared cookie jar, or the session cookie's own
    default (non-permanent, browser-lifetime) expiry all mint a fresh jar
    that has never seen `_HANDOFF_DONE_KEY` and would otherwise re-run the
    full emission path for a user who has already signed up. Callers should
    only pay for this SELECT on the already-narrow path where the cheap
    session check says "not done yet" — see handoff.py's `_should_emit()`.

    `events` carries no uniqueness constraint on `(user_id, event_type)`
    (no migration adds one), so this is a best-effort check-before-write
    guard, not a schema-enforced one: a narrow race between two concurrent
    *first* authed requests for the same user (both reading "no row" before
    either writes) is accepted as strictly better than today's guaranteed
    duplication on every fresh cookie jar."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT 1 FROM events WHERE user_id = %s AND event_type = 'user_signed_up' LIMIT 1",
        (user_id,),
    ).fetchone()
    return row is not None


def db_now_iso(conn: Any) -> str:
    """The database's own clock, formatted as e.g. "2026-08-13T18:04:11.512034Z".

    The ONLY sanctioned source of record_consent's `consented_at` argument: a
    caller that reads this on the SAME connection inside the SAME transaction
    as a subsequent record_consent() call gets back a string drawn from that
    transaction's own now() — byte-identical to what record_consent's UPDATE
    stores in analytics_consent_updated_at (also SQL now()), because psycopg3
    connections here are not autocommit (jobcannon/db/pool.py). This is how a
    process-wall-clock-free consented_at is produced without weakening the
    no-datetime.now()-in-persistence-paths rule to a bare exception.

    Quoting note: the to_char format string below embeds Postgres-literal
    double quotes around T and Z, so this SQL is written as a Python
    triple-single-quoted string — a double-quoted Python string would
    terminate early at the first embedded `"` and either fail to parse or
    silently produce the wrong format.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        """SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS now_iso"""
    ).fetchone()
    return row["now_iso"]


def read_consent_choice_made(conn: Any, user_id: str, *, current_version: str) -> bool:
    """ "Has a choice been recorded that still stands?" — False for the two
    original no-choice cases (row exists but analytics_consent_updated_at is
    NULL — never chosen — or no row exists at all — unknown user) PLUS one
    more that m0006 adds: a GRANT recorded at a version other than
    `current_version`. That third case is deliberately asymmetric with
    decline: re-consent exists to re-ask people whose grant predates new
    tracking, not to nag decliners, so a decline at any stored version — or
    NULL, for a decline recorded before version tracking existed — still
    reads as "choice made" and is never re-prompted. Only a stale GRANT
    forces this back to False, which re-fires the existing
    handoff.py redirect the next time `_pending()` allows it to run.

    Do not branch callers on which no-choice case applies. If a future
    caller needs that distinction, add a separate reader rather than
    changing this one's return type.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT analytics_consent, analytics_consent_updated_at, analytics_consent_version "
        "FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    if not row or row["analytics_consent_updated_at"] is None:
        return False
    if row["analytics_consent"] and row["analytics_consent_version"] != current_version:
        return False
    return True


def record_consent(
    conn: Any,
    *,
    user_id: str,
    consent_type: str,
    granted: bool,
    consent_version: str,
    consented_at: str,
) -> None:
    """Update the current-consent columns AND insert the consent_recorded
    audit event against the SAME connection, so both writes land in one
    transaction under the caller's commit boundary (mirrors _companies.py /
    _jobs.py: no commit here — the caller wraps this in connection_factory()
    + pool.commit_unless_nested(), or a test's rollback-isolated db_conn).

    `consent_type` selects one of two literal UPDATE statements (kept as
    two separate, fully-spelled-out statements rather than one
    column-name-mapping-driven UPDATE — the two consent domains' column
    sets already differ, `analytics` carries a version column and
    `mailbox` (m0025, design note §1.2) does not, so a generic mapping
    would be a leaky abstraction for a shape that doesn't hold; keeping
    both as visible literals also keeps them legible to
    tests/host/test_events_single_writer.py's string-literal AST scan):

    - "analytics": `analytics_consent_version` (m0006) is set
      unconditionally, on a decline as well as a grant — the version stored
      on a decline row is never consulted by read_consent_state/
      read_consent_choice_made (a decline's analytics_consent is already
      false, which short-circuits both readers before the version
      comparison), so this is inert for a decline and is the only column
      that lets a later grant's version-mismatch check work at all.
    - "mailbox": no version column exists or is needed yet (single
      mailbox-consent version in flight); `consent_version` stays a
      required kwarg regardless of `consent_type` so the audit event's
      payload shape is uniform across both consent domains — it is simply
      not persisted to a `users` column for this branch.

    Any other `consent_type` raises ValueError before either write is
    attempted — there is no silent third branch.

    The payload is validated against events_schema's PII allowlist + 200-char
    cap BEFORE either write is issued — the same validation every log_event
    write gets — so an oversized/illegal value aborts the whole write, not
    just the event insert."""
    payload = {
        "consent_type": consent_type,
        "granted": granted,
        "consent_version": consent_version,
        "consented_at": consented_at,
    }
    events_schema.validate_payload("consent_recorded", payload)
    raw = conn.raw if hasattr(conn, "raw") else conn
    if consent_type == "analytics":
        raw.execute(
            "UPDATE users SET analytics_consent = %s, analytics_consent_updated_at = now(), "
            "analytics_consent_version = %s WHERE id = %s",
            (granted, consent_version, user_id),
        )
    elif consent_type == "mailbox":
        raw.execute(
            "UPDATE users SET mailbox_consent = %s, mailbox_consent_updated_at = now() "
            "WHERE id = %s",
            (granted, user_id),
        )
    else:
        raise ValueError(f"record_consent: unknown consent_type {consent_type!r}")
    insert_event(raw, event_type="consent_recorded", user_id=user_id, payload=payload)


def read_mailbox_consent(conn: Any, user_id: str) -> bool:
    """True only when the tenant has an active mailbox-intake grant.

    Mirrors read_consent_state's shape but mailbox consent has no version
    column to compare (see record_consent's docstring) — a bare boolean
    read is the whole contract. jobcannon.host.credentials.build_mailbox_
    resolver calls this FIRST and fails closed (returns None from every
    resolve call) when it reads False, even if an active mailbox_credentials
    row exists — consent is the gate, not the row's mere presence."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT mailbox_consent FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    return bool(row and row["mailbox_consent"])


def delete_expired_events(conn: Any, *, retention_days: int) -> list[int]:
    """Hard-delete `events` rows older than the retention window for
    non-anon user ids, excluding every type in
    events_schema.DURABLE_EVENT_TYPES (see this module's docstring). Returns
    the reaped event ids so a caller can log/count them; the periodic task
    itself must summarize this to a count before returning (procrastinate
    persists task return values into the same database being reaped)."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    # `_` is a LIKE wildcard (matches any single char); escape the literal
    # underscore in ANON_ID_PREFIX so the pattern can't accidentally widen.
    like_pattern = ANON_ID_PREFIX.replace("_", "\\_") + "%"
    rows = raw.execute(
        "DELETE FROM events "
        "WHERE user_id IS NOT NULL "
        "AND user_id NOT LIKE %s ESCAPE '\\' "
        "AND NOT (event_type = ANY(%s)) "
        "AND occurred_at < now() - make_interval(days => %s) "
        "RETURNING id",
        (like_pattern, list(events_schema.DURABLE_EVENT_TYPES), retention_days),
    ).fetchall()
    commit_unless_nested(raw)
    return [row["id"] for row in rows]


def list_events_for_user(conn: Any, user_id: str) -> list[Any]:
    """Read-only: every `events` row for this user, oldest first. A SELECT,
    not an INSERT/UPDATE, so it does not trip
    tests/host/test_events_single_writer.py's AST guard (see that test's own
    `_is_forbidden_events_sql` predicate coverage, which explicitly asserts a
    bare SELECT is not flagged). The account-export route
    (jobcannon/web/export.py) is the first caller."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    return raw.execute(
        "SELECT id, event_type, posting_id, feed_position, ranker_version, "
        "feed_session_id, interleave_experiment_id, interleave_team, occurred_at, payload "
        "FROM events WHERE user_id = %s ORDER BY occurred_at, id",
        (user_id,),
    ).fetchall()


def read_latest_consent_record(conn: Any, user_id: str) -> dict | None:
    """Read-only: the payload of this user's most recent `consent_recorded`
    event — `{consent_type, granted, consent_version, consented_at}` — the
    same four keys `record_consent` writes. None if the user has never
    recorded a choice (mirrors `read_consent_choice_made`'s False case,
    which is the cheaper column-only check for the same fact; this function
    exists because a self-service export additionally needs the version and
    exact timestamp, which only live in the event payload, not on `users`).
    The account-export route (jobcannon/web/export.py) is the first caller."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT payload FROM events WHERE user_id = %s AND event_type = 'consent_recorded' "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["payload"] if row is not None else None

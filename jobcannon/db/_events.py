"""insert_event / read_consent_state / record_consent — the single sanctioned
events-table writer (1B Wave 2 PR 8).

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
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from jobcannon.db import events_schema


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


def read_consent_state(conn: Any, user_id: str) -> bool:
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute("SELECT analytics_consent FROM users WHERE id = %s", (user_id,)).fetchone()
    return bool(row and row["analytics_consent"])


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


def read_consent_choice_made(conn: Any, user_id: str) -> bool:
    """ "Has a choice been recorded?" — False for BOTH no-choice cases: the
    row exists but analytics_consent_updated_at is NULL (never chosen), and
    no row exists at all (unknown user). The two are deliberately collapsed
    because every caller today does the same thing with either answer: show
    the consent surface. This is the only way to distinguish "never chose"
    from "declined" — both leave analytics_consent = false; only this
    column, and only record_consent, ever sets it.

    Do not branch callers on which of the two no-choice cases applies. If a
    future caller needs that distinction, add a separate reader rather than
    changing this one's return type.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT analytics_consent_updated_at IS NOT NULL AS choice_made FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    return bool(row and row["choice_made"])


def record_consent(
    conn: Any,
    *,
    user_id: str,
    consent_type: str,
    granted: bool,
    consent_version: str,
    consented_at: str,
) -> None:
    """Update the current-consent column AND insert the consent_recorded
    audit event against the SAME connection, so both writes land in one
    transaction under the caller's commit boundary (mirrors _companies.py /
    _jobs.py: no commit here — the caller wraps this in connection_factory()
    + pool.commit_unless_nested(), or a test's rollback-isolated db_conn).

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
    raw.execute(
        "UPDATE users SET analytics_consent = %s, analytics_consent_updated_at = now() "
        "WHERE id = %s",
        (granted, user_id),
    )
    insert_event(raw, event_type="consent_recorded", user_id=user_id, payload=payload)


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

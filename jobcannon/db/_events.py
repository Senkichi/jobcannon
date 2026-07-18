"""insert_event / read_consent_state / record_consent — the single sanctioned
events-table writer (1B Wave 2 PR 8).

No other module may write to `events` or `users.analytics_consent` with raw
SQL — tests/host/test_events_single_writer.py AST-scans jobcannon/host and
jobcannon/web for "insert into events" / "update events" literals and fails
the build if any turn up outside this file. jobcannon/host/events.py
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
    + pool.commit_unless_nested(), or a test's rollback-isolated db_conn)."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "UPDATE users SET analytics_consent = %s, analytics_consent_updated_at = now() "
        "WHERE id = %s",
        (granted, user_id),
    )
    insert_event(
        raw,
        event_type="consent_recorded",
        user_id=user_id,
        payload={
            "consent_type": consent_type,
            "granted": granted,
            "consent_version": consent_version,
            "consented_at": consented_at,
        },
    )

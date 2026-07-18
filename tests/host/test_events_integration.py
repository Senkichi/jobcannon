"""Postgres-backed integration coverage for jobcannon.db._events and
jobcannon.host.posthog_client (1B Wave 2 PR 8): interleave_team CHECK
enforcement, anonymous (user_id=None) inserts, record_consent's
one-transaction contract, and posthog_client's no-op-when-unwired behavior.

Unit coverage (validate_payload, the consent gate, write-then-fan-out
ordering, PostHog failure isolation) lives in tests/host/test_events.py,
which needs no Postgres at all.
"""

from __future__ import annotations

import psycopg
import pytest

from jobcannon.db import _events
from jobcannon.host import posthog_client
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def test_interleave_team_accepts_a_b_and_null(db_conn):
    _events.insert_event(
        db_conn, event_type="posting_impression", user_id=None, interleave_team="A"
    )
    _events.insert_event(
        db_conn, event_type="posting_impression", user_id=None, interleave_team="B"
    )
    _events.insert_event(
        db_conn, event_type="posting_impression", user_id=None, interleave_team=None
    )
    n = db_conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
    assert n == 3


def test_interleave_team_rejects_values_outside_a_b(db_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        _events.insert_event(
            db_conn, event_type="posting_impression", user_id=None, interleave_team="C"
        )


def test_anonymous_user_id_insert_succeeds(db_conn):
    _events.insert_event(db_conn, event_type="posting_impression", user_id=None)
    row = db_conn.execute("SELECT user_id FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["user_id"] is None


def test_record_consent_writes_column_and_audit_event_in_one_transaction(db_conn):
    user_id = "user_consent_1"
    db_conn.execute("INSERT INTO users (id, email) VALUES (%s, 'a@example.org')", (user_id,))

    _events.record_consent(
        db_conn,
        user_id=user_id,
        consent_type="analytics",
        granted=True,
        consent_version="v1",
        consented_at="2026-07-17T00:00:00Z",
    )

    user_row = db_conn.execute(
        "SELECT analytics_consent, analytics_consent_updated_at FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    assert user_row["analytics_consent"] is True
    assert user_row["analytics_consent_updated_at"] is not None

    event_row = db_conn.execute(
        "SELECT payload FROM events WHERE user_id = %s AND event_type = 'consent_recorded'",
        (user_id,),
    ).fetchone()
    assert event_row is not None
    assert event_row["payload"] == {
        "consent_type": "analytics",
        "granted": True,
        "consent_version": "v1",
        "consented_at": "2026-07-17T00:00:00Z",
    }


def test_record_consent_decline_still_writes_audit_event(db_conn):
    user_id = "user_consent_decline"
    db_conn.execute("INSERT INTO users (id, email) VALUES (%s, 'b@example.org')", (user_id,))

    _events.record_consent(
        db_conn,
        user_id=user_id,
        consent_type="analytics",
        granted=False,
        consent_version="v1",
        consented_at="2026-07-17T00:00:00Z",
    )

    user_row = db_conn.execute(
        "SELECT analytics_consent FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    assert user_row["analytics_consent"] is False

    n = db_conn.execute(
        "SELECT count(*) AS n FROM events WHERE user_id = %s AND event_type = 'consent_recorded'",
        (user_id,),
    ).fetchone()["n"]
    assert n == 1


def test_read_consent_state_reflects_column(db_conn):
    user_id = "user_read_consent"
    db_conn.execute(
        "INSERT INTO users (id, email, analytics_consent) VALUES (%s, 'c@example.org', true)",
        (user_id,),
    )
    assert _events.read_consent_state(db_conn, user_id) is True


def test_read_consent_state_false_for_unknown_user(db_conn):
    assert _events.read_consent_state(db_conn, "no_such_user") is False


def test_set_posthog_client_none_is_pure_noop():
    posthog_client.set_posthog_client(None)
    posthog_client.capture("user_1", "posting_saved", {})  # must not raise

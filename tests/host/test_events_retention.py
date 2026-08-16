"""jobcannon.db._events.delete_expired_events (events-retention issue) plus
the periodic task wiring (no DB — connection_factory and the DAL function
are mocked), mirroring tests/host/test_anon_reap.py's split between real-
Postgres predicate coverage and connector-free wiring coverage.

Coverage includes both members of events_schema.DURABLE_EVENT_TYPES
(consent_recorded, user_signed_up), not just consent_recorded — reaping
user_signed_up would silently re-open the re-emission gap
has_signed_up_event exists to close (jobcannon/db/_events.py)."""

from __future__ import annotations

import contextlib

from tests.host.conftest import requires_postgres

_RETENTION_DAYS = 365


def _backdate_event(conn, event_id: int, *, days: int) -> None:
    conn.execute(
        "UPDATE events SET occurred_at = now() - make_interval(days => %s) WHERE id = %s",
        (days, event_id),
    )


def _insert_user(conn, user_id: str) -> None:
    conn.execute(
        "INSERT INTO users (id, email) VALUES (%s, %s)", (user_id, f"{user_id}@example.org")
    )


def _insert_event(conn, *, user_id: str | None, event_type: str = "posting_saved") -> int:
    return conn.execute(
        "INSERT INTO events (user_id, event_type) VALUES (%s, %s) RETURNING id",
        (user_id, event_type),
    ).fetchone()["id"]


@requires_postgres
def test_delete_expired_events_reaps_an_old_non_consent_row(db_conn):
    from jobcannon.db._events import delete_expired_events

    user_id = "user_2retentionOld"
    _insert_user(db_conn, user_id)
    old_event = _insert_event(db_conn, user_id=user_id)
    _backdate_event(db_conn, old_event, days=_RETENTION_DAYS + 10)

    reaped = delete_expired_events(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == [old_event]
    row = db_conn.execute("SELECT id FROM events WHERE id = %s", (old_event,)).fetchone()
    assert row is None


@requires_postgres
def test_delete_expired_events_keeps_a_fresh_event(db_conn):
    from jobcannon.db._events import delete_expired_events

    user_id = "user_2retentionFresh"
    _insert_user(db_conn, user_id)
    fresh_event = _insert_event(db_conn, user_id=user_id)  # occurred_at defaults to now()

    reaped = delete_expired_events(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == []
    row = db_conn.execute("SELECT id FROM events WHERE id = %s", (fresh_event,)).fetchone()
    assert row is not None


@requires_postgres
def test_delete_expired_events_leaves_anon_user_events_to_the_anon_reaper(db_conn):
    """An old event for an anon-namespaced user id is excluded here even
    though it clears the age predicate — it's left for reap_unconverted_anon_users
    to remove via the cascade off the parent `users` row, not trimmed here."""
    from jobcannon.db._events import delete_expired_events
    from jobcannon.db._users import mint_anon_user

    anon_id = mint_anon_user(db_conn)
    old_event = _insert_event(db_conn, user_id=anon_id)
    _backdate_event(db_conn, old_event, days=_RETENTION_DAYS + 10)

    reaped = delete_expired_events(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == []
    row = db_conn.execute("SELECT id FROM events WHERE id = %s", (old_event,)).fetchone()
    assert row is not None


@requires_postgres
def test_delete_expired_events_leaves_a_null_user_id_row_alone(db_conn):
    """insert_event(user_id=None) is a supported, tested path (a
    pre-auth/anonymous-session impression) -- it has no parent `users` row
    for reap_unconverted_anon_users to cascade from, so nothing else ever
    revisits it either. This reaper's `NOT LIKE` predicate already excludes
    NULL by SQL three-valued-logic; this test pins that as intended
    behavior rather than an accident of the WHERE clause."""
    from jobcannon.db._events import delete_expired_events

    old_anonymous_event = _insert_event(db_conn, user_id=None)
    _backdate_event(db_conn, old_anonymous_event, days=_RETENTION_DAYS + 10)

    reaped = delete_expired_events(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == []
    row = db_conn.execute("SELECT id FROM events WHERE id = %s", (old_anonymous_event,)).fetchone()
    assert row is not None


@requires_postgres
def test_delete_expired_events_never_touches_consent_recorded(db_conn):
    """consent_recorded is the audit trail of a consent decision and must
    survive independent of age -- explicitly excluded regardless of how old
    it is."""
    from jobcannon.db._events import delete_expired_events

    user_id = "user_2retentionConsent"
    _insert_user(db_conn, user_id)
    old_consent_event = _insert_event(db_conn, user_id=user_id, event_type="consent_recorded")
    _backdate_event(db_conn, old_consent_event, days=_RETENTION_DAYS + 3650)

    reaped = delete_expired_events(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == []
    row = db_conn.execute("SELECT id FROM events WHERE id = %s", (old_consent_event,)).fetchone()
    assert row is not None


@requires_postgres
def test_delete_expired_events_never_touches_user_signed_up(db_conn):
    """user_signed_up is signup attribution that has_signed_up_event durably
    keys off of to avoid re-emitting on a cleared cookie jar (see
    jobcannon/web/handoff.py) -- it must survive independent of age exactly
    like consent_recorded, not just the one literal event_type."""
    from jobcannon.db._events import delete_expired_events

    user_id = "user_2retentionSignup"
    _insert_user(db_conn, user_id)
    old_signup_event = _insert_event(db_conn, user_id=user_id, event_type="user_signed_up")
    _backdate_event(db_conn, old_signup_event, days=_RETENTION_DAYS + 3650)

    reaped = delete_expired_events(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == []
    row = db_conn.execute("SELECT id FROM events WHERE id = %s", (old_signup_event,)).fetchone()
    assert row is not None


@requires_postgres
def test_delete_expired_events_only_reaps_the_old_non_durable_row(db_conn):
    """One connection, five rows, each clearing exactly one predicate: old +
    non-durable + authed (reaped), fresh (kept), old anon (kept, left to the
    anon reaper), old consent_recorded (kept, audit trail), old
    user_signed_up (kept, durable signup attribution)."""
    from jobcannon.db._events import delete_expired_events
    from jobcannon.db._users import mint_anon_user

    user_id = "user_2retentionMixed"
    _insert_user(db_conn, user_id)

    old_authed = _insert_event(db_conn, user_id=user_id)
    _backdate_event(db_conn, old_authed, days=_RETENTION_DAYS + 10)

    fresh_authed = _insert_event(db_conn, user_id=user_id)

    anon_id = mint_anon_user(db_conn)
    old_anon = _insert_event(db_conn, user_id=anon_id)
    _backdate_event(db_conn, old_anon, days=_RETENTION_DAYS + 10)

    old_consent = _insert_event(db_conn, user_id=user_id, event_type="consent_recorded")
    _backdate_event(db_conn, old_consent, days=_RETENTION_DAYS + 10)

    old_signup = _insert_event(db_conn, user_id=user_id, event_type="user_signed_up")
    _backdate_event(db_conn, old_signup, days=_RETENTION_DAYS + 10)

    reaped = delete_expired_events(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == [old_authed]
    remaining = {
        row["id"]
        for row in db_conn.execute(
            "SELECT id FROM events WHERE id IN (%s, %s, %s, %s, %s)",
            (old_authed, fresh_authed, old_anon, old_consent, old_signup),
        ).fetchall()
    }
    assert remaining == {fresh_authed, old_anon, old_consent, old_signup}


def test_reap_old_events_task_reports_a_count_not_ids(monkeypatch):
    """No Postgres needed: delete_expired_events and connection_factory are
    both seams -- proves the periodic task's wiring (open conn -> delete ->
    summarize) and that raw event ids never leave the task result."""
    from jobcannon.host import tasks

    monkeypatch.delenv("JC_EVENTS_RETENTION_DAYS", raising=False)
    monkeypatch.setattr(
        tasks, "delete_expired_events", lambda conn, *, retention_days: [101, 102, 103]
    )

    @contextlib.contextmanager
    def _fake_connection_factory():
        yield object()

    monkeypatch.setattr("jobcannon.db.connection_factory", _fake_connection_factory)

    result = tasks.reap_old_events(0)

    assert result == {"reaped": 3, "retention_days": tasks.DEFAULT_EVENTS_RETENTION_DAYS}


def test_reap_old_events_task_defaults_to_365_days(monkeypatch):
    from jobcannon.host import tasks

    monkeypatch.delenv("JC_EVENTS_RETENTION_DAYS", raising=False)
    captured = {}

    def _fake_delete(conn, *, retention_days):
        captured["retention_days"] = retention_days
        return []

    monkeypatch.setattr(tasks, "delete_expired_events", _fake_delete)

    @contextlib.contextmanager
    def _fake_connection_factory():
        yield object()

    monkeypatch.setattr("jobcannon.db.connection_factory", _fake_connection_factory)

    result = tasks.reap_old_events(0)

    assert captured["retention_days"] == 365
    assert result == {"reaped": 0, "retention_days": 365}


def test_reap_old_events_task_honors_retention_env_override(monkeypatch):
    from jobcannon.host import tasks

    monkeypatch.setenv("JC_EVENTS_RETENTION_DAYS", "90")
    captured = {}

    def _fake_delete(conn, *, retention_days):
        captured["retention_days"] = retention_days
        return []

    monkeypatch.setattr(tasks, "delete_expired_events", _fake_delete)

    @contextlib.contextmanager
    def _fake_connection_factory():
        yield object()

    monkeypatch.setattr("jobcannon.db.connection_factory", _fake_connection_factory)

    result = tasks.reap_old_events(0)

    assert captured["retention_days"] == 90
    assert result == {"reaped": 0, "retention_days": 90}

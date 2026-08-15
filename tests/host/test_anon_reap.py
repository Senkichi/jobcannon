"""jobcannon.db._users.reap_unconverted_anon_users (#48) plus the periodic
task wiring (no DB — connection_factory and the DAL function are mocked),
mirroring tests/host/test_storage_check.py's split between real-Postgres
predicate coverage and connector-free wiring coverage."""

from __future__ import annotations

import contextlib

from tests.host.conftest import requires_postgres

_RETENTION_DAYS = 30


def _backdate(conn, user_id: str, *, days: int) -> None:
    conn.execute(
        "UPDATE users SET created_at = now() - make_interval(days => %s) WHERE id = %s",
        (days, user_id),
    )


@requires_postgres
def test_reap_deletes_only_the_old_unconverted_anon_row(db_conn):
    """Three rows, one predicate each has to clear: an old anon row (past
    the window, never converted -> reaped), a fresh anon row (same
    namespace, inside the window -> kept), and an old Clerk-style row
    (past the window too, so only the id-namespace check saves it, not
    freshness -> kept)."""
    from jobcannon.db._users import ensure_user, mint_anon_user, reap_unconverted_anon_users

    old_anon = mint_anon_user(db_conn)
    _backdate(db_conn, old_anon, days=_RETENTION_DAYS + 10)

    fresh_anon = mint_anon_user(db_conn)  # created_at defaults to now()

    converted = "user_2abcXYZ"  # Clerk-issued shape: never carries ANON_ID_PREFIX
    ensure_user(db_conn, converted, email="real@example.org")
    _backdate(db_conn, converted, days=_RETENTION_DAYS + 10)

    reaped = reap_unconverted_anon_users(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == [old_anon]
    remaining = {
        row["id"]
        for row in db_conn.execute(
            "SELECT id FROM users WHERE id IN (%s, %s, %s)",
            (old_anon, fresh_anon, converted),
        ).fetchall()
    }
    assert remaining == {fresh_anon, converted}


@requires_postgres
def test_reap_cascades_the_profile_of_a_reaped_row(db_conn):
    from jobcannon.db._profiles import get_profile, upsert_profile
    from jobcannon.db._users import mint_anon_user, reap_unconverted_anon_users

    anon_id = mint_anon_user(db_conn)
    upsert_profile(db_conn, anon_id, skills=["sql"])
    _backdate(db_conn, anon_id, days=_RETENTION_DAYS + 10)

    reaped = reap_unconverted_anon_users(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == [anon_id]
    assert get_profile(db_conn, anon_id) is None


@requires_postgres
def test_reap_skips_an_old_anon_row_that_somehow_has_events(db_conn):
    """Anon ids should never reach an authenticated-only route (jobcannon/
    web/__init__.py's PUBLIC_PATHS gate), so an events row for one should
    never exist — but the guard must fail SAFE (skip the row) rather than
    silently cascading real activity away if that invariant is ever broken."""
    from jobcannon.db._users import mint_anon_user, reap_unconverted_anon_users

    anon_id = mint_anon_user(db_conn)
    _backdate(db_conn, anon_id, days=_RETENTION_DAYS + 10)
    db_conn.execute(
        "INSERT INTO events (user_id, event_type) VALUES (%s, 'test_event')", (anon_id,)
    )

    reaped = reap_unconverted_anon_users(db_conn, retention_days=_RETENTION_DAYS)

    assert reaped == []
    row = db_conn.execute("SELECT id FROM users WHERE id = %s", (anon_id,)).fetchone()
    assert row is not None


def test_reap_anon_users_task_reports_a_count_not_ids(monkeypatch):
    """No Postgres needed: reap_unconverted_anon_users and connection_factory
    are both seams — this proves the periodic task's wiring (open conn ->
    reap -> summarize) and that raw ids never leave the task result."""
    from jobcannon.host import tasks

    monkeypatch.setattr(
        tasks, "reap_unconverted_anon_users", lambda conn, *, retention_days: ["anon_a", "anon_b"]
    )

    @contextlib.contextmanager
    def _fake_connection_factory():
        yield object()

    monkeypatch.setattr("jobcannon.db.connection_factory", _fake_connection_factory)

    result = tasks.reap_anon_users(0)

    assert result == {"reaped": 2, "retention_days": _RETENTION_DAYS}


def test_reap_anon_users_task_honors_retention_env_override(monkeypatch):
    from jobcannon.host import tasks

    monkeypatch.setenv("JC_ANON_RETENTION_DAYS", "7")
    captured = {}

    def _fake_reap(conn, *, retention_days):
        captured["retention_days"] = retention_days
        return []

    monkeypatch.setattr(tasks, "reap_unconverted_anon_users", _fake_reap)

    @contextlib.contextmanager
    def _fake_connection_factory():
        yield object()

    monkeypatch.setattr("jobcannon.db.connection_factory", _fake_connection_factory)

    result = tasks.reap_anon_users(0)

    assert captured["retention_days"] == 7
    assert result == {"reaped": 0, "retention_days": 7}

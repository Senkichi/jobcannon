"""jobcannon.host.user_deletion — the single user-deletion cascade path
(issues #135, #136) plus the reconciliation-sweep periodic's DB half.

Any test below that sets the analytics pseudonym salt and lets
cascade_delete_user actually run MUST wrap the call in
`task_app.app.replace_connector(testing.InMemoryConnector())` — without it,
a real `.defer()` call would hit whatever DATABASE_URL happens to be in the
environment, and this module's own try/except around the defer call would
silently swallow the result, letting the test pass while writing somewhere
unintended."""

from __future__ import annotations

import contextlib

import httpx
from clerk_backend_api import models
from procrastinate import testing

from tests.host.conftest import requires_postgres

_SETTLE_DAYS = 3


def _backdate_created(conn, user_id: str, *, days: int) -> None:
    conn.execute(
        "UPDATE users SET created_at = now() - make_interval(days => %s) WHERE id = %s",
        (days, user_id),
    )


def _backdate_checked(conn, user_id: str, *, days: int) -> None:
    conn.execute(
        "UPDATE users SET deletion_checked_at = now() - make_interval(days => %s) WHERE id = %s",
        (days, user_id),
    )


@contextlib.contextmanager
def _factory_for(conn):
    """A `connection_factory`-shaped callable that always yields the SAME
    already-open connection — mirrors the ambient-transaction db_conn test
    fixture, since run_reconciliation_sweep opens several short-lived
    "connections" that must all land in one rollback-isolated transaction
    for the test to clean up after itself."""
    yield conn


def _clerk_errors(status_code: int) -> models.ClerkErrors:
    req = httpx.Request("GET", "https://api.clerk.com/v1/users/x")
    resp = httpx.Response(status_code=status_code, request=req, json={"errors": []})
    return models.ClerkErrors(data=models.ClerkErrorsData(errors=[]), raw_response=resp)


def _sdk_error() -> models.SDKError:
    req = httpx.Request("GET", "https://api.clerk.com/v1/users/x")
    resp = httpx.Response(status_code=500, request=req, text="boom")
    return models.SDKError("boom", raw_response=resp)


# --- _lookup_clerk_user: pure outcome-classification, no DB/network -------


class _ScriptedUsers:
    """Fake Clerk `users` resource: `outcomes[user_id]` is either None
    (present) or an exception instance to raise."""

    def __init__(self, outcomes: dict):
        self.outcomes = outcomes
        self.calls: list[str] = []

    def get(self, *, user_id):
        self.calls.append(user_id)
        outcome = self.outcomes[user_id]
        if outcome is not None:
            raise outcome
        return object()


def test_lookup_clerk_user_present():
    from jobcannon.host.user_deletion import _lookup_clerk_user

    users = _ScriptedUsers({"u1": None})
    assert _lookup_clerk_user(users, "u1") == "present"


def test_lookup_clerk_user_definitive_404_is_not_found():
    from jobcannon.host.user_deletion import _lookup_clerk_user

    users = _ScriptedUsers({"u1": _clerk_errors(404)})
    assert _lookup_clerk_user(users, "u1") == "not_found"


def test_lookup_clerk_user_non_404_clerk_error_is_error():
    from jobcannon.host.user_deletion import _lookup_clerk_user

    users = _ScriptedUsers({"u1": _clerk_errors(401)})
    assert _lookup_clerk_user(users, "u1") == "error"


def test_lookup_clerk_user_sdk_error_is_error():
    from jobcannon.host.user_deletion import _lookup_clerk_user

    users = _ScriptedUsers({"u1": _sdk_error()})
    assert _lookup_clerk_user(users, "u1") == "error"


def test_lookup_clerk_user_unexpected_exception_is_error():
    from jobcannon.host.user_deletion import _lookup_clerk_user

    users = _ScriptedUsers({"u1": RuntimeError("network blip")})
    assert _lookup_clerk_user(users, "u1") == "error"


# --- cascade_delete_user: single deletion path -----------------------------


@requires_postgres
def test_cascade_delete_user_deletes_row_without_salt(db_conn):
    from jobcannon.db._users import ensure_user
    from jobcannon.host.user_deletion import cascade_delete_user

    user_id = "user_cascade_1"
    ensure_user(db_conn, user_id, email="x@example.org")

    cascade_delete_user(db_conn, user_id)

    row = db_conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone()
    assert row is None


@requires_postgres
def test_cascade_delete_user_enqueues_purge_with_pseudonym_not_raw_id(db_conn):
    from jobcannon.db._users import ensure_user
    from jobcannon.host import posthog_client, task_app
    from jobcannon.host.user_deletion import PURGE_POSTHOG_PERSON_TASK, cascade_delete_user

    posthog_client.set_analytics_salt("cascade-test-salt")
    try:
        user_id = "user_cascade_2"
        ensure_user(db_conn, user_id, email="x@example.org")
        expected_pseudonym = posthog_client.pseudonymize(user_id)

        with task_app.app.replace_connector(testing.InMemoryConnector()) as app:
            cascade_delete_user(db_conn, user_id)
            jobs = list(app.connector.jobs.values())

        assert len(jobs) == 1
        job = jobs[0]
        assert job["task_name"] == PURGE_POSTHOG_PERSON_TASK
        assert job["queue_name"] == "maintenance"
        assert job["args"]["distinct_id"] == expected_pseudonym
        assert job["args"]["distinct_id"] != user_id
    finally:
        posthog_client.set_analytics_salt(None)


@requires_postgres
def test_cascade_delete_user_skips_enqueue_without_salt(db_conn):
    from jobcannon.db._users import ensure_user
    from jobcannon.host import task_app
    from jobcannon.host.user_deletion import cascade_delete_user

    user_id = "user_cascade_3"
    ensure_user(db_conn, user_id, email="x@example.org")

    with task_app.app.replace_connector(testing.InMemoryConnector()) as app:
        cascade_delete_user(db_conn, user_id)
        jobs = list(app.connector.jobs.values())

    assert jobs == []


def test_purge_task_name_constant_matches_a_registered_worker_task():
    """L3-wiring guard: PURGE_POSTHOG_PERSON_TASK is a string, deferred by
    name without ever importing jobcannon.host.tasks from the web process
    (see task_app.py's docstring) -- a typo here would defer jobs no worker
    would ever execute, silently. This proves the string actually matches a
    task jobcannon.host.tasks (imported here, a test-only concession) has
    registered."""
    from jobcannon.host import tasks
    from jobcannon.host.user_deletion import PURGE_POSTHOG_PERSON_TASK

    assert PURGE_POSTHOG_PERSON_TASK in tasks.app.tasks
    assert tasks.app.tasks[PURGE_POSTHOG_PERSON_TASK].name == PURGE_POSTHOG_PERSON_TASK


# --- DAL: list_users_pending_deletion_reconciliation / mark_deletion_checked


@requires_postgres
def test_list_pending_reconciliation_respects_settle_days_and_anon_exclusion(db_conn):
    from jobcannon.db._users import (
        ensure_user,
        list_users_pending_deletion_reconciliation,
        mint_anon_user,
    )

    old_enough = "user_old_enough"
    ensure_user(db_conn, old_enough, email="a@example.org")
    _backdate_created(db_conn, old_enough, days=_SETTLE_DAYS + 1)

    too_fresh = "user_too_fresh"
    ensure_user(db_conn, too_fresh, email="b@example.org")  # created_at defaults to now()

    old_anon = mint_anon_user(db_conn)
    _backdate_created(db_conn, old_anon, days=_SETTLE_DAYS + 1)

    candidates = list_users_pending_deletion_reconciliation(
        db_conn, settle_days=_SETTLE_DAYS, limit=50
    )

    assert old_enough in candidates
    assert too_fresh not in candidates
    assert old_anon not in candidates


@requires_postgres
def test_list_pending_reconciliation_rotates_never_checked_first_then_oldest_checked(db_conn):
    """The regression this pins: an ordering by created_at alone would
    return the SAME oldest `limit` rows forever, since rows only leave that
    ordering by being deleted (rare). deletion_checked_at NULLS FIRST, then
    oldest-checked-first, means a row that was confirmed present eventually
    rotates back to the front instead of being permanently unreachable."""
    from jobcannon.db._users import ensure_user, list_users_pending_deletion_reconciliation

    never_checked = "user_never_checked"
    checked_long_ago = "user_checked_long_ago"
    checked_recently = "user_checked_recently"
    for uid in (never_checked, checked_long_ago, checked_recently):
        ensure_user(db_conn, uid, email=f"{uid}@example.org")
        _backdate_created(db_conn, uid, days=_SETTLE_DAYS + 10)
    _backdate_checked(db_conn, checked_long_ago, days=2)
    _backdate_checked(db_conn, checked_recently, days=1)

    candidates = list_users_pending_deletion_reconciliation(
        db_conn, settle_days=_SETTLE_DAYS, limit=2
    )

    assert candidates == [never_checked, checked_long_ago]


@requires_postgres
def test_mark_deletion_checked_stamps_timestamp(db_conn):
    from jobcannon.db._users import ensure_user, mark_deletion_checked

    user_id = "user_mark_checked"
    ensure_user(db_conn, user_id, email="x@example.org")
    before = db_conn.execute(
        "SELECT deletion_checked_at FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    assert before["deletion_checked_at"] is None

    mark_deletion_checked(db_conn, user_id)

    after = db_conn.execute(
        "SELECT deletion_checked_at FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    assert after["deletion_checked_at"] is not None


# --- run_reconciliation_sweep: orchestration -------------------------------


@requires_postgres
def test_run_reconciliation_sweep_deletes_stamps_and_counts_errors(db_conn):
    from jobcannon.db._users import ensure_user
    from jobcannon.host.user_deletion import run_reconciliation_sweep

    gone = "user_sweep_gone"
    present = "user_sweep_present"
    erroring = "user_sweep_erroring"
    for uid in (gone, present, erroring):
        ensure_user(db_conn, uid, email=f"{uid}@example.org")
        _backdate_created(db_conn, uid, days=_SETTLE_DAYS + 1)

    fake_users = _ScriptedUsers({gone: _clerk_errors(404), present: None, erroring: _sdk_error()})
    sleeps: list[float] = []

    result = run_reconciliation_sweep(
        lambda: _factory_for(db_conn),
        fake_users,
        settle_days=_SETTLE_DAYS,
        row_cap=50,
        sleep_fn=sleeps.append,
    )

    assert result == {"checked": 3, "deleted": 1, "confirmed_present": 1, "errors": 1}
    # Paced between rows, never before the first lookup.
    assert len(sleeps) == 2

    remaining = {
        row["id"]
        for row in db_conn.execute(
            "SELECT id FROM users WHERE id IN (%s, %s, %s)", (gone, present, erroring)
        ).fetchall()
    }
    assert remaining == {present, erroring}

    present_row = db_conn.execute(
        "SELECT deletion_checked_at FROM users WHERE id = %s", (present,)
    ).fetchone()
    assert present_row["deletion_checked_at"] is not None

    erroring_row = db_conn.execute(
        "SELECT deletion_checked_at FROM users WHERE id = %s", (erroring,)
    ).fetchone()
    assert erroring_row["deletion_checked_at"] is None  # never stamped -> retried next sweep


@requires_postgres
def test_run_reconciliation_sweep_never_issues_a_clerk_delete(db_conn):
    """#136's design constraint: the sweep only ever READS Clerk. _ScriptedUsers
    has no `delete` method at all -- if the sweep ever called one, this test
    would fail with AttributeError instead of silently passing."""
    from jobcannon.db._users import ensure_user
    from jobcannon.host.user_deletion import run_reconciliation_sweep

    user_id = "user_sweep_no_delete_call"
    ensure_user(db_conn, user_id, email="x@example.org")
    _backdate_created(db_conn, user_id, days=_SETTLE_DAYS + 1)

    fake_users = _ScriptedUsers({user_id: _clerk_errors(404)})
    assert not hasattr(fake_users, "delete")

    result = run_reconciliation_sweep(
        lambda: _factory_for(db_conn),
        fake_users,
        settle_days=_SETTLE_DAYS,
        row_cap=50,
        sleep_fn=lambda s: None,
    )
    assert result["deleted"] == 1


@requires_postgres
def test_run_reconciliation_sweep_routes_deletion_through_cascade_delete_user(db_conn):
    """A 404 outcome must go through cascade_delete_user (the ONE deletion
    path), not a second inline DELETE -- proven observably: with the
    analytics salt configured, a confirmed-deleted row must ALSO enqueue
    the PostHog purge job, exactly like the webhook path does."""
    from jobcannon.db._users import ensure_user
    from jobcannon.host import posthog_client, task_app
    from jobcannon.host.user_deletion import PURGE_POSTHOG_PERSON_TASK, run_reconciliation_sweep

    posthog_client.set_analytics_salt("sweep-cascade-salt")
    try:
        user_id = "user_sweep_cascade"
        ensure_user(db_conn, user_id, email="x@example.org")
        _backdate_created(db_conn, user_id, days=_SETTLE_DAYS + 1)
        expected_pseudonym = posthog_client.pseudonymize(user_id)

        fake_users = _ScriptedUsers({user_id: _clerk_errors(404)})
        with task_app.app.replace_connector(testing.InMemoryConnector()) as app:
            run_reconciliation_sweep(
                lambda: _factory_for(db_conn),
                fake_users,
                settle_days=_SETTLE_DAYS,
                row_cap=50,
                sleep_fn=lambda s: None,
            )
            jobs = list(app.connector.jobs.values())

        purge_jobs = [j for j in jobs if j["task_name"] == PURGE_POSTHOG_PERSON_TASK]
        assert len(purge_jobs) == 1
        assert purge_jobs[0]["args"]["distinct_id"] == expected_pseudonym
    finally:
        posthog_client.set_analytics_salt(None)


# --- reconcile_deleted_users periodic: wiring only, no DB/network ----------


def test_reconcile_deleted_users_reports_clerk_unreachable_when_key_unset(monkeypatch):
    from jobcannon.host import tasks

    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)

    result = tasks.reconcile_deleted_users(0)

    assert result == {"status": "clerk_unreachable", "checked": 0}


def test_reconcile_deleted_users_wires_settle_days_and_row_cap_from_env(monkeypatch):
    from jobcannon.host import tasks
    from jobcannon.host import user_deletion as user_deletion_mod

    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("JC_DELETION_RECONCILE_SETTLE_DAYS", "9")
    monkeypatch.setenv("JC_DELETION_RECONCILE_ROW_CAP", "7")

    captured = {}

    def _fake_sweep(connection_factory, clerk_users, *, settle_days, row_cap, **kwargs):
        captured["settle_days"] = settle_days
        captured["row_cap"] = row_cap
        return {"checked": 0, "deleted": 0, "confirmed_present": 0, "errors": 0}

    monkeypatch.setattr(user_deletion_mod, "run_reconciliation_sweep", _fake_sweep)

    class _FakeUsers:
        pass

    class _FakeClerkClient:
        users = _FakeUsers()

    import jobcannon.web.auth as auth_mod

    monkeypatch.setattr(auth_mod, "build_clerk_client", lambda host_config: _FakeClerkClient())

    result = tasks.reconcile_deleted_users(0)

    assert captured == {"settle_days": 9, "row_cap": 7}
    assert result["status"] == "ok"
    assert result["checked"] == 0

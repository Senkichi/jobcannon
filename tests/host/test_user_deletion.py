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
import logging

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


def test_lookup_clerk_user_429_is_rate_limited():
    """Issue #136 'rate-limit-aware': a 429 must be distinguishable from a
    generic error so the sweep can back off/abort instead of continuing to
    hammer an already-rate-limited Clerk row by row."""
    from jobcannon.host.user_deletion import _lookup_clerk_user

    users = _ScriptedUsers({"u1": _clerk_errors(429)})
    assert _lookup_clerk_user(users, "u1") == "rate_limited"


def test_lookup_clerk_user_never_logs_full_traceback(caplog):
    """F4 / LEAD-6: mirrors jobcannon.web.auth._clerk_failure_reason's
    documented invariant -- log only the status code / exception class
    name, never `logger.exception` (full traceback), so nothing from the
    request (a Clerk user id embedded in a request URL, in particular) can
    surface in a log line."""
    from jobcannon.host.user_deletion import _lookup_clerk_user

    users = _ScriptedUsers({"u1": _sdk_error()})
    with caplog.at_level(logging.WARNING, logger="jobcannon.host.user_deletion"):
        _lookup_clerk_user(users, "u1")

    assert caplog.records
    for record in caplog.records:
        assert record.levelno == logging.WARNING
        assert record.exc_info is None  # logger.exception would set this


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
def test_list_pending_reconciliation_excludes_guest_demo_sentinel(db_conn):
    """HIGH-2 / F1: `guest_demo` is a real, seeded, non-anon `users` row
    (`jobcannon.db._profiles.GUEST_USER_ID`) that is NOT Clerk-issued. The
    previous denylist (`NOT LIKE 'anon\\_%'`) let it reach the Clerk lookup
    once aged past settle_days -- a definitive 404 there would hard-delete
    it and break `/demo`. The fix is a positive allowlist (Clerk ids are
    always `user_`-prefixed)."""
    from jobcannon.db._profiles import GUEST_USER_ID
    from jobcannon.db._users import ensure_user, list_users_pending_deletion_reconciliation

    ensure_user(db_conn, GUEST_USER_ID, email=None)
    _backdate_created(db_conn, GUEST_USER_ID, days=_SETTLE_DAYS + 1)

    candidates = list_users_pending_deletion_reconciliation(
        db_conn, settle_days=_SETTLE_DAYS, limit=50
    )

    assert GUEST_USER_ID not in candidates


@requires_postgres
def test_run_reconciliation_sweep_never_looks_up_guest_demo(db_conn):
    """Stronger signal than the DAL-level exclusion test above: proves the
    sweep never even REACHES the Clerk lookup for guest_demo, not just that
    it isn't deleted. `_ScriptedUsers.get` raises `KeyError` on an id with
    no scripted outcome, so if the allowlist regressed and guest_demo
    reached the lookup, this test fails loudly with `KeyError` rather than
    silently passing."""
    from jobcannon.db._profiles import GUEST_USER_ID
    from jobcannon.db._users import ensure_user
    from jobcannon.host.user_deletion import run_reconciliation_sweep

    ensure_user(db_conn, GUEST_USER_ID, email=None)
    _backdate_created(db_conn, GUEST_USER_ID, days=_SETTLE_DAYS + 1)

    real_user = "user_alongside_guest_demo"
    ensure_user(db_conn, real_user, email="x@example.org")
    _backdate_created(db_conn, real_user, days=_SETTLE_DAYS + 1)

    fake_users = _ScriptedUsers({real_user: None})  # no scripted outcome for GUEST_USER_ID
    result = run_reconciliation_sweep(
        lambda: _factory_for(db_conn),
        fake_users,
        settle_days=_SETTLE_DAYS,
        row_cap=50,
        sleep_fn=lambda s: None,
    )

    assert GUEST_USER_ID not in fake_users.calls
    assert result["confirmed_present"] == 1


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

    assert result == {
        "status": "ok",
        "checked": 3,
        "deleted": 1,
        "confirmed_present": 1,
        "errors": 1,
    }
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
    # MEDIUM-5 / FINDING-4: now stamped even on error, so it rotates to the
    # BACK of the next sweep's candidate window instead of parking itself at
    # the front (NULLS FIRST) forever and starving out never-checked rows.
    assert erroring_row["deletion_checked_at"] is not None


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


# --- circuit breaker (HIGH-3) ------------------------------------------


@requires_postgres
def test_run_reconciliation_sweep_circuit_breaker_trips_on_all_404(db_conn):
    """HIGH-3: a valid CLERK_SECRET_KEY for the WRONG Clerk instance
    authenticates fine (no 401) but 404s for every real user -- without
    this guard, one sweep would hard-delete the whole checked cohort.
    Seeds exactly `_CIRCUIT_BREAKER_MIN_CHECKED` rows (the breaker's floor)
    all scripted 404."""
    from jobcannon.db._users import ensure_user
    from jobcannon.host.user_deletion import run_reconciliation_sweep

    user_ids = [f"user_breaker_{i}" for i in range(5)]
    for uid in user_ids:
        ensure_user(db_conn, uid, email=f"{uid}@example.org")
        _backdate_created(db_conn, uid, days=_SETTLE_DAYS + 1)

    fake_users = _ScriptedUsers({uid: _clerk_errors(404) for uid in user_ids})

    result = run_reconciliation_sweep(
        lambda: _factory_for(db_conn),
        fake_users,
        settle_days=_SETTLE_DAYS,
        row_cap=50,
        sleep_fn=lambda s: None,
    )

    assert result == {
        "status": "clerk_misconfigured",
        "checked": 5,
        "deleted": 0,
        "confirmed_present": 0,
        "errors": 0,
    }
    placeholders = ", ".join(["%s"] * len(user_ids))
    remaining = {
        row["id"]
        for row in db_conn.execute(
            f"SELECT id FROM users WHERE id IN ({placeholders})", tuple(user_ids)
        ).fetchall()
    }
    assert remaining == set(user_ids)  # NOTHING deleted


@requires_postgres
def test_run_reconciliation_sweep_circuit_breaker_does_not_block_minority_404(db_conn):
    """A genuine single deletion inside a healthy majority-present cohort
    must NOT be blocked by the breaker -- only a majority/all-404 pattern
    should trip it."""
    from jobcannon.db._users import ensure_user
    from jobcannon.host.user_deletion import run_reconciliation_sweep

    gone = "user_breaker_gone"
    present_ids = [f"user_breaker_present_{i}" for i in range(6)]
    for uid in (gone, *present_ids):
        ensure_user(db_conn, uid, email=f"{uid}@example.org")
        _backdate_created(db_conn, uid, days=_SETTLE_DAYS + 1)

    outcomes = {gone: _clerk_errors(404)}
    outcomes.update({uid: None for uid in present_ids})
    fake_users = _ScriptedUsers(outcomes)

    result = run_reconciliation_sweep(
        lambda: _factory_for(db_conn),
        fake_users,
        settle_days=_SETTLE_DAYS,
        row_cap=50,
        sleep_fn=lambda s: None,
    )

    assert result["status"] == "ok"
    assert result["checked"] == 7
    assert result["deleted"] == 1
    assert result["confirmed_present"] == 6
    row = db_conn.execute("SELECT id FROM users WHERE id = %s", (gone,)).fetchone()
    assert row is None


# --- rate-limit abort (issue #136 "rate-limit-aware") -------------------


@requires_postgres
def test_run_reconciliation_sweep_aborts_early_on_rate_limit(db_conn):
    """A 429 mid-sweep must abort the remaining candidates rather than
    hammering an already-rate-limited Clerk row by row, and the result must
    be distinguishable from a healthy tick even though nothing outright
    errored. Backdated `created_at` values are staggered (not all identical)
    so `ORDER BY ... created_at ASC` gives a deterministic processing
    order: first, then second (which rate-limits), then never_reached."""
    from jobcannon.db._users import ensure_user
    from jobcannon.host.user_deletion import run_reconciliation_sweep

    first = "user_ratelimit_first"
    second = "user_ratelimit_second"
    never_reached = "user_ratelimit_never_reached"
    ensure_user(db_conn, first, email="a@example.org")
    _backdate_created(db_conn, first, days=_SETTLE_DAYS + 3)
    ensure_user(db_conn, second, email="b@example.org")
    _backdate_created(db_conn, second, days=_SETTLE_DAYS + 2)
    ensure_user(db_conn, never_reached, email="c@example.org")
    _backdate_created(db_conn, never_reached, days=_SETTLE_DAYS + 1)

    # No scripted outcome for never_reached -- a lookup attempt on it raises
    # KeyError, so this test fails loudly (not silently) if the abort ever
    # regresses into continuing past the rate-limited row.
    fake_users = _ScriptedUsers({first: None, second: _clerk_errors(429)})

    result = run_reconciliation_sweep(
        lambda: _factory_for(db_conn),
        fake_users,
        settle_days=_SETTLE_DAYS,
        row_cap=50,
        sleep_fn=lambda s: None,
    )

    assert fake_users.calls == [first, second]
    assert result["checked"] == 2
    assert result["status"] == "degraded"
    assert result["confirmed_present"] == 1
    assert result["errors"] == 1


# --- HIGH-1: real PsycopgConnector / AppNotOpen self-heal -----------------


@requires_postgres
def test_cascade_delete_user_web_process_defer_lands_real_procrastinate_job(monkeypatch):
    """FINDING 1/2 (review-3), HIGH-1 (review-1): the only test that
    exercises the REAL PsycopgConnector, starting from the exact state the
    web process is actually in (task_app.app never opened) -- every other
    cascade test in this file deliberately swaps in InMemoryConnector (see
    this module's own docstring), which cannot raise AppNotOpen and
    therefore cannot prove this path at all. Empirically closes review-3's
    reproduction (never-open -> AppNotOpen; task_app.ensure_open() -> a
    real procrastinate_jobs row lands) end to end against a throwaway
    Postgres DB standing in for the web process's own database."""
    import procrastinate
    import psycopg

    import jobcannon.worker.__main__ as worker_main
    from jobcannon.db._users import ensure_user
    from jobcannon.db.migrate import run_migrations
    from jobcannon.host import posthog_client, task_app
    from jobcannon.host.user_deletion import PURGE_POSTHOG_PERSON_TASK, cascade_delete_user
    from tests.host.conftest import create_throwaway_db, drop_throwaway_db

    dsn, db_name = create_throwaway_db("jobcannon_cascade_real_defer")
    monkeypatch.setenv("DATABASE_URL", dsn)
    posthog_client.set_analytics_salt("real-defer-test-salt")
    try:
        run_migrations(dsn)
        # Apply procrastinate's own schema scoped inside replace_connector,
        # so task_app.app.connector reverts to its untouched (never-opened)
        # default afterwards -- exactly the state a fresh web-process
        # worker is in before its first webhook delivery.
        with task_app.app.replace_connector(procrastinate.PsycopgConnector(conninfo=dsn)):
            worker_main._ensure_procrastinate_schema()

        # Mirrors wiring.init_engine_seams's seam 5 -- bookkeeping only, no
        # I/O (see task_app.py's docstring for why the real open is lazy).
        task_app.configure(dsn)
        try:
            user_id = "user_real_defer"
            with psycopg.connect(dsn) as conn:
                ensure_user(conn, user_id, email="x@example.org")
                expected_pseudonym = posthog_client.pseudonymize(user_id)

                cascade_delete_user(conn, user_id)  # the REAL path, no InMemoryConnector

            with psycopg.connect(dsn) as conn:
                row = conn.execute(
                    "SELECT task_name, queue_name, args FROM procrastinate_jobs"
                ).fetchone()
            assert row is not None
            assert row[0] == PURGE_POSTHOG_PERSON_TASK
            assert row[1] == "maintenance"
            assert row[2]["distinct_id"] == expected_pseudonym
        finally:
            task_app.close()
            task_app.configure(None)
    finally:
        posthog_client.set_analytics_salt(None)
        drop_throwaway_db(db_name)


@requires_postgres
def test_cascade_delete_user_defer_failure_is_fail_open_and_logged(db_conn, monkeypatch):
    """VERIFIED-3 (devin): a genuine (non-AppNotOpen) failure enqueueing the
    purge must not turn an already-committed local delete into a raised
    exception -- the caller (the webhook handler) would otherwise turn a
    successful deletion into a 500, making Clerk retry the whole webhook
    delivery. Forces the failure via monkeypatch (not a real backend error)
    so this test cannot pass for the wrong reason -- AppNotOpen now takes
    an entirely different, self-healing branch, proven by
    test_cascade_delete_user_web_process_defer_lands_real_procrastinate_job
    above; this test is specifically the OTHER branch."""
    import logging

    from jobcannon.db._users import ensure_user
    from jobcannon.host import posthog_client, task_app
    from jobcannon.host.user_deletion import cascade_delete_user

    posthog_client.set_analytics_salt("defer-failure-test-salt")
    try:
        user_id = "user_defer_failure"
        ensure_user(db_conn, user_id, email="x@example.org")

        class _BoomDeferrer:
            def defer(self, **kwargs):
                raise RuntimeError("simulated transient defer failure")

        monkeypatch.setattr(task_app.app, "configure_task", lambda *a, **kw: _BoomDeferrer())

        with caplog_ctx() as records:
            cascade_delete_user(db_conn, user_id)  # must NOT raise

        row = db_conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone()
        assert row is None  # local delete already committed regardless
        assert any(
            "failed to enqueue PostHog purge" in r.getMessage() and r.levelno == logging.ERROR
            for r in records
        )
    finally:
        posthog_client.set_analytics_salt(None)


@contextlib.contextmanager
def caplog_ctx():
    """A minimal log-capture helper, used instead of the `caplog` fixture
    for the one test above that also needs `db_conn` + `monkeypatch` in a
    specific order -- avoids any fixture-interaction surprise, captures
    directly off jobcannon.host.user_deletion's own logger."""
    import logging as _logging

    target_logger = _logging.getLogger("jobcannon.host.user_deletion")
    records: list[_logging.LogRecord] = []

    class _Handler(_logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler()
    target_logger.addHandler(handler)
    try:
        yield records
    finally:
        target_logger.removeHandler(handler)


def test_purge_posthog_person_retry_strategy_bounded_and_gives_up_past_max():
    """MEDIUM-4 (review-1/2/devin): purge_posthog_person must carry a
    bounded RetryStrategy so a transient PostHog 5xx/network error doesn't
    permanently lose the purge (nothing ever re-enqueues it once the local
    users row is gone). Asserts the strategy's actual retry DECISIONS
    (retries early, gives up past max_attempts) rather than just its
    presence -- a RetryStrategy with max_attempts=0 would technically
    "exist" while retrying nothing."""
    from procrastinate.jobs import Job

    from jobcannon.host import tasks
    from jobcannon.host.user_deletion import PURGE_POSTHOG_PERSON_TASK

    task = tasks.app.tasks[PURGE_POSTHOG_PERSON_TASK]
    strategy = task.retry_strategy
    assert strategy is not None
    assert strategy.max_attempts is not None
    assert strategy.max_attempts > 0

    exc = RuntimeError("transient posthog 5xx")

    def _job(attempts: int) -> Job:
        return Job(
            queue="maintenance",
            lock=None,
            queueing_lock=None,
            task_name=PURGE_POSTHOG_PERSON_TASK,
            attempts=attempts,
        )

    assert strategy.get_retry_decision(exception=exc, job=_job(0)) is not None
    assert strategy.get_retry_decision(exception=exc, job=_job(strategy.max_attempts)) is None

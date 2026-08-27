"""jobcannon/web/account.py — GET/POST /account/delete, the self-service
account-deletion trigger.

Two fixture shapes coexist in this file. Most of it stays "no Postgres
needed": this route never itself touches the users/profiles/... tables (the
existing user.deleted Clerk webhook owns that cascade, covered separately
by tests/host/test_webhooks.py), so those tests inject a fake Clerk client
via app.config["CLERK_CLIENT"] the same way tests/host/test_auth.py injects
VERIFY_REQUEST — no throwaway database, matching test_auth.py's lightweight
shape. The before_request chain (ensure_session_ids/capture_attribution/
run_handoff_if_pending) still runs on every request here exactly as it does
in test_auth.py's requests; its DB phase degrades to a no-op without a live
pool (see handoff.py's module docstring), which is what lets those tests
run without Postgres at all.

Issue #159 changed that for the confirm-success path specifically:
post_delete now writes a revoked_subjects tombstone via a real pooled
connection BEFORE calling Clerk, so every test that reaches that code
(confirm-success, the ordering/visibility test, and the tombstone-failure
test) uses the DB-backed `app` fixture below instead (own throwaway
database, mirrors tests/host/test_webhooks.py's `app` fixture) and carries
`@requires_postgres`. GET routes, confirm-rejection, and signed-out tests
never reach that code and stay on the original DB-free `_app()` helper.
"""

from __future__ import annotations

import time

import psycopg
import pytest

from jobcannon.web.auth import ClerkIdentity
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

USER_ID = "user_delete_1"


class _FakeUsers:
    def __init__(self):
        self.calls: list[str] = []

    def delete(self, *, user_id):
        self.calls.append(user_id)


class _FakeClerkClient:
    def __init__(self):
        self.users = _FakeUsers()


class _ExplodingUsers:
    def delete(self, *, user_id):
        raise RuntimeError("simulated Clerk API failure")


class _ExplodingClerkClient:
    def __init__(self):
        self.users = _ExplodingUsers()


def _app(verify, clerk_client=None):
    from jobcannon.web import create_app

    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": "whsec_dGVzdA==",
            "CLERK_CLIENT": clerk_client,
        }
    )


def _authed_verify(user_id=USER_ID):
    return lambda req: ClerkIdentity(user_id=user_id, claims={"sub": user_id})


@pytest.fixture()
def app():
    """DB-backed variant, used only by the tests below that reach
    post_delete's tombstone write. Own throwaway database, same shape as
    tests/host/test_webhooks.py's `app` fixture. VERIFY_REQUEST/CLERK_CLIENT
    start unset and are filled in per-test via `_authed()`, mirroring
    tests/host/test_account_export.py's `_authed(app, user_id)` idiom of
    mutating app.config post-creation rather than needing a second create_app
    seam."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_account_delete")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        flask_app = create_app(
            config={
                "TESTING": True,
                "VERIFY_REQUEST": lambda r: None,
                "WEBHOOK_SECRET": "whsec_dGVzdA==",
            }
        )
        flask_app.config["_TEST_DSN"] = dsn
        yield flask_app
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def _authed(app, clerk_client, user_id=USER_ID):
    app.config["VERIFY_REQUEST"] = _authed_verify(user_id)
    app.config["CLERK_CLIENT"] = clerk_client


class _OrderCheckingUsers:
    """Records not just THAT delete() was called, but whether the tombstone
    was already committed and visible to a SEPARATE connection at the
    moment it was called -- see the test below for why that's a materially
    stronger assertion than a bare call-order recorder."""

    def __init__(self, dsn):
        self.dsn = dsn
        self.calls: list[str] = []
        self.tombstone_visible_at_call_time: bool | None = None

    def delete(self, *, user_id):
        self.calls.append(user_id)
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT 1 FROM revoked_subjects WHERE clerk_user_id = %s AND expires_at > now()",
                (user_id,),
            ).fetchone()
        self.tombstone_visible_at_call_time = row is not None


class _OrderCheckingClerkClient:
    def __init__(self, dsn):
        self.users = _OrderCheckingUsers(dsn)


def _raise_tombstone_failure(conn, user_id):
    raise RuntimeError("simulated tombstone write failure")


def test_account_delete_is_authed_only():
    from jobcannon.web import PUBLIC_PATHS

    assert "/account/delete" not in PUBLIC_PATHS


def test_get_delete_renders_confirmation_form_when_authed():
    app = _app(verify=_authed_verify(), clerk_client=_FakeClerkClient())
    resp = app.test_client().get("/account/delete")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="confirm"' in html
    assert 'value="delete-my-account"' in html


def test_get_delete_401s_when_signed_out():
    app = _app(verify=lambda req: None)
    assert app.test_client().get("/account/delete").status_code == 401


def test_post_delete_401s_when_signed_out():
    app = _app(verify=lambda req: None)
    resp = app.test_client().post("/account/delete", data={"confirm": "delete-my-account"})
    assert resp.status_code == 401


def test_post_without_confirmation_is_rejected_and_does_not_call_clerk():
    client_double = _FakeClerkClient()
    app = _app(verify=_authed_verify(), clerk_client=client_double)

    resp = app.test_client().post("/account/delete", data={})

    assert resp.status_code == 400
    assert client_double.users.calls == []


def test_post_with_wrong_confirmation_value_is_rejected():
    client_double = _FakeClerkClient()
    app = _app(verify=_authed_verify(), clerk_client=client_double)

    resp = app.test_client().post("/account/delete", data={"confirm": "yes"})

    assert resp.status_code == 400
    assert client_double.users.calls == []


@requires_postgres
def test_post_with_confirmation_calls_clerk_delete_exactly_once(app):
    client_double = _FakeClerkClient()
    _authed(app, client_double)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True

    resp = client.post("/account/delete", data={"confirm": "delete-my-account"})

    assert resp.status_code == 200
    assert client_double.users.calls == [USER_ID]


@requires_postgres
def test_post_with_confirmation_clears_the_local_session(app):
    client_double = _FakeClerkClient()
    _authed(app, client_double)
    client = app.test_client()

    # Seed both session ids the way a real prior request would (ensure_session_ids
    # mints them together; seeding only one would KeyError on the next request),
    # plus the handoff-done marker so this DB-backed app (unlike the DB-free
    # `_app()` tests above) doesn't redirect into the one-time post-handoff flow.
    with client.session_transaction() as sess:
        sess["anon_session_id"] = "anon_should_be_cleared"
        sess["feed_session_id"] = "feed_should_be_cleared"
        sess[_HANDOFF_DONE_KEY] = True

    resp = client.post("/account/delete", data={"confirm": "delete-my-account"})
    assert resp.status_code == 200

    with client.session_transaction() as sess:
        assert "anon_session_id" not in sess
        assert "feed_session_id" not in sess


@requires_postgres
def test_post_delete_failure_does_not_clear_session_or_pass_confirmation_twice(app):
    client_double = _ExplodingClerkClient()
    _authed(app, client_double)
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["anon_session_id"] = "anon_kept_on_failure"
        sess["feed_session_id"] = "feed_kept_on_failure"
        sess[_HANDOFF_DONE_KEY] = True

    resp = client.post("/account/delete", data={"confirm": "delete-my-account"})

    assert resp.status_code == 502
    with client.session_transaction() as sess:
        assert sess["anon_session_id"] == "anon_kept_on_failure"


@requires_postgres
def test_post_delete_failure_then_fresh_relogin_recovers_the_account(app):
    """Issue #159 follow-up (refuter-1 L1 / refuter-3 MED, corroborated):
    the previous test proves a Clerk-delete-call failure leaves the
    tombstone committed even though the account was never actually
    deleted. Before the iat comparison, that left the account hard-locked
    out of the ENTIRE authed surface -- including this very route -- for
    the full TTL, with no un-revoke path at all: a naive relogin mints a
    JWT for the same `sub`, which the tombstone still matched
    unconditionally. Proves the actual fix: the STALE identity (no iat,
    same as the token in use at failure time) stays denied, while a FRESH
    relogin (new JWT, iat minted after the tombstone's revoked_at) reaches
    GET /account/delete again."""
    client_double = _ExplodingClerkClient()
    _authed(app, client_double)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True

    resp = client.post("/account/delete", data={"confirm": "delete-my-account"})
    assert resp.status_code == 502  # tombstone now committed, account NOT deleted

    stale_resp = client.get("/account/delete")
    assert stale_resp.status_code == 401

    fresh_iat = int(time.time()) + 300
    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(
        user_id=USER_ID, claims={"sub": USER_ID, "iat": fresh_iat}
    )
    fresh_client = app.test_client()
    with fresh_client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True

    fresh_resp = fresh_client.get("/account/delete")

    assert fresh_resp.status_code == 200


@requires_postgres
def test_tombstone_is_committed_and_visible_on_another_connection_before_clerk_is_called(app):
    """Stronger than a call-order recorder: proves COMMITTED, cross-connection
    visibility by the time Clerk is called, not just sequencing -- the gate
    (_is_subject_revoked in jobcannon/web/__init__.py) reads through a
    DIFFERENT pooled connection than the one post_delete writes through, so
    "wrote before calling Clerk" only matters if that write is ALREADY VISIBLE
    elsewhere by the time Clerk is called. If post_delete were ever
    restructured to hold its own connection open across the Clerk call
    (instead of the `with connection_factory()` block closing first), this
    test would catch it going red even though a bare order-recorder would
    stay green."""
    dsn = app.config["_TEST_DSN"]
    client_double = _OrderCheckingClerkClient(dsn)
    _authed(app, client_double)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True

    resp = client.post("/account/delete", data={"confirm": "delete-my-account"})

    assert resp.status_code == 200
    assert client_double.users.calls == [USER_ID]
    assert client_double.users.tombstone_visible_at_call_time is True


@requires_postgres
def test_post_delete_tombstone_write_failure_never_calls_clerk(app, monkeypatch):
    """Load-bearing assertion for the fail-stop branch (issue #159): if the
    revocation tombstone write itself fails, post_delete must 502 WITHOUT
    ever calling Clerk's delete and WITHOUT clearing the session -- calling
    Clerk anyway would delete the account while leaving its still-valid JWT
    with no working revocation path, reopening the exact window this
    feature exists to close. Patches jobcannon.db._revoked_subjects.
    revoke_subject on ITS OWN module, not a name bound in
    jobcannon.web.account: account.py imports the module inside the
    function body and resolves the attribute at call time, so patching the
    source module is the only target that actually intercepts the call
    (same reason tests/host/test_events_retention.py patches
    "jobcannon.db.connection_factory" rather than a tasks.py-local alias)."""
    client_double = _FakeClerkClient()
    _authed(app, client_double)
    monkeypatch.setattr("jobcannon.db._revoked_subjects.revoke_subject", _raise_tombstone_failure)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["anon_session_id"] = "anon_kept_on_tombstone_failure"
        sess["feed_session_id"] = "feed_kept_on_tombstone_failure"
        sess[_HANDOFF_DONE_KEY] = True

    resp = client.post("/account/delete", data={"confirm": "delete-my-account"})

    assert resp.status_code == 502
    assert client_double.users.calls == []
    with client.session_transaction() as sess:
        assert sess["anon_session_id"] == "anon_kept_on_tombstone_failure"

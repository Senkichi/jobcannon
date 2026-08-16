"""jobcannon/web/account.py — GET/POST /account/delete, the self-service
account-deletion trigger.

No Postgres needed: this route never touches the users/profiles/... tables
itself (the existing user.deleted Clerk webhook owns that cascade, covered
separately by tests/host/test_webhooks.py), so these tests inject a fake
Clerk client via app.config["CLERK_CLIENT"] the same way
tests/host/test_auth.py injects VERIFY_REQUEST — no throwaway database,
matching test_auth.py's lightweight shape rather than
tests/host/test_consent_route.py's DB-backed one. The before_request chain
(ensure_session_ids/capture_attribution/run_handoff_if_pending) still runs
on every request here exactly as it does in test_auth.py's requests; its DB
phase degrades to a no-op without a live pool (see handoff.py's module
docstring), which is what lets these tests run without Postgres at all.
"""

from __future__ import annotations

from jobcannon.web.auth import ClerkIdentity

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


def test_post_with_confirmation_calls_clerk_delete_exactly_once():
    client_double = _FakeClerkClient()
    app = _app(verify=_authed_verify(), clerk_client=client_double)

    resp = app.test_client().post("/account/delete", data={"confirm": "delete-my-account"})

    assert resp.status_code == 200
    assert client_double.users.calls == [USER_ID]


def test_post_with_confirmation_clears_the_local_session():
    client_double = _FakeClerkClient()
    app = _app(verify=_authed_verify(), clerk_client=client_double)
    client = app.test_client()

    # Seed both session ids the way a real prior request would (ensure_session_ids
    # mints them together; seeding only one would KeyError on the next request).
    with client.session_transaction() as sess:
        sess["anon_session_id"] = "anon_should_be_cleared"
        sess["feed_session_id"] = "feed_should_be_cleared"

    resp = client.post("/account/delete", data={"confirm": "delete-my-account"})
    assert resp.status_code == 200

    with client.session_transaction() as sess:
        assert "anon_session_id" not in sess
        assert "feed_session_id" not in sess


def test_post_delete_failure_does_not_clear_session_or_pass_confirmation_twice():
    client_double = _ExplodingClerkClient()
    app = _app(verify=_authed_verify(), clerk_client=client_double)
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["anon_session_id"] = "anon_kept_on_failure"
        sess["feed_session_id"] = "feed_kept_on_failure"

    resp = client.post("/account/delete", data={"confirm": "delete-my-account"})

    assert resp.status_code == 502
    with client.session_transaction() as sess:
        assert sess["anon_session_id"] == "anon_kept_on_failure"

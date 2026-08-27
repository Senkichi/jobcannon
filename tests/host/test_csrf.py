"""CSRFProtect coverage for every state-changing route — issue #146.

`jobcannon.web.create_app` defaults `WTF_CSRF_ENABLED` to False whenever
TESTING is set (jobcannon/web/__init__.py's own comment at that line) so
every OTHER tests/host/ module's pre-existing POST call sites — none of
which carry a token — keep passing unmodified. This module is the one place
that opts back IN (`config={"WTF_CSRF_ENABLED": True, ...}`) to exercise the
real enforcement path end to end: without a token every state-changing route
below 400s; with one, the route behaves exactly as it did before CSRF
shipped (the same status codes tests/host/test_onboarding.py,
tests/host/test_account_route.py, tests/host/test_consent_route.py, and
tests/host/test_feed_events.py already pin for a valid, authenticated
request).

Every "without token" case below deliberately needs no live Postgres pool
and no seeded row: CSRFProtect's `before_request` hook runs before the view
function (and therefore before any DB read/write) even for a syntactically
valid `<int:posting_id>` path segment, so a nonexistent id is fine there.
The "with token" cases that DO reach a real write (`/start`, `/consent`,
`/postings/<id>/save`) open a throwaway database, the same shape
tests/host/test_onboarding.py / tests/host/test_consent_route.py /
tests/host/test_feed_events.py already use.

/healthz carries no test here: it is `@app.get`-only, and
`WTF_CSRF_METHODS` never checks GET, so there is no negative case to prove
(`@csrf.exempt` on it in jobcannon/web/__init__.py is defensive
documentation, not something a POST test could observe).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time

import psycopg
import pytest
from psycopg.rows import dict_row

from jobcannon.web.auth import ClerkIdentity
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

USER_ID = "user_csrf_test"
_CSRF_FIELD_RE = re.compile(rb'name="csrf_token" value="([^"]+)"')
_CSRF_META_RE = re.compile(rb'name="csrf-token" content="([^"]+)"')

WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="


def _stateless_app():
    """CSRF forced on, no DB pool opened — every route below that this app
    exercises either 400s before its view runs (the "without token" half of
    every test) or (account/delete) never touches Postgres at all."""
    from jobcannon.web import create_app

    return create_app(
        config={
            "TESTING": True,
            "WTF_CSRF_ENABLED": True,
            "VERIFY_REQUEST": lambda req: ClerkIdentity(user_id=USER_ID, claims={"sub": USER_ID}),
            "WEBHOOK_SECRET": WEBHOOK_SECRET,
        }
    )


def _token_from(response_data: bytes) -> str:
    match = _CSRF_FIELD_RE.search(response_data) or _CSRF_META_RE.search(response_data)
    assert match, "no CSRF token (hidden field or meta tag) found in response body"
    return match.group(1).decode()


# ---------------------------------------------------------------------------
# Without a token: every state-changing route 400s, none reach their view.
# ---------------------------------------------------------------------------


def test_post_start_without_token_is_400():
    client = _stateless_app().test_client()
    resp = client.post("/start", data={"seniority_level": ""})
    assert resp.status_code == 400
    assert b"Request could not be verified" in resp.data


def test_post_account_delete_without_token_is_400():
    client = _stateless_app().test_client()
    resp = client.post("/account/delete", data={"confirm": "delete-my-account"})
    assert resp.status_code == 400


def test_post_consent_without_token_is_400():
    client = _stateless_app().test_client()
    resp = client.post("/consent", data={"choice": "grant"})
    assert resp.status_code == 400


@pytest.mark.parametrize("action", ["save", "dismiss", "apply"])
def test_post_posting_action_without_token_is_400(action):
    client = _stateless_app().test_client()
    resp = client.post(f"/postings/1/{action}")
    assert resp.status_code == 400


def test_post_posting_action_without_token_and_hx_request_renders_small_fragment():
    """The HX-Request branch of jobcannon.web's CSRFError handler: a small
    swap-safe fragment, not the full error_csrf.html page — save/dismiss
    target their row with hx-swap="outerHTML", so a full HTML document
    landing there would corrupt the surrounding page."""
    client = _stateless_app().test_client()
    resp = client.post("/postings/1/save", headers={"HX-Request": "true"})
    assert resp.status_code == 400
    assert b"<html" not in resp.data
    assert b"data-csrf-error" in resp.data


# ---------------------------------------------------------------------------
# Webhook route: exempt regardless of CSRF token presence.
# ---------------------------------------------------------------------------


def _svix_headers(payload: bytes, msg_id: str = "msg_1") -> dict:
    ts = int(time.time())
    key = base64.b64decode(WEBHOOK_SECRET.removeprefix("whsec_"))
    to_sign = f"{msg_id}.{ts}.".encode() + payload
    sig = base64.b64encode(hmac.new(key, to_sign, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}"}


def test_webhook_route_is_csrf_exempt():
    """An unknown Clerk event type is acknowledged 200 with no DB write
    (jobcannon/web/webhooks.py's own comment on that branch) — the minimal
    case that proves CSRF exemption without needing a live Postgres pool.
    Carries no X-CSRFToken header and no csrf_token form field; a 400 here
    would mean the webhooks blueprint exemption (jobcannon/web/__init__.py's
    `csrf.exempt(webhooks_bp)`) regressed."""
    client = _stateless_app().test_client()
    payload = b'{"type": "some.unknown.event", "object": "event", "data": {}}'
    resp = client.post("/webhooks/clerk", data=payload, headers=_svix_headers(payload))
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# With a token: prior behavior, form-field and header paths both.
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_csrf_test")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        flask_app = create_app(
            config={
                "TESTING": True,
                "WTF_CSRF_ENABLED": True,
                "VERIFY_REQUEST": lambda req: ClerkIdentity(
                    user_id=USER_ID, claims={"sub": USER_ID}
                ),
                "WEBHOOK_SECRET": WEBHOOK_SECRET,
            }
        )
        flask_app.config["_TEST_DSN"] = dsn
        yield flask_app
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


@requires_postgres
def test_post_start_with_token_mints_anon_user(db_app):
    """Prior behavior pinned by tests/host/test_onboarding.py: a valid
    submission redirects to /preview and writes an anon users row."""
    client = db_app.test_client()
    get_resp = client.get("/start")
    token = _token_from(get_resp.data)

    resp = client.post("/start", data={"seniority_level": "", "csrf_token": token})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/preview")

    dsn = db_app.config["_TEST_DSN"]
    with psycopg.connect(dsn) as conn:
        count = conn.execute(
            "SELECT count(*) FROM users WHERE id LIKE 'anon\\_%' ESCAPE '\\'"
        ).fetchone()[0]
    assert count == 1


@requires_postgres
def test_post_consent_with_token_records_grant(db_app):
    """Prior behavior pinned by tests/host/test_consent_route.py: a grant
    redirects and sets analytics_consent."""
    dsn = db_app.config["_TEST_DSN"]
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s)", (USER_ID,))
        conn.commit()

    client = db_app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    get_resp = client.get("/consent")
    token = _token_from(get_resp.data)

    resp = client.post("/consent", data={"choice": "grant", "csrf_token": token})
    assert resp.status_code == 302

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        user = conn.execute(
            "SELECT analytics_consent FROM users WHERE id = %s", (USER_ID,)
        ).fetchone()
    assert user["analytics_consent"] is True


@requires_postgres
def test_post_posting_save_with_header_token_is_the_htmx_path(db_app):
    """Prior behavior pinned by tests/host/test_feed_events.py:
    posting_saved event + 200. Submitted via the X-CSRFToken HEADER, not a
    form field — this is the mechanism base.html's `hx-headers` attribute
    actually uses for every htmx-triggered save/dismiss control, so this is
    the test that proves the "HTMX header path works" half of issue #146's
    requirements, distinct from every other test above (which all use the
    hidden csrf_token FORM field a plain <form method=post> submits)."""
    dsn = db_app.config["_TEST_DSN"]
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s)", (USER_ID,))
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES ('Csrf Header Co') RETURNING id"
        ).fetchone()[0]
        posting_id = conn.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES ('csrf-header-1', %s, 'Engineer', 'Csrf Header Co') RETURNING id",
            (company_id,),
        ).fetchone()[0]
        conn.commit()

    client = db_app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    get_resp = client.get("/demo")  # any base.html render mints the token
    token = _token_from(get_resp.data)

    resp = client.post(f"/postings/{posting_id}/save", headers={"X-CSRFToken": token})
    assert resp.status_code == 200


def test_post_account_delete_with_token_calls_clerk():
    """No throwaway DB needed (account.py never touches Postgres itself —
    same rationale as tests/host/test_account_route.py's module docstring,
    which carries no requires_postgres marker either). Prior behavior pinned
    by tests/host/test_account_route.py: a confirmed deletion calls Clerk's
    delete exactly once and returns 200."""
    from jobcannon.web import create_app

    calls: list[str] = []

    class _FakeUsers:
        def delete(self, *, user_id):
            calls.append(user_id)

    class _FakeClerkClient:
        def __init__(self):
            self.users = _FakeUsers()

    app = create_app(
        config={
            "TESTING": True,
            "WTF_CSRF_ENABLED": True,
            "VERIFY_REQUEST": lambda req: ClerkIdentity(
                user_id=USER_ID, claims={"sub": USER_ID}
            ),
            "WEBHOOK_SECRET": WEBHOOK_SECRET,
            "CLERK_CLIENT": _FakeClerkClient(),
        }
    )
    client = app.test_client()
    get_resp = client.get("/account/delete")
    token = _token_from(get_resp.data)

    resp = client.post(
        "/account/delete", data={"confirm": "delete-my-account", "csrf_token": token}
    )
    assert resp.status_code == 200
    assert calls == [USER_ID]

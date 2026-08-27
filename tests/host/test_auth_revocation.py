"""jobcannon/web/__init__.py's clerk_auth gate revocation check (issue #159)
end to end: a valid, unexpired-but-revoked JWT must 401 on a live request,
not just fail to be issued a fresh one. Own throwaway database, the same
shape as tests/host/test_empty_states.py / tests/host/test_account_export.py
(the gate reads real rows via a real pooled connection, so this cannot share
the session-scoped rollback-isolated `db_conn` fixture).

Route choice matters here (see each test's docstring): `/account/export` is
used for the revoked-401 assertions because it never runs a write and the
request never reaches the route handler anyway (the gate rejects it
upstream) — but the PASS-THROUGH assertions (expired row / no row / a
different subject's row) are made against `/`, not `/account/export`,
because `/` is what actually exercises the code path the new revocation
check sits directly upstream of (consent resolution, ensure_session_ids,
capture_attribution, run_handoff_if_pending) — a `/account/export` pass
would prove nothing about whether the new early-return branch perturbed
that sequence.
"""

from __future__ import annotations

import psycopg
import pytest

from jobcannon.db._profiles import upsert_profile
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

REVOKED_USER = "user_revocation_revoked"
LIVE_USER = "user_revocation_live"


@pytest.fixture()
def app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_auth_revocation")
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


def _authed(app, user_id):
    from jobcannon.web.auth import ClerkIdentity

    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(
        user_id=user_id, claims={"sub": user_id}
    )


def _seed_user(dsn, user_id):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,)
        )


def _seed_profile(dsn, user_id):
    with psycopg.connect(dsn) as conn:
        upsert_profile(conn, user_id)


def _feed_client(app, user_id):
    """An authed test client past the handoff, with a real `users` row and a
    `profiles` row already committed -- mirrors tests/host/test_empty_states.
    py's identical helper, needed here because the pass-through assertions
    hit `/`, which (unlike /account/export) does not degrade gracefully
    without a seeded profile row."""
    dsn = app.config["_TEST_DSN"]
    _authed(app, user_id)
    _seed_user(dsn, user_id)
    _seed_profile(dsn, user_id)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    return client


def _revoke(dsn, user_id, *, minutes_until_expiry: float = 15) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO revoked_subjects (clerk_user_id, expires_at) "
            "VALUES (%s, now() + make_interval(mins => %s))",
            (user_id, minutes_until_expiry),
        )


def test_revoked_subject_401s_on_account_export(app):
    """The gate rejects before export.py's route handler ever runs -- no
    users/profiles seeding needed, since the request never reaches it."""
    dsn = app.config["_TEST_DSN"]
    _authed(app, REVOKED_USER)
    _revoke(dsn, REVOKED_USER)
    client = app.test_client()

    resp = client.get("/account/export")

    assert resp.status_code == 401


def test_revoked_subject_401s_on_feed_root(app):
    dsn = app.config["_TEST_DSN"]
    _authed(app, REVOKED_USER)
    _revoke(dsn, REVOKED_USER)
    client = app.test_client()

    resp = client.get("/")

    assert resp.status_code == 401


def test_revoked_subject_gets_its_session_cleared(app):
    """A stale Flask session cookie must not survive a revoked identity --
    this is the ONLY place a webhook-triggered (Account-Portal) deletion
    ever gets a chance to clear the local session, since that path never
    goes through account.py::post_delete."""
    dsn = app.config["_TEST_DSN"]
    _authed(app, REVOKED_USER)
    _revoke(dsn, REVOKED_USER)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["some_pre_existing_marker"] = "should-not-survive"

    client.get("/account/export")

    with client.session_transaction() as sess:
        assert "some_pre_existing_marker" not in sess


def test_expired_revocation_row_allows_the_request_through(app):
    """A tombstone past its own expires_at must not keep denying access --
    pruning it is the periodic sweep's job, not this read path's."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, LIVE_USER)
    _revoke(dsn, LIVE_USER, minutes_until_expiry=-1)

    resp = client.get("/")

    assert resp.status_code == 200


def test_no_revocation_row_allows_the_request_through(app):
    client = _feed_client(app, LIVE_USER)

    resp = client.get("/")

    assert resp.status_code == 200


def test_revocation_of_a_different_subject_does_not_block_this_one(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, LIVE_USER)
    _revoke(dsn, REVOKED_USER)

    resp = client.get("/")

    assert resp.status_code == 200

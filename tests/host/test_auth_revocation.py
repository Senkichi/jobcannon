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

import time

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


def _authed(app, user_id, *, iat=None):
    from jobcannon.web.auth import ClerkIdentity

    claims = {"sub": user_id}
    if iat is not None:
        claims["iat"] = iat
    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(user_id=user_id, claims=claims)


def _seed_user(dsn, user_id):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))


def _seed_profile(dsn, user_id):
    with psycopg.connect(dsn) as conn:
        upsert_profile(conn, user_id, workplace_type=None)


def _feed_client(app, user_id, *, iat=None):
    """An authed test client past the handoff, with a real `users` row and a
    `profiles` row already committed -- mirrors tests/host/test_empty_states.
    py's identical helper, needed here because the pass-through assertions
    hit `/`, which (unlike /account/export) does not degrade gracefully
    without a seeded profile row."""
    dsn = app.config["_TEST_DSN"]
    _authed(app, user_id, iat=iat)
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


def test_revoked_subject_401_page_hides_authed_nav(app):
    """Issue #205 follow-up, and the regression test that actually
    motivates it: base.html's header nav, My-postings link, and footer
    Export/Delete links were repointed from `g.clerk_user` to
    `visitor_is_authed` so a signed-in visitor sees the right nav on a
    PUBLIC_PATHS page (test_auth_nav.py / test_demo_feed.py /
    test_preview.py). Naively deriving `visitor_is_authed` from
    `_visitor_is_anonymous()`'s existing fallback (onboarding.
    _current_identity(), a raw VERIFY_REQUEST re-check that knows nothing
    about revoked_subjects) would have re-authenticated this exact
    cryptographically-valid-but-tombstoned JWT on THIS page and shown
    "My postings" / "Export your data" / "Delete account" on the very
    response telling this visitor they're signed out -- the account-
    mutation-link leak jobcannon/web/__init__.py's revoked-subject comment
    already warned `g.clerk_user` itself must never suffer.
    _visitor_is_anonymous() is tightened to fail CLOSED (anonymous)
    whenever g.clerk_user is None off of an actual PUBLIC_PATHS render --
    this proves that holds for the one non-public-path case where
    g.clerk_user is deliberately reset to None: the revocation abort."""
    dsn = app.config["_TEST_DSN"]
    _authed(app, REVOKED_USER)
    _revoke(dsn, REVOKED_USER)
    client = app.test_client()

    resp = client.get("/account/export")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 401
    assert "data-postings-history-nav-link" not in html
    assert ">My postings<" not in html
    assert ">Export your data<" not in html
    assert ">Delete account<" not in html


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


def test_revocation_lookup_failure_fails_open(app, monkeypatch, caplog):
    """A DB error surfaced from the revocation lookup itself (not a
    pool-wide outage) must not block a live request -- fail-open here
    creates no NEW "revoked user reaches data" path, since every authed
    route already depends on the same pool for its own reads; see
    _is_subject_revoked's docstring in jobcannon/web/__init__.py for the
    full rationale. Patches the DAL call itself, not connection_factory,
    so this isolates the revocation check's own error handling from
    consent resolution and the route handler's own DB reads -- both share
    the same pool and would otherwise also break, muddying what actually
    failed open."""
    import logging

    from jobcannon.db import _revoked_subjects

    client = _feed_client(app, LIVE_USER)

    def _boom(conn, user_id, issued_at=None):
        raise RuntimeError("revoked_subjects query failed (simulated)")

    monkeypatch.setattr(_revoked_subjects, "is_subject_revoked", _boom)

    with caplog.at_level(logging.WARNING):
        resp = client.get("/")

    assert resp.status_code == 200
    assert any("revocation" in rec.message.lower() for rec in caplog.records)


def test_revoked_subject_with_a_pre_revocation_iat_still_401s(app):
    """Issue #159 follow-up (refuter-1 L1 / refuter-3 MED, corroborated):
    the iat comparison must keep denying the SAME token that could have
    existed before the revocation -- an iat minted well before revoked_at
    must never slip through, or the tombstone stops meaning anything."""
    dsn = app.config["_TEST_DSN"]
    _revoke(dsn, REVOKED_USER)
    stale_iat = int(time.time()) - 300  # minted 5 minutes before revocation
    _authed(app, REVOKED_USER, iat=stale_iat)
    client = app.test_client()

    resp = client.get("/account/export")

    assert resp.status_code == 401


def test_revoked_subject_with_a_fresh_post_revocation_iat_is_allowed_through(app):
    """Issue #159 follow-up: this is the recovery path account.py::post_
    delete's docstring now cites by name. When the Clerk-delete call fails
    after the tombstone already committed, the account is NOT deleted and
    the tombstone is deliberately left in place -- the ONLY way back in is
    a fresh relogin minting a JWT whose iat postdates revoked_at. Proves
    the gate honors that JWT even while the row is still within its TTL."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, LIVE_USER)
    _revoke(dsn, LIVE_USER)
    fresh_iat = int(time.time()) + 300  # minted 5 minutes after revocation
    _authed(app, LIVE_USER, iat=fresh_iat)

    resp = client.get("/")

    assert resp.status_code == 200

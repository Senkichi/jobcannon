"""Consent-version enforcement (issue: consent version is recorded but never
enforced — no re-consent trigger on a CONSENT_VERSION bump).

A grant recorded at a stale jobcannon.web.consent.CONSENT_VERSION must stop
authorizing PostHog fan-out immediately (jobcannon/web/__init__.py's
per-request g.consent_granted resolution), and the anon-to-authed handoff's
existing one-time /consent redirect (jobcannon/web/handoff.py) must fire
again the next time a session hasn't yet completed it — no new route, per
the issue's stated fix shape. A decline is deliberately version-independent:
re-consent exists to re-ask people whose GRANT predates new tracking, not to
nag decliners, so a stale decline must never redirect.

Every "code is now at a new version" scenario below is simulated by
monkeypatching jobcannon.web.consent.CONSENT_VERSION directly rather than
running two app instances: every reader of it (jobcannon.web._resolve_consent,
jobcannon.web.handoff.run_handoff_if_pending, jobcannon.web.consent's own
route) does a call-time — not import-time — lookup for exactly this reason
(see each function's docstring), so the monkeypatch takes effect on the very
next request.

Own throwaway database, same shape as tests/host/test_handoff.py and
tests/host/test_feed_events.py: record_consent/log_event do real, durable
writes that a separately-opened connection must see.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from jobcannon.db._events import db_now_iso, record_consent
from jobcannon.host import posthog_client
from jobcannon.host.events import log_event
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

CLERK_ID = "user_consent_version_test"

_SIGNUP_PAYLOAD = {
    "channel": "direct",
    "wave": "0",
    "signup_method": "clerk",
    "referrer_url": "unknown",
}


@pytest.fixture(autouse=True)
def _clean_posthog_client():
    yield
    posthog_client.set_posthog_client(None)


@pytest.fixture()
def app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_consent_version")
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


def _authed(app, user_id=CLERK_ID):
    from jobcannon.web.auth import ClerkIdentity

    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(
        user_id=user_id, claims={"sub": user_id}
    )


def _seed_user(dsn, user_id):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))


def _record(dsn, user_id, *, granted, version):
    """The sanctioned way to write a consent decision: record_consent with a
    database-clock consented_at, committed on its own connection before the
    request under test is issued (mirrors tests/host/test_feed_events.py's
    _grant_consent)."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        record_consent(
            conn,
            user_id=user_id,
            consent_type="analytics",
            granted=granted,
            consent_version=version,
            consented_at=db_now_iso(conn),
        )
        conn.commit()


def _seed_prior_signup(user_id):
    """Marks this user as already-signed-up (the durable
    jobcannon.db._events.has_signed_up_event check), matching a returning
    user rather than a brand-new one — this test suite is about re-consent
    on an EXISTING account, not the first-signup emission path already
    covered by tests/host/test_handoff.py."""
    log_event(
        "user_signed_up",
        user_id=user_id,
        consent_granted=True,
        payload=dict(_SIGNUP_PAYLOAD),
    )


def _handoff_done_client(app):
    """An authed client whose handoff has already completed for THIS
    session — isolates a test from the one-time post-signup redirect so it
    can exercise a route directly, same technique as
    tests/host/test_consent_resolution.py's _skip_handoff."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    return client


def _events_rows(dsn, user_id, event_type):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT * FROM events WHERE user_id = %s AND event_type = %s",
            (user_id, event_type),
        ).fetchall()


class _FakePosthog:
    def __init__(self):
        self.captured = []

    def capture(self, **kwargs):
        self.captured.append(kwargs)


def test_grant_at_current_version_is_consented(app):
    dsn = app.config["_TEST_DSN"]
    _seed_user(dsn, CLERK_ID)
    _record(dsn, CLERK_ID, granted=True, version="v1")
    _authed(app)

    from flask import g

    seen = {}

    @app.get("/whoami")
    def whoami():
        seen["consent_granted"] = g.consent_granted
        return "ok"

    resp = _handoff_done_client(app).get("/whoami")
    assert resp.status_code == 200
    assert seen["consent_granted"] is True


def test_version_bump_revokes_consent_and_gates_posthog_fanout_until_regrant(app, monkeypatch):
    """grant at v1 + bump to v2 -> not consented, PostHog fan-out gated off
    -- and restored once the user re-grants under the new version."""
    import jobcannon.web.consent as consent_mod

    dsn = app.config["_TEST_DSN"]
    _seed_user(dsn, CLERK_ID)
    _record(dsn, CLERK_ID, granted=True, version="v1")
    _authed(app)

    fake = _FakePosthog()
    posthog_client.set_posthog_client(fake)
    # Post-pseudonymization, fan-out fails closed without a salt; opt in the
    # same way test_events.py does (conftest's autouse reset clears it after).
    posthog_client.set_analytics_salt("test-salt-consent-version")

    @app.get("/emit")
    def emit():
        log_event("posting_saved", user_id=CLERK_ID)
        return "ok"

    client = _handoff_done_client(app)

    # Before the bump: the v1 grant authorizes the fan-out.
    client.get("/emit")
    assert len(fake.captured) == 1

    # Code bumps CONSENT_VERSION -- the SAME stored v1 grant must stop
    # authorizing new tracking immediately, with no user action.
    monkeypatch.setattr(consent_mod, "CONSENT_VERSION", "v2")
    fake.captured.clear()

    client.get("/emit")
    assert fake.captured == []

    # "Until re-grant": granting again under the new version restores it.
    client.post("/consent", data={"choice": "grant"})
    client.get("/emit")
    assert len(fake.captured) == 1

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT analytics_consent_version FROM users WHERE id = %s", (CLERK_ID,)
        ).fetchone()
    assert row["analytics_consent_version"] == "v2"


def test_stale_grant_reprompts_once_via_the_existing_handoff_redirect(app, monkeypatch):
    """grant at v1 + bump to v2 -> the next session that has not yet
    completed the handoff (jobcannon.web.handoff._HANDOFF_DONE_KEY) gets
    redirected to /consent exactly once, then never again for that same
    session -- the existing one-time redirect, re-armed by choice_made
    turning false, not a new route."""
    import jobcannon.web.consent as consent_mod

    dsn = app.config["_TEST_DSN"]
    _seed_user(dsn, CLERK_ID)
    _record(dsn, CLERK_ID, granted=True, version="v1")
    _seed_prior_signup(CLERK_ID)
    _authed(app)

    monkeypatch.setattr(consent_mod, "CONSENT_VERSION", "v2")

    fresh_client = app.test_client()
    first = fresh_client.get("/")
    assert first.status_code == 302
    assert first.headers["Location"].endswith("/consent")

    second = fresh_client.get("/")
    assert second.status_code == 200

    # No duplicate signup emission -- only the re-consent redirect fired.
    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1


def test_stale_decline_is_never_reprompted(app, monkeypatch):
    """decline at v1 + bump to v2 -> still declined, no redirect. The
    asymmetry from the issue: re-consent re-asks people whose GRANT predates
    new tracking, never people who already said no."""
    import jobcannon.web.consent as consent_mod

    dsn = app.config["_TEST_DSN"]
    _seed_user(dsn, CLERK_ID)
    _record(dsn, CLERK_ID, granted=False, version="v1")
    _seed_prior_signup(CLERK_ID)
    _authed(app)

    monkeypatch.setattr(consent_mod, "CONSENT_VERSION", "v2")

    fresh_client = app.test_client()
    resp = fresh_client.get("/")
    assert resp.status_code == 200  # no redirect to /consent

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT analytics_consent FROM users WHERE id = %s", (CLERK_ID,)
        ).fetchone()
    assert row["analytics_consent"] is False

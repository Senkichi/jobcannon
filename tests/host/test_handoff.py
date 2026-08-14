"""jobcannon/web/handoff.py — the anon-to-authed handoff.

Own throwaway database, same shape as tests/host/test_webhooks.py and
tests/host/test_onboarding.py: the handoff does real, durable INSERT/DELETE
writes on users/profiles/events, so this module cannot share the
session-scoped postgres_test_dsn every rollback-isolated tests/host/ module
reads inside a transaction.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from jobcannon.host import posthog_client
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

CLERK_ID = "user_clerk_1"


@pytest.fixture(autouse=True)
def _clean_posthog_client():
    yield
    posthog_client.set_posthog_client(None)


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_handoff")
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


def _complete_picker(client):
    return client.post(
        "/start",
        data={
            "titles": ["Engineer"],
            "skills": ["python"],
            "seniority_level": "senior",
            "years_of_experience": "5",
            "workplace_type": "remote",
        },
    )


def _events_rows(dsn, user_id, event_type):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT * FROM events WHERE user_id = %s AND event_type = %s",
            (user_id, event_type),
        ).fetchall()


def _profile_row(dsn, user_id):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute("SELECT * FROM profiles WHERE user_id = %s", (user_id,)).fetchone()


def test_signup_writes_user_signed_up_with_channel_and_wave(app):
    dsn = app.config["_TEST_DSN"]
    client = app.test_client()
    _authed(app)

    resp = client.get("/", query_string={"ref": "hackernews"})
    assert resp.status_code in (200, 302, 303)

    rows = _events_rows(dsn, CLERK_ID, "user_signed_up")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["channel"] == "hackernews"
    assert payload["wave"] == "0"
    assert payload["signup_method"] == "clerk"


def test_attribution_is_recorded_for_a_brand_new_non_consenting_account(app):
    """Signup attribution's default-form acceptance: a brand-new account's
    analytics_consent is false (nobody has chosen yet) and one
    user_signed_up row still exists. Without the first-party-write
    exemption this is zero rows for every signup."""
    dsn = app.config["_TEST_DSN"]
    client = app.test_client()
    _authed(app)

    client.get("/")

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        user = conn.execute(
            "SELECT analytics_consent FROM users WHERE id = %s", (CLERK_ID,)
        ).fetchone()
    assert user["analytics_consent"] is False
    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1


def test_new_signup_produces_no_posthog_capture_and_a_consenting_user_does(app):
    dsn = app.config["_TEST_DSN"]

    class _FakeClient:
        def __init__(self):
            self.captured = []

        def capture(self, **kwargs):
            self.captured.append(kwargs)

    fake = _FakeClient()
    posthog_client.set_posthog_client(fake)

    client = app.test_client()
    _authed(app, user_id="user_new")
    client.get("/")
    assert fake.captured == []

    # A second account, already consenting BEFORE its first authed request.
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO users (id, analytics_consent, analytics_consent_updated_at) "
            "VALUES ('user_consented', true, now())"
        )
        conn.commit()

    client2 = app.test_client()
    _authed(app, user_id="user_consented")
    client2.get("/")

    assert len(fake.captured) == 1
    assert fake.captured[0]["event"] == "user_signed_up"


def test_signup_event_passes_consent_explicitly_not_from_stale_g(app, monkeypatch):
    """Force the ambient g.consent_granted (resolved at request start, before
    the handoff runs) to a value that DISAGREES with the database, proving
    the user_signed_up emission reads read_consent_state fresh at emit time
    rather than trusting the stale ambient value."""
    import jobcannon.web as web_mod

    monkeypatch.setattr(web_mod, "_resolve_consent", lambda identity: True)

    class _FakeClient:
        def __init__(self):
            self.captured = []

        def capture(self, **kwargs):
            self.captured.append(kwargs)

    fake = _FakeClient()
    posthog_client.set_posthog_client(fake)

    dsn = app.config["_TEST_DSN"]
    client = app.test_client()
    _authed(app)

    client.get("/")

    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1
    # A fresh account has never had consent granted. If the emission had
    # trusted the forced-True ambient g instead of a fresh DB read, this
    # would have fanned out to PostHog. It must not have.
    assert fake.captured == []


def test_referrer_url_payload_is_hostname_only(app):
    dsn = app.config["_TEST_DSN"]
    client = app.test_client()
    _authed(app)

    client.get("/", headers={"Referer": "https://example.com/path?x=1"})

    rows = _events_rows(dsn, CLERK_ID, "user_signed_up")
    assert rows[0]["payload"]["referrer_url"] == "example.com"


def test_anon_profile_is_rekeyed_to_clerk_id_and_anon_user_row_is_deleted(app):
    dsn = app.config["_TEST_DSN"]
    client = app.test_client()

    _complete_picker(client)
    with client.session_transaction() as sess:
        anon_id = sess["pending_picker"]["anon_id"]

    _authed(app)
    client.get("/")

    clerk_profile = _profile_row(dsn, CLERK_ID)
    assert clerk_profile is not None
    assert clerk_profile["seniority_level"] == "senior"
    assert sorted(clerk_profile["skills"]) == ["python"]

    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute("SELECT count(*) FROM users WHERE id = %s", (anon_id,)).fetchone()[0] == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM profiles WHERE user_id = %s", (anon_id,)).fetchone()[
                0
            ]
            == 0
        )


def test_handoff_writes_no_consent_row(app):
    dsn = app.config["_TEST_DSN"]
    client = app.test_client()
    _authed(app)

    client.get("/")

    assert _events_rows(dsn, CLERK_ID, "consent_recorded") == []


def test_handoff_redirects_once_to_consent_then_never_again(app):
    client = app.test_client()
    _authed(app)

    first = client.get("/")
    assert first.status_code == 302
    assert first.headers["Location"].endswith("/consent")

    second = client.get("/")
    assert second.status_code == 200


def test_handoff_is_idempotent_on_subsequent_requests(app):
    dsn = app.config["_TEST_DSN"]
    client = app.test_client()
    _authed(app)

    client.get("/")
    client.get("/")
    client.get("/")

    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1


def test_signup_without_picker_still_records_attribution(app):
    """No POST /start in this session at all — the handoff must skip the
    profile re-key and the anon-row delete, but still emit user_signed_up
    with real attribution and still redirect once to /consent."""
    dsn = app.config["_TEST_DSN"]
    client = app.test_client()
    _authed(app)

    resp = client.get("/", query_string={"ref": "direct-signup"})
    assert resp.status_code == 302

    rows = _events_rows(dsn, CLERK_ID, "user_signed_up")
    assert len(rows) == 1
    assert rows[0]["payload"]["channel"] == "direct-signup"

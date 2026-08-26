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
    """Opts this file in to a fixed analytics pseudonymization salt: the
    fan-out assertions below need one configured or every PostHog call fails
    closed (tests/host/conftest.py's directory-wide default is unconfigured/
    None, and it resets back to that after this fixture's own teardown)."""
    posthog_client.set_analytics_salt("test-salt-handoff")
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
    # analytics_consent_version = 'v1' matches CONSENT_VERSION
    # (jobcannon/web/consent.py): a stored grant with no version, or a stale
    # one, now reads as not consented (jobcannon/db/_events.py::
    # read_consent_state) — this test is about the emission's fan-out gate,
    # not version staleness, so the seeded grant is current.
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO users (id, analytics_consent, analytics_consent_updated_at, "
            "analytics_consent_version) VALUES ('user_consented', true, now(), 'v1')"
        )
        conn.commit()

    client2 = app.test_client()
    _authed(app, user_id="user_consented")
    client2.get("/")

    assert len(fake.captured) == 1
    assert fake.captured[0]["event"] == "user_signed_up"
    # The raw Clerk user id must never reach PostHog as distinct_id.
    assert fake.captured[0]["distinct_id"] != "user_consented"
    assert fake.captured[0]["distinct_id"] == posthog_client.pseudonymize("user_consented")


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


def test_two_cookie_jars_for_same_clerk_user_emit_user_signed_up_once(app):
    """The cross-session repro this suite was missing: a second browser /
    device / incognito window for the SAME Clerk user is a second
    `test_client()` — a fresh cookie jar that has never seen
    `_HANDOFF_DONE_KEY` — not a second request on the same client. Before the
    durable per-user guard in `_events.has_signed_up_event`, each jar ran the
    full emission path independently and doubled the `user_signed_up` row."""
    dsn = app.config["_TEST_DSN"]
    _authed(app)

    client1 = app.test_client()
    resp1 = client1.get("/")
    assert resp1.status_code in (200, 302, 303)

    client2 = app.test_client()
    resp2 = client2.get("/")
    assert resp2.status_code in (200, 302, 303)

    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1


def test_distinct_user_still_emits_after_another_users_signup(app):
    """The durable guard is scoped to `user_id`, not global: a second,
    distinct Clerk user's first authed request must still get its own
    user_signed_up row even though another user has already signed up and
    left a row in the same events table."""
    dsn = app.config["_TEST_DSN"]
    other_id = "user_clerk_2"

    _authed(app, user_id=CLERK_ID)
    client1 = app.test_client()
    client1.get("/")
    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1

    _authed(app, user_id=other_id)
    client2 = app.test_client()
    client2.get("/")
    assert len(_events_rows(dsn, other_id, "user_signed_up")) == 1
    # The first user's row is untouched by the second user's signup.
    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1


def test_picker_resubmission_after_signup_is_rekeyed_not_orphaned(app):
    """A picker submitted at /start AFTER the handoff has already completed
    once must still be consumed on a later authed request — the completion
    marker gates only the emission phase, never the DB re-key phase. Before
    the fix, `_HANDOFF_DONE_KEY` short-circuited the whole handoff, so
    `pending_picker` was never consumed and the anon users+profiles pair
    was orphaned permanently."""
    dsn = app.config["_TEST_DSN"]
    client = app.test_client()
    _authed(app)

    # First authed request: no pending picker, completes the handoff.
    client.get("/")
    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1

    # /start is public and cannot see the authed identity, so resubmitting
    # the picker mints a fresh anon users+profiles pair.
    resp = _complete_picker(client)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        anon_id = sess["pending_picker"]["anon_id"]

    # A later authed request must still consume the pending picker even
    # though handoff_done is already set from the first request.
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
    with client.session_transaction() as sess:
        assert "pending_picker" not in sess

    # The emission phase stayed gated on the completion marker: no second
    # user_signed_up row for the resubmission.
    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1


def test_signup_emission_failure_does_not_500_and_retries_next_request(app, monkeypatch):
    """A `log_event` failure during the emission phase (e.g. an oversized
    `wave` value rejected by `events_schema.validate_payload`) must not
    propagate into `before_request` as a 500, and must not permanently wedge
    the session: the DB phase has already committed by the time this runs,
    so a later authed request must complete the emission instead of 500ing
    forever."""
    dsn = app.config["_TEST_DSN"]
    client = app.test_client()
    _authed(app)

    import jobcannon.web.handoff as handoff_mod

    real_log_event = handoff_mod.log_event
    calls = {"n": 0}

    def _flaky_log_event(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("payload rejected (simulated)")
        return real_log_event(*args, **kwargs)

    monkeypatch.setattr(handoff_mod, "log_event", _flaky_log_event)

    resp = client.get("/")
    assert resp.status_code != 500
    assert _events_rows(dsn, CLERK_ID, "user_signed_up") == []
    with client.session_transaction() as sess:
        assert sess.get(handoff_mod._HANDOFF_DONE_KEY) is not True
        assert sess.get("attribution") is not None

    resp2 = client.get("/")
    assert resp2.status_code != 500
    assert len(_events_rows(dsn, CLERK_ID, "user_signed_up")) == 1
    with client.session_transaction() as sess:
        assert sess.get(handoff_mod._HANDOFF_DONE_KEY) is True


def test_oversized_signup_wave_does_not_500_or_wedge_the_session():
    """Reproduces the exact production reachability path named in review:
    JC_SIGNUP_WAVE (jobcannon/host/config.py) is read unvalidated and
    unbounded into the `wave` payload key, and
    events_schema.validate_payload rejects any string over 200 chars.
    Without wrapping the emission this 500s every authed request in the
    session forever."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.host.config import HostConfig
    from jobcannon.web import create_app
    from jobcannon.web.auth import ClerkIdentity

    dsn, db_name = create_throwaway_db("jobcannon_handoff_wave")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        flask_app = create_app(
            config={
                "TESTING": True,
                "VERIFY_REQUEST": lambda req: ClerkIdentity(
                    user_id=CLERK_ID, claims={"sub": CLERK_ID}
                ),
                "WEBHOOK_SECRET": "whsec_dGVzdA==",
                "HOST_CONFIG": HostConfig(
                    database_url="",
                    secret_key="testing-secret-key",
                    clerk_sign_up_url="https://clerk.test/sign-up",
                    signup_wave="w" * 250,
                ),
            }
        )
        client = flask_app.test_client()

        resp = client.get("/")
        assert resp.status_code != 500

        assert _events_rows(dsn, CLERK_ID, "user_signed_up") == []

        # Nothing wedged: an unrelated request on the same session still
        # succeeds instead of 500ing on every subsequent hit.
        resp2 = client.get("/healthz")
        assert resp2.status_code == 200
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


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

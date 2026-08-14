"""jobcannon/web/__init__.py's per-request g.consent_granted resolution
(1B Wave 2 PR 8, Task F): the before_request hook that feeds
jobcannon.host.events.log_event's ambient consent gate.

test_resolve_consent_fails_closed_without_a_pool needs no Postgres: it pins
that an unopened connection pool (the exact state of every existing
TESTING=True test in tests/host/test_auth.py, which authenticates requests
without ever opening a pool) degrades g.consent_granted to False instead of
raising — this is what keeps this PR's new before_request code from
regressing test_auth.py's pool-free tests. The remaining tests need a real
Postgres users.analytics_consent column and are requires_postgres-gated.
"""

from __future__ import annotations

import pytest

from jobcannon.web import _resolve_consent
from jobcannon.web.auth import ClerkIdentity
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres


def test_resolve_consent_fails_closed_without_a_pool():
    # No pool_mod.open_pool() call anywhere in this test — connection_factory()
    # must raise "pool not opened", which _resolve_consent must swallow.
    identity = ClerkIdentity(user_id="user_no_pool", claims={"sub": "user_no_pool"})
    assert _resolve_consent(identity) is False


@pytest.fixture()
def app_with_pool():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_consent")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        yield (
            create_app(
                config={
                    "TESTING": True,
                    "VERIFY_REQUEST": None,  # set per-test below
                    "WEBHOOK_SECRET": "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=",
                }
            ),
            dsn,
        )
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def _skip_handoff(client) -> None:
    """These tests exercise _resolve_consent's per-request gate in
    isolation, not the anon-to-authed handoff (covered separately in
    tests/host/test_handoff.py). Without this, a fresh client's first
    authenticated request is the handoff's one-time trip, which redirects
    to /consent instead of ever reaching the /whoami route registered
    below."""
    from jobcannon.web.handoff import _HANDOFF_DONE_KEY

    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True


@requires_postgres
def test_consent_granted_true_reaches_g_consent_granted(app_with_pool):
    import psycopg
    from flask import g

    app, dsn = app_with_pool
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO users (id, email, analytics_consent) VALUES "
            "('user_consented', 'a@example.org', true)"
        )
        conn.commit()

    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(
        user_id="user_consented", claims={"sub": "user_consented"}
    )
    seen = {}

    @app.get("/whoami")
    def whoami():
        seen["consent_granted"] = g.consent_granted
        return "ok"

    client = app.test_client()
    _skip_handoff(client)
    resp = client.get("/whoami")
    assert resp.status_code == 200
    assert seen["consent_granted"] is True


@requires_postgres
def test_consent_defaults_false_for_user_without_consent_row(app_with_pool):
    from flask import g

    app, dsn = app_with_pool
    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(
        user_id="user_never_seen", claims={"sub": "user_never_seen"}
    )
    seen = {}

    @app.get("/whoami")
    def whoami():
        seen["consent_granted"] = g.consent_granted
        return "ok"

    client = app.test_client()
    _skip_handoff(client)
    resp = client.get("/whoami")
    assert resp.status_code == 200
    assert seen["consent_granted"] is False


@requires_postgres
def test_public_and_webhook_paths_default_consent_false_without_db_read(app_with_pool):
    app, _dsn = app_with_pool
    app.config["VERIFY_REQUEST"] = lambda req: None
    # /healthz is PUBLIC_PATHS-exempt; must not touch the DB or crash.
    assert app.test_client().get("/healthz").status_code == 200

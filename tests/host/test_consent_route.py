"""jobcannon/web/consent.py — GET/POST /consent, the one consent-collection
surface in the product.

Own throwaway database, same shape as tests/host/test_webhooks.py and
tests/host/test_handoff.py: record_consent does a real, durable UPDATE on
users plus an INSERT on events, so this module cannot share the
session-scoped postgres_test_dsn every rollback-isolated tests/host/ module
reads inside a transaction.

Every test that exercises the route pre-seeds a users row and marks the
handoff done in the session (jobcannon.web.handoff._HANDOFF_DONE_KEY) so the
anon-to-authed handoff (tested separately in tests/host/test_handoff.py)
never intercepts the request — capture_attribution() populates
session["attribution"] on a client's very first request regardless of path,
which would otherwise make the handoff pending and redirect the request
before it ever reaches this route's view function.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from jobcannon.db import _events
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

USER_ID = "user_consent_1"


@pytest.fixture()
def app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_consent_route")
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


def _authed(app, user_id=USER_ID):
    from jobcannon.web.auth import ClerkIdentity

    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(
        user_id=user_id, claims={"sub": user_id}
    )


def _seeded_client(app, dsn, user_id=USER_ID):
    """An authed test client whose user row already exists and whose handoff
    has already run (skip target: see module docstring) — every request it
    makes lands directly on the route under test, not on the one-time
    post-handoff redirect."""
    from jobcannon.web.handoff import _HANDOFF_DONE_KEY

    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
        conn.commit()

    _authed(app, user_id)
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


def _user_row(dsn, user_id):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT analytics_consent, analytics_consent_updated_at FROM users WHERE id = %s",
            (user_id,),
        ).fetchone()


def test_post_consent_grant_sets_column_and_writes_one_audit_row(app):
    """HX-Request (the norm -- the form's own hx-post) so the write is
    asserted through the in-place-confirmation response shape; the non-HX
    PRG redirect shape is covered separately by
    test_post_consent_direct_request_redirects_then_shows_the_choice."""
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    resp = client.post("/consent", data={"choice": "grant"}, headers={"HX-Request": "true"})
    # issue #182: no longer a 302-to-feed that silently discards the ack --
    # the same route re-renders with an inline confirmation instead.
    assert resp.status_code == 200
    assert "Analytics enabled." in resp.get_data(as_text=True)

    user = _user_row(dsn, USER_ID)
    assert user["analytics_consent"] is True
    assert user["analytics_consent_updated_at"] is not None

    rows = _events_rows(dsn, USER_ID, "consent_recorded")
    assert len(rows) == 1


def test_post_consent_decline_is_a_real_producible_path(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    resp = client.post("/consent", data={"choice": "decline"}, headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Analytics disabled." in resp.get_data(as_text=True)

    user = _user_row(dsn, USER_ID)
    assert user["analytics_consent"] is False
    assert user["analytics_consent_updated_at"] is not None  # distinguishes from "never asked"

    rows = _events_rows(dsn, USER_ID, "consent_recorded")
    assert len(rows) == 1
    assert rows[0]["payload"]["granted"] is False


def test_post_consent_hx_request_gets_bare_panel_fragment_with_confirmation(app):
    """issue #182 + issue #173's HX convention: an htmx-driven grant/decline
    gets just the swappable #consent-panel fragment (never a full <html>
    document swapped into that small target), carrying the SAME inline
    confirmation banner the full-page response shows."""
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    resp = client.post("/consent", data={"choice": "grant"}, headers={"HX-Request": "true"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "<html" not in html
    assert 'id="consent-panel"' in html
    assert "Analytics enabled." in html


def test_post_consent_direct_request_redirects_then_shows_the_choice(app):
    """Post/Redirect/Get for the non-HX (no-JS) path (refuter-1): a direct
    POST no longer re-renders the full page in place -- it 303s to GET
    /consent so a refresh/back replays the safe GET instead of the browser
    re-submitting the form (and writing a second consent_recorded event).
    The choice is still visible after the round-trip -- never silently
    discarded the way the pre-#182 redirect-to-feed was -- as "Current
    choice: allowed.", the same line the HX path's confirmation banner sits
    above."""
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    resp = client.post("/consent", data={"choice": "grant"})
    assert resp.status_code == 303
    assert resp.headers["Location"].rstrip("/").endswith("/consent")

    follow = client.get("/consent")
    html = follow.get_data(as_text=True)
    assert "<html" in html
    assert "Current choice: allowed." in html

    user = _user_row(dsn, USER_ID)
    assert user["analytics_consent"] is True
    assert len(_events_rows(dsn, USER_ID, "consent_recorded")) == 1


def test_consent_payload_carries_all_four_allowlisted_keys(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    client.post("/consent", data={"choice": "grant"})

    payload = _events_rows(dsn, USER_ID, "consent_recorded")[0]["payload"]
    assert payload["consent_type"] == "analytics"
    assert payload["consent_version"] == "v1"
    assert "granted" in payload
    assert "consented_at" in payload


def test_consented_at_equals_the_column_written_by_sql_now(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    client.post("/consent", data={"choice": "grant"})

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            """SELECT (e.payload->>'consented_at') AS from_payload,
                      to_char(u.analytics_consent_updated_at AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS from_column
               FROM users u JOIN events e ON e.user_id = u.id
               WHERE u.id = %s AND e.event_type = 'consent_recorded' """,
            (USER_ID,),
        ).fetchone()

    assert row["from_payload"] == row["from_column"]


def test_unknown_choice_value_writes_nothing_and_returns_400(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    resp = client.post("/consent", data={"choice": "maybe-later"})
    assert resp.status_code == 400

    user = _user_row(dsn, USER_ID)
    assert user["analytics_consent"] is False
    assert user["analytics_consent_updated_at"] is None
    assert _events_rows(dsn, USER_ID, "consent_recorded") == []


def test_never_chosen_is_distinguishable_from_declined(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        assert _events.read_consent_choice_made(conn, USER_ID, current_version="v1") is False

    client.post("/consent", data={"choice": "decline"})

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        assert _events.read_consent_choice_made(conn, USER_ID, current_version="v1") is True


def test_consent_post_stays_401_when_signed_out(app):
    """POST /consent -- the actual mutation -- stays fully gated (issue
    #171 is explicit: consent is an account-level, authed-only decision;
    only the read-only explanatory GET view opens up)."""
    from jobcannon.web import PUBLIC_PATHS

    assert "/consent" not in PUBLIC_PATHS

    app.config["VERIFY_REQUEST"] = lambda req: None
    client = app.test_client()

    assert client.post("/consent", data={"choice": "grant"}).status_code == 401


def test_consent_get_renders_signed_out_variant_instead_of_401(app):
    """issue #171: the footer's "Analytics preferences" link is rendered
    on every page regardless of auth state, so a signed-out visitor
    clicking it must not land on the generic 401 gate -- GET /consent is
    marked @public_get and renders consent_signed_out.html instead, with
    working sign-in/sign-up links sourced from the same HOST_CONFIG
    fields issue #145 wired up everywhere else."""
    app.config["VERIFY_REQUEST"] = lambda req: None
    client = app.test_client()

    resp = client.get("/consent")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Analytics preferences" in html
    assert 'href="https://clerk.test/sign-in"' in html
    assert 'href="https://clerk.test/sign-up"' in html


def test_consent_trailing_slash_behaves_the_same_as_get_consent_when_signed_out(app):
    """devin lead: consent.py registers /consent with strict_slashes=False,
    and public_get's marker is keyed to the view function (issue #171), not
    the path string, so GET /consent/ must render the same signed-out
    variant as GET /consent -- not a 401, not a redirect loop."""
    app.config["VERIFY_REQUEST"] = lambda req: None
    client = app.test_client()

    resp = client.get("/consent/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Analytics preferences" in html


def test_consent_options_is_exempted_same_as_get_when_signed_out(app):
    """`_is_auth_optional_for_method` covers OPTIONS alongside GET/HEAD, not
    just "GET only" -- Flask's automatic OPTIONS responder answers this
    itself in dispatch_request without ever calling get_consent(), so if
    the gate aborted 401 first, a signed-out `OPTIONS /consent` would 401
    on a route that serves GET as 200: the same routing-vs-auth-layer
    confusion issue #173 exists to fix, one layer down. Pins the decision
    (not merely absence-of-401) by also checking the Allow header Flask's
    default responder fills in."""
    app.config["VERIFY_REQUEST"] = lambda req: None
    client = app.test_client()

    resp = client.options("/consent")

    assert resp.status_code == 200
    assert "GET" in resp.headers["Allow"]
    assert "POST" in resp.headers["Allow"]


def test_no_python_wallclock_in_the_consent_route():
    """consented_at must come from the database's own clock (db_now_iso),
    never a process wall-clock call. Covers both modules this PR adds that
    touch consent — consent.py (the route) and handoff.py (which reads
    consent state but never computes a timestamp of its own)."""
    import pathlib

    for path in ("jobcannon/web/consent.py", "jobcannon/web/handoff.py"):
        src = pathlib.Path(path).read_text(encoding="utf-8")
        assert "datetime.now(" not in src, path
        assert "utcnow(" not in src, path

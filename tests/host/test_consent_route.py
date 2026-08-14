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
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    resp = client.post("/consent", data={"choice": "grant"})
    assert resp.status_code == 302

    user = _user_row(dsn, USER_ID)
    assert user["analytics_consent"] is True
    assert user["analytics_consent_updated_at"] is not None

    rows = _events_rows(dsn, USER_ID, "consent_recorded")
    assert len(rows) == 1


def test_post_consent_decline_is_a_real_producible_path(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn)

    resp = client.post("/consent", data={"choice": "decline"})
    assert resp.status_code == 302

    user = _user_row(dsn, USER_ID)
    assert user["analytics_consent"] is False
    assert user["analytics_consent_updated_at"] is not None  # distinguishes from "never asked"

    rows = _events_rows(dsn, USER_ID, "consent_recorded")
    assert len(rows) == 1
    assert rows[0]["payload"]["granted"] is False


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
        assert _events.read_consent_choice_made(conn, USER_ID) is False

    client.post("/consent", data={"choice": "decline"})

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        assert _events.read_consent_choice_made(conn, USER_ID) is True


def test_consent_route_is_authed_only(app):
    from jobcannon.web import PUBLIC_PATHS

    assert "/consent" not in PUBLIC_PATHS

    app.config["VERIFY_REQUEST"] = lambda req: None
    client = app.test_client()

    assert client.get("/consent").status_code == 401
    assert client.post("/consent", data={"choice": "grant"}).status_code == 401


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

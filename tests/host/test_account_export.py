"""jobcannon/web/export.py — GET /account/export, the authed-only,
read-only self-service data-export route.

Own throwaway database, the same shape as tests/host/test_consent_route.py:
this route reads real, durable rows across profiles/watchlists/
pipeline_status/events/users, so it cannot share the session-scoped
`postgres_test_dsn` every rollback-isolated tests/host/ module reads inside
a transaction.

The cross-user-leakage test (`test_export_contains_only_the_requesting_users_
own_rows`) seeds TWO users against the SAME posting id on purpose: if the
route's `WHERE user_id = %s` filter were ever dropped, both users' watchlist/
pipeline_status rows would be indistinguishable by posting_id alone (they'd
all point at the one shared posting), so only a row-COUNT assertion would
catch the regression — an identity-based assertion (e.g. "no foreign
posting_id present") would pass for the wrong reason under that seeding.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

USER_A = "user_export_a"
USER_B = "user_export_b"


@pytest.fixture()
def app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_export_route")
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


def _seeded_client(app, dsn, user_id):
    """An authed test client whose user row already exists and whose handoff
    has already run (mirrors tests/host/test_consent_route.py's identical
    helper) — every request it makes lands directly on the route under
    test, not on the one-time post-handoff redirect."""
    from jobcannon.web.handoff import _HANDOFF_DONE_KEY

    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
        conn.commit()

    _authed(app, user_id)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    return client


def _shared_posting(dsn) -> int:
    """One company + one posting, shared by both seeded users below — the
    adversarial shape the leakage test needs (see module docstring)."""
    with psycopg.connect(dsn) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES ('Shared Co') RETURNING id"
        ).fetchone()[0]
        posting_id = conn.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES ('shared-dedup', %s, 'Engineer', 'Shared Co') RETURNING id",
            (company_id,),
        ).fetchone()[0]
        conn.commit()
        return posting_id


def _seed_full_account(dsn, user_id, posting_id) -> None:
    """A profile (with a numeric `years_of_experience`, so the Decimal ->
    JSON path is actually exercised), a watchlist entry, a pipeline_status
    row, a consent grant, and one extra plain event — all against the SAME
    `posting_id` every other seeded user in this module also uses."""
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
        conn.execute(
            "INSERT INTO profiles (user_id, experience_summary, years_of_experience) "
            "VALUES (%s, %s, %s)",
            (user_id, f"{user_id} summary", 4.5),
        )
        conn.execute(
            "INSERT INTO watchlists (user_id, posting_id) VALUES (%s, %s)",
            (user_id, posting_id),
        )
        conn.execute(
            "INSERT INTO pipeline_status (user_id, posting_id, status) VALUES (%s, %s, 'applied')",
            (user_id, posting_id),
        )
        conn.execute(
            "INSERT INTO events (user_id, event_type, payload) VALUES (%s, 'consent_recorded', %s)",
            (
                user_id,
                json.dumps(
                    {
                        "consent_type": "analytics",
                        "granted": True,
                        "consent_version": "v1",
                        "consented_at": "2026-08-01T00:00:00.000000Z",
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO events (user_id, event_type, posting_id) VALUES (%s, 'posting_saved', %s)",
            (user_id, posting_id),
        )
        conn.commit()


def test_export_requires_auth(app):
    client = app.test_client()
    assert client.get("/account/export").status_code == 401


def test_export_returns_json_attachment_with_date_stamped_filename(app):
    dsn = app.config["_TEST_DSN"]
    posting_id = _shared_posting(dsn)
    _seed_full_account(dsn, USER_A, posting_id)
    client = _seeded_client(app, dsn, USER_A)

    resp = client.get("/account/export")

    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    disposition = resp.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="jobcannon-account-export-' in disposition
    assert disposition.strip().endswith('.json"')

    doc = resp.get_json()
    assert doc["schema_version"]
    assert doc["generated_at"]
    assert doc["user_id"] == USER_A


def test_export_contains_only_the_requesting_users_own_rows(app):
    """Load-bearing: two users, seeded against the identical posting id, so
    only a row-count assertion (not an identity comparison) can catch a
    dropped user_id filter. See module docstring."""
    dsn = app.config["_TEST_DSN"]
    posting_id = _shared_posting(dsn)
    _seed_full_account(dsn, USER_A, posting_id)
    _seed_full_account(dsn, USER_B, posting_id)
    client = _seeded_client(app, dsn, USER_A)

    doc = client.get("/account/export").get_json()

    assert doc["user_id"] == USER_A
    assert doc["profile"]["experience_summary"] == f"{USER_A} summary"
    assert doc["profile"]["years_of_experience"] == 4.5

    assert len(doc["watchlist"]) == 1
    assert doc["watchlist"][0]["posting_id"] == posting_id

    assert len(doc["pipeline_status"]) == 1
    assert doc["pipeline_status"][0]["posting_id"] == posting_id
    assert doc["pipeline_status"][0]["status"] == "applied"

    # Exactly A's two seeded events (consent_recorded + posting_saved) —
    # never B's, even though B's rows reference the identical posting_id.
    assert len(doc["events"]) == 2
    assert {e["event_type"] for e in doc["events"]} == {"consent_recorded", "posting_saved"}


def test_export_consent_record_carries_granted_version_and_timestamp(app):
    dsn = app.config["_TEST_DSN"]
    posting_id = _shared_posting(dsn)
    _seed_full_account(dsn, USER_A, posting_id)
    client = _seeded_client(app, dsn, USER_A)

    doc = client.get("/account/export").get_json()

    consent = doc["consent"]
    assert consent is not None
    assert consent["granted"] is True
    assert consent["consent_version"] == "v1"
    assert consent["consented_at"]


def test_export_for_user_with_no_data_still_produces_valid_document(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn, "user_export_empty")

    resp = client.get("/account/export")

    assert resp.status_code == 200
    doc = resp.get_json()
    assert doc["schema_version"]
    assert doc["generated_at"]
    assert doc["user_id"] == "user_export_empty"
    assert doc["profile"] is None
    assert doc["watchlist"] == []
    assert doc["pipeline_status"] == []
    assert doc["consent"] is None
    assert doc["events"] == []

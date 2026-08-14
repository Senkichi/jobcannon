"""Authed-feed impression logging (jobcannon/web/pages.py's `_log_impressions`)
and the save/dismiss/apply mutation surface (jobcannon/web/actions.py).

Own throwaway database, same shape as tests/host/test_feed_page.py: postings
and consent must be durably committed on a different connection than the
Flask app's pooled one. Every client bypasses the anon-to-authed handoff the
same way tests/host/test_feed_page.py does — presetting the session's
`_HANDOFF_DONE_KEY` marker directly — so a fresh authed request never gets
redirected to /consent before it reaches the route under test.

Consent is a precondition for every test that asserts an event exists: a
brand-new account is non-consenting by column default (m0004), and
posting_impression /
posting_saved / posting_dismissed / posting_apply_clicked are all outside
log_event's `_FIRST_PARTY_ALWAYS` set, so they are dropped entirely — no
Postgres row, no PostHog call — unless consent has been granted. `_grant`
below does that the sanctioned way: `record_consent` with `consented_at`
sourced from `db_now_iso` (never a Python-computed timestamp), committed on
its own connection BEFORE the request is issued (`_resolve_consent` reads on
its own pooled connection and will not see an uncommitted write).
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jobcannon.db._events import db_now_iso, record_consent
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

CLERK_ID = "user_feed_events_test"


@pytest.fixture()
def app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_feed_events")
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
        conn.execute(
            "INSERT INTO users (id, plan_tier) VALUES (%s, 'free') ON CONFLICT (id) DO NOTHING",
            (user_id,),
        )


def _grant_consent(dsn, user_id):
    """The sanctioned way to grant consent in a test: record_consent with a
    database-clock consented_at, on its own connection, committed before the
    request under test is issued — never a raw UPDATE users SET
    analytics_consent, which would put a second writer of that column in the
    tree the events single-writer guard is meant to keep clean. row_factory
    must be dict_row: db_now_iso reads its result by string key
    (jobcannon/db/_events.py), and a bare psycopg.connect() defaults to
    tuple rows."""
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        record_consent(
            conn,
            user_id=user_id,
            consent_type="analytics",
            granted=True,
            consent_version="v1",
            consented_at=db_now_iso(conn),
        )
        conn.commit()


def _seed_profile(dsn, user_id):
    from jobcannon.db._profiles import upsert_profile

    with psycopg.connect(dsn) as conn:
        upsert_profile(conn, user_id, skills=["python"])


def _feed_client(app, user_id=CLERK_ID, *, consent=False):
    dsn = app.config["_TEST_DSN"]
    _authed(app, user_id)
    _seed_user(dsn, user_id)
    _seed_profile(dsn, user_id)
    if consent:
        _grant_consent(dsn, user_id)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    return client


def _seed_company(dsn, name):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()[0]


def _seed_posting(
    dsn, dedup_key, company_id, *, title="Engineer", last_seen=None, source_urls=None
):
    columns = ["dedup_key", "company_id", "title", "company"]
    values = [dedup_key, company_id, title, "Feed Events Co"]
    if last_seen is not None:
        columns.append("last_seen")
        values.append(last_seen)
    if source_urls is not None:
        columns.append("source_urls")
        values.append(Jsonb(source_urls))
    placeholders = ", ".join(["%s"] * len(values))
    cols_sql = ", ".join(columns)
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            f"INSERT INTO postings ({cols_sql}) VALUES ({placeholders}) RETURNING id",
            values,
        ).fetchone()[0]


def _seed_feed_state(dsn, user_id, posting_id, ranker_version):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO feed_state (user_id, posting_id, rank_score, ranker_version, computed_at) "
            "VALUES (%s, %s, 0.5, %s, now())",
            (user_id, posting_id, ranker_version),
        )


def _events(dsn, event_type=None):
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        if event_type is None:
            return conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        return conn.execute(
            "SELECT * FROM events WHERE event_type = %s ORDER BY id", (event_type,)
        ).fetchall()


def test_rendering_n_rows_emits_n_impressions_with_positions_1_to_n(app):
    from datetime import datetime, timedelta, timezone

    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Impression Co")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    oldest = _seed_posting(dsn, "impr-old", company_id, title="Oldest Row", last_seen=base)
    middle = _seed_posting(
        dsn, "impr-mid", company_id, title="Middle Row", last_seen=base + timedelta(hours=1)
    )
    newest = _seed_posting(
        dsn, "impr-new", company_id, title="Newest Row", last_seen=base + timedelta(hours=2)
    )

    resp = client.get("/")
    assert resp.status_code == 200

    impressions = _events(dsn, "posting_impression")
    assert len(impressions) == 3
    assert [row["feed_position"] for row in impressions] == [1, 2, 3]
    # Default ordering is last_seen DESC -> newest first.
    assert [row["posting_id"] for row in impressions] == [newest, middle, oldest]


def test_non_consenting_user_sees_the_feed_and_emits_zero_events(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=False)
    company_id = _seed_company(dsn, "No Consent Co")
    _seed_posting(dsn, "no-consent-1", company_id, title="No Consent Row")

    html = client.get("/").get_data(as_text=True)

    assert "No Consent Row" in html
    assert "Your feed isn't wired up yet" not in html
    assert "No postings scanned yet" not in html
    assert _events(dsn) == []


def test_every_impression_carries_ranker_version(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Ranker Version Co")
    ranked_id = _seed_posting(dsn, "ranked-impr-1", company_id, title="Ranked Impression Row")
    unranked_id = _seed_posting(dsn, "unranked-impr-1", company_id, title="Unranked Impression Row")
    _seed_feed_state(dsn, CLERK_ID, ranked_id, "ranker-test-v9")

    resp = client.get("/")
    assert resp.status_code == 200

    impressions = {
        row["posting_id"]: row["ranker_version"] for row in _events(dsn, "posting_impression")
    }
    assert impressions[ranked_id] == "ranker-test-v9"
    assert impressions[unranked_id] == "unranked-v0"


def test_impression_payload_contains_only_surface(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Payload Co")
    _seed_posting(dsn, "payload-impr-1", company_id, title="Payload Row")

    resp = client.get("/")
    assert resp.status_code == 200

    impressions = _events(dsn, "posting_impression")
    assert len(impressions) == 1
    payload = impressions[0]["payload"]
    assert set(payload.keys()) == {"surface"}
    assert payload["surface"] == "feed"


def test_apply_destination_is_not_a_full_url(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Apply URL Co")
    posting_id = _seed_posting(
        dsn,
        "apply-url-1",
        company_id,
        title="Apply URL Row",
        source_urls=["https://boards.greenhouse.io/acme/jobs/123?utm_source=test"],
    )

    resp = client.post(f"/postings/{posting_id}/apply")
    assert resp.status_code == 200

    clicks = _events(dsn, "posting_apply_clicked")
    assert len(clicks) == 1
    destination = clicks[0]["payload"]["apply_destination"]
    assert "://" not in destination
    assert "?" not in destination
    assert destination == "boards.greenhouse.io"


def test_posting_with_no_usable_url_renders_degraded_apply_control(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "No URL Co")
    posting_id = _seed_posting(dsn, "no-url-1", company_id, title="No URL Row")

    html = client.get("/").get_data(as_text=True)
    assert "No URL Row" in html
    assert "Your feed isn't wired up yet" not in html
    assert "No postings scanned yet" not in html
    assert "data-apply-degraded" in html
    assert "data-action-apply>" not in html

    resp = client.post(f"/postings/{posting_id}/apply")
    assert resp.status_code == 200
    assert _events(dsn, "posting_apply_clicked") == []


def test_save_dismiss_apply_each_emit_their_allowlisted_event(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Allowlist Co")
    save_id = _seed_posting(dsn, "allow-save-1", company_id, title="Save Row")
    dismiss_id = _seed_posting(dsn, "allow-dismiss-1", company_id, title="Dismiss Row")
    apply_id = _seed_posting(
        dsn,
        "allow-apply-1",
        company_id,
        title="Apply Row",
        source_urls=["https://jobs.lever.co/acme/xyz"],
    )

    assert client.post(f"/postings/{save_id}/save").status_code == 200
    assert client.post(f"/postings/{dismiss_id}/dismiss").status_code == 200
    assert client.post(f"/postings/{apply_id}/apply").status_code == 200

    saved = _events(dsn, "posting_saved")
    dismissed = _events(dsn, "posting_dismissed")
    applied = _events(dsn, "posting_apply_clicked")
    assert len(saved) == 1 and saved[0]["posting_id"] == save_id and saved[0]["payload"] is None
    assert (
        len(dismissed) == 1
        and dismissed[0]["posting_id"] == dismiss_id
        and dismissed[0]["payload"] is None
    )
    assert len(applied) == 1 and applied[0]["posting_id"] == apply_id
    assert set(applied[0]["payload"].keys()) == {"apply_destination"}


def test_mutations_persist_per_user(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Per User Co")
    posting_id = _seed_posting(dsn, "per-user-1", company_id, title="Per User Row")

    client_a = _feed_client(app, user_id="user_a_actions", consent=True)
    assert client_a.post(f"/postings/{posting_id}/save").status_code == 200

    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        a_rows = conn.execute(
            "SELECT * FROM watchlists WHERE user_id = %s AND posting_id = %s",
            ("user_a_actions", posting_id),
        ).fetchall()
        b_rows = conn.execute(
            "SELECT * FROM watchlists WHERE user_id = %s AND posting_id = %s",
            ("user_b_actions", posting_id),
        ).fetchall()
    assert len(a_rows) == 1
    assert len(b_rows) == 0


def test_apply_on_nonexistent_posting_is_404_not_500(app):
    client = _feed_client(app, consent=True)
    resp = client.post("/postings/999999999/apply")
    assert resp.status_code == 404


def test_save_and_dismiss_on_nonexistent_posting_are_404_not_500(app):
    """save/dismiss write through the same FK (watchlists.posting_id /
    pipeline_status.posting_id both reference postings.id) as apply, so this
    covers the ForeignKeyViolation -> 404 path for the other two mutation
    routes rather than leaving it inferred from the apply case alone."""
    client = _feed_client(app, consent=True)
    assert client.post("/postings/999999999/save").status_code == 404
    assert client.post("/postings/999999999/dismiss").status_code == 404


def test_save_is_idempotent_under_a_double_submit_via_the_route(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Double Submit Co")
    posting_id = _seed_posting(dsn, "double-submit-1", company_id, title="Double Submit Row")

    assert client.post(f"/postings/{posting_id}/save").status_code == 200
    assert client.post(f"/postings/{posting_id}/save").status_code == 200

    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute(
            "SELECT id FROM watchlists WHERE user_id = %s AND posting_id = %s",
            (CLERK_ID, posting_id),
        ).fetchall()
    assert len(rows) == 1

"""GET /postings (jobcannon/web/postings_history.py) — issue #180's in-app
saved/applied/dismissed review page.

Own throwaway database, same shape as tests/host/test_feed_events.py: the
watchlists/pipeline_status rows this page reads must be durably committed
on a different connection than the Flask app's pooled one.
"""

from __future__ import annotations

import psycopg
import pytest

from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

CLERK_ID = "user_postings_history_test"


@pytest.fixture()
def app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_postings_history")
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


def _seed_profile(dsn, user_id):
    from jobcannon.db._profiles import upsert_profile

    with psycopg.connect(dsn) as conn:
        upsert_profile(conn, user_id, skills=["python"])


def _client(app, user_id=CLERK_ID):
    dsn = app.config["_TEST_DSN"]
    _authed(app, user_id)
    _seed_user(dsn, user_id)
    _seed_profile(dsn, user_id)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    return client


def _seed_company(dsn, name):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()[0]


def _seed_posting(dsn, dedup_key, company_id, *, title="Engineer"):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES (%s, %s, %s, 'Postings History Co') RETURNING id",
            (dedup_key, company_id, title),
        ).fetchone()[0]


def test_saved_view_shows_only_saved_postings(app):
    dsn = app.config["_TEST_DSN"]
    client = _client(app)
    company_id = _seed_company(dsn, "Saved View Co")
    saved_id = _seed_posting(dsn, "history-saved-1", company_id, title="Saved Row")
    other_id = _seed_posting(dsn, "history-other-1", company_id, title="Untouched Row")
    assert client.post(f"/postings/{saved_id}/save").status_code == 200

    html = client.get("/postings?view=saved").get_data(as_text=True)

    assert "Saved Row" in html
    assert "Untouched Row" not in html
    assert other_id  # sanity: the untouched posting really was seeded


def test_applied_view_shows_only_applied_postings(app):
    dsn = app.config["_TEST_DSN"]
    client = _client(app)
    company_id = _seed_company(dsn, "Applied View Co")
    applied_id = _seed_posting(dsn, "history-applied-1", company_id, title="Applied Row")
    _seed_posting(dsn, "history-applied-other-1", company_id, title="Untouched Applied Row")
    assert client.post(f"/postings/{applied_id}/apply").status_code == 200

    html = client.get("/postings?view=applied").get_data(as_text=True)

    assert "Applied Row" in html
    assert "Untouched Applied Row" not in html


def test_dismissed_view_shows_only_dismissed_postings_that_the_feed_itself_excludes(app):
    """The point of list_postings_by_ids (jobcannon/db/_feed.py) over
    list_feed_postings: the feed's own authed branch unconditionally
    excludes status='dismissed' rows (test_feed_dal.py's
    test_dismissed_posting_is_excluded_...) -- this view must show that
    exact posting anyway, proving the review page's read path is not simply
    list_feed_postings with an id filter."""
    dsn = app.config["_TEST_DSN"]
    client = _client(app)
    company_id = _seed_company(dsn, "Dismissed View Co")
    dismissed_id = _seed_posting(dsn, "history-dismissed-1", company_id, title="Dismissed Row")
    assert client.post(f"/postings/{dismissed_id}/dismiss").status_code == 200

    # Sanity: the main feed excludes it.
    feed_html = client.get("/").get_data(as_text=True)
    assert "Dismissed Row" not in feed_html

    html = client.get("/postings?view=dismissed").get_data(as_text=True)
    assert "Dismissed Row" in html


def test_default_view_is_saved_when_view_param_missing(app):
    dsn = app.config["_TEST_DSN"]
    client = _client(app)
    company_id = _seed_company(dsn, "Default View Co")
    saved_id = _seed_posting(dsn, "history-default-1", company_id, title="Default View Row")
    assert client.post(f"/postings/{saved_id}/save").status_code == 200

    html = client.get("/postings").get_data(as_text=True)

    assert "Default View Row" in html
    assert 'data-postings-history-tab="saved"' in html
    assert 'aria-current="page"' in html


def test_unrecognized_view_degrades_to_saved_not_a_500(app):
    dsn = app.config["_TEST_DSN"]
    client = _client(app)
    company_id = _seed_company(dsn, "Bogus View Co")
    saved_id = _seed_posting(dsn, "history-bogus-1", company_id, title="Bogus View Row")
    assert client.post(f"/postings/{saved_id}/save").status_code == 200

    resp = client.get("/postings?view=not-a-real-view")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Bogus View Row" in html


@pytest.mark.parametrize(
    "view,expected_copy",
    [
        ("saved", "No saved postings yet."),
        ("applied", "No applied postings yet."),
        ("dismissed", "No dismissed postings yet."),
    ],
)
def test_empty_state_copy_is_specific_to_each_view(app, view, expected_copy):
    client = _client(app)

    html = client.get(f"/postings?view={view}").get_data(as_text=True)

    assert expected_copy in html
    assert "data-postings-history-empty" in html


def test_hx_request_returns_only_the_list_fragment(app):
    dsn = app.config["_TEST_DSN"]
    client = _client(app)
    company_id = _seed_company(dsn, "HX Fragment Co")
    saved_id = _seed_posting(dsn, "history-hx-1", company_id, title="HX Fragment Row")
    assert client.post(f"/postings/{saved_id}/save").status_code == 200

    resp = client.get("/postings?view=saved", headers={"HX-Request": "true"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "HX Fragment Row" in html
    assert "<h1" not in html
    assert "data-postings-history-tabs" not in html


def test_direct_browser_hit_returns_the_full_page(app):
    client = _client(app)

    html = client.get("/postings").get_data(as_text=True)

    assert "My postings" in html
    assert "data-postings-history-tabs" in html


def test_rows_render_read_only_with_no_mutation_controls(app):
    """#180's explicit design decision: this page never sets show_actions,
    so save/dismiss/apply/undo-apply never appear here even though the same
    _posting_row.html partial renders them on the authed feed -- see
    jobcannon/web/postings_history.py's module docstring for why (the
    _fetch_entry dismissed-exclusion hazard)."""
    dsn = app.config["_TEST_DSN"]
    client = _client(app)
    company_id = _seed_company(dsn, "Read Only Co")
    saved_id = _seed_posting(dsn, "history-readonly-1", company_id, title="Read Only Row")
    assert client.post(f"/postings/{saved_id}/save").status_code == 200

    html = client.get("/postings?view=saved").get_data(as_text=True)

    assert "Read Only Row" in html
    assert "data-action-save" not in html
    assert "data-action-dismiss" not in html
    assert "data-action-apply" not in html
    assert "data-action-undo-apply" not in html
    assert "data-posting-actions" not in html


def test_unauthenticated_request_gets_401(app):
    client = app.test_client()

    resp = client.get("/postings")

    assert resp.status_code == 401


def test_nav_link_present_when_authenticated(app):
    client = _client(app)

    html = client.get("/").get_data(as_text=True)

    assert 'href="/postings"' in html
    assert "My postings" in html


def test_nav_link_absent_when_unauthenticated(app):
    client = app.test_client()

    html = client.get("/demo").get_data(as_text=True)

    assert "My postings" not in html


def test_postings_history_page_does_not_500_when_pipeline_row_carries_no_posting(app):
    """Regression guard for the watchlists posting_id-vs-company_id split
    (jobcannon/web/postings_history.py's `_read_entries`): a watchlist row
    that saved a COMPANY rather than a posting must be silently skipped
    (never crash the `list_postings_by_ids` call with a None in the id
    list)."""
    dsn = app.config["_TEST_DSN"]
    client = _client(app)
    company_id = _seed_company(dsn, "Company Only Co")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO watchlists (user_id, company_id, created_at) VALUES (%s, %s, now())",
            (CLERK_ID, company_id),
        )

    resp = client.get("/postings?view=saved")

    assert resp.status_code == 200
    assert "No saved postings yet." in resp.get_data(as_text=True)

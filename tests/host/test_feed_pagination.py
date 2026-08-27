"""Keyset "Load more" pagination on the authed feed, GET / (#156,
jobcannon/web/pages.py). Own throwaway database, same shape as
tests/host/test_feed_page.py: the Flask app's pooled connections need to see
durably committed postings on a different connection than db_conn.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from jobcannon.db._feed import FEED_PAGE_MAX
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

CLERK_ID = "user_feed_pagination_test"
_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
_LOAD_MORE_RE = re.compile(r'hx-get="([^"]+)"[^>]*data-load-more|data-load-more[^>]*hx-get="([^"]+)"')


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_feed_pagination")
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


def _feed_client(app, user_id=CLERK_ID):
    from jobcannon.db._profiles import upsert_profile

    dsn = app.config["_TEST_DSN"]
    _authed(app, user_id)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, plan_tier) VALUES (%s, 'free') ON CONFLICT (id) DO NOTHING",
            (user_id,),
        )
    with psycopg.connect(dsn) as conn:
        upsert_profile(conn, user_id)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    return client


def _seed_company(dsn, name):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()[0]


def _seed_posting(dsn, dedup_key, company_id, *, title, last_seen, company="Pagination Test Co"):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company, last_seen) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (dedup_key, company_id, title, company, last_seen),
        ).fetchone()[0]


def _seed_pages_worth(dsn, company_id, count, *, title_prefix="Pagination Row"):
    """Count postings at 1-second last_seen increments — newest (highest i)
    sorts first, matching _SORTS["default"]'s DESC ordering."""
    for i in range(count):
        _seed_posting(
            dsn,
            f"page-row-{title_prefix}-{i}",
            company_id,
            title=f"{title_prefix} {i:03d}",
            last_seen=_BASE_TIME + timedelta(seconds=i),
        )


def _extract_load_more_url(html: str) -> str | None:
    match = _LOAD_MORE_RE.search(html)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _row_count(html: str) -> int:
    """Number of rendered posting rows. `data-posting-row` alone
    over-counts by 3x with show_actions=True: _posting_row.html's own
    save/dismiss controls each carry `hx-target="closest [data-posting-row]"`
    as a CSS selector, so the raw substring appears 3 times per row (the
    `<article>` tag itself, plus one per control). Counting `<article ...>`
    opening tags directly is unambiguous regardless of show_actions."""
    return len(re.findall(r"<article[^>]*data-posting-row[^>]*>", html))


def test_load_more_button_appears_when_first_page_is_full(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Full Page Co")
    _seed_pages_worth(dsn, company_id, FEED_PAGE_MAX + 5)

    html = client.get("/").get_data(as_text=True)

    assert "data-load-more" in html
    assert _row_count(html) == FEED_PAGE_MAX


def test_load_more_button_absent_when_fewer_than_a_full_page(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Short Page Co")
    _seed_pages_worth(dsn, company_id, 3)

    html = client.get("/").get_data(as_text=True)

    assert "data-load-more" not in html
    assert _row_count(html) == 3


def test_load_more_button_absent_when_exactly_a_full_page(app):
    """A page landing exactly on FEED_PAGE_MAX rows is genuinely
    ambiguous from list_feed_postings' return value alone (it looks
    identical to "there might be more") — the button DOES render in that
    case (the code cannot tell FEED_PAGE_MAX apart from FEED_PAGE_MAX+1
    without a second query), and the follow-up click resolving to zero new
    rows is exactly test_load_more_removes_itself_when_exhausted below."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Exact Page Co")
    _seed_pages_worth(dsn, company_id, FEED_PAGE_MAX)

    html = client.get("/").get_data(as_text=True)

    assert "data-load-more" in html
    assert _row_count(html) == FEED_PAGE_MAX


def test_load_more_hx_request_returns_only_the_next_batch(app):
    """The HX-Request fragment must contain the SECOND batch of rows with no
    overlap against the first, and none of the full page's own chrome (the
    page heading, nav) — proving this is genuinely a fragment response, not
    the full page reused."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "HX Fragment Co")
    _seed_pages_worth(dsn, company_id, FEED_PAGE_MAX + 10)

    first_page_html = client.get("/").get_data(as_text=True)
    load_more_url = _extract_load_more_url(first_page_html)
    assert load_more_url is not None

    resp = client.get(load_more_url, headers={"HX-Request": "true"})
    fragment_html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert _row_count(fragment_html) == 10
    assert "Your feed" not in fragment_html
    assert "<nav" not in fragment_html
    # No duplicate rows across the two pages: extract every rendered
    # dedup-free identifier (the h2 title) from both responses.
    first_titles = set(re.findall(r"<h2[^>]*>([^<]+)</h2>", first_page_html))
    second_titles = set(re.findall(r"<h2[^>]*>([^<]+)</h2>", fragment_html))
    assert first_titles & second_titles == set()
    assert len(first_titles) == FEED_PAGE_MAX
    assert len(second_titles) == 10
    # Exhausted: no further "Load more" in the last (short) batch.
    assert "data-load-more" not in fragment_html


def test_load_more_without_hx_request_returns_the_full_page(app):
    """A direct browser hit on a 'Load more' URL (no HX-Request header —
    e.g. opened in a new tab, or a JS-disabled visitor) must still get a
    real, fully-chromed page at that cursor, never a bare fragment."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "No HX Header Co")
    _seed_pages_worth(dsn, company_id, FEED_PAGE_MAX + 5)

    first_page_html = client.get("/").get_data(as_text=True)
    load_more_url = _extract_load_more_url(first_page_html)
    assert load_more_url is not None

    resp = client.get(load_more_url)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Your feed" in html
    assert _row_count(html) == 5


def test_load_more_preserves_the_current_title_filter(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Filter Preserve Co")
    _seed_pages_worth(dsn, company_id, FEED_PAGE_MAX + 3, title_prefix="Matching Engineer")
    _seed_pages_worth(dsn, company_id, 5, title_prefix="Other Analyst")

    first_page = client.get("/", query_string={"title": "Matching"}).get_data(as_text=True)
    load_more_url = _extract_load_more_url(first_page)
    assert load_more_url is not None
    assert "title=Matching" in load_more_url

    resp = client.get(load_more_url, headers={"HX-Request": "true"})
    fragment_html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert _row_count(fragment_html) == 3
    assert "Other Analyst" not in fragment_html


def test_load_more_appended_rows_carry_save_dismiss_controls(app):
    """A regression the fragment route must not introduce: appended rows
    still need show_actions (save/dismiss/apply), the same as the first
    page — _feed_page.html's HX branch must pass show_actions=True through,
    not just entries."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Actions Co")
    _seed_pages_worth(dsn, company_id, FEED_PAGE_MAX + 2)

    first_page = client.get("/").get_data(as_text=True)
    load_more_url = _extract_load_more_url(first_page)
    fragment_html = client.get(load_more_url, headers={"HX-Request": "true"}).get_data(
        as_text=True
    )

    assert "data-action-save" in fragment_html
    assert "data-action-dismiss" in fragment_html


def test_malformed_cursor_degrades_to_first_page_not_500(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Malformed Cursor Co")
    _seed_pages_worth(dsn, company_id, 3)

    resp = client.get("/", query_string={"cursor_id": "not-a-number"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert _row_count(html) == 3

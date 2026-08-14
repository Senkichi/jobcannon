"""Designed empty states, end to end, plus the HTML 401 error page.

Four rendering states share `_feed_list.html` / `_posting_row.html` (or a
sibling page template): corpus-empty, zero-match, a row present but its
`structural_axes` still NULL ("signals still computing"), and /preview's
no-picker-selections prompt (already covered by tests/host/test_preview.py,
not repeated here). This module covers the first three (the pending-marker
case on both of its consuming routes, `/` and `/preview`, since only `/`
had any prior fallback for it) plus the standalone 401 page, following
tests/host/test_feed_page.py's fixture shape (own throwaway database,
positive control on every populated-render assertion — see that module's
docstring for why open_pool must happen explicitly).

No module-level `pytestmark`: the two 401-page tests build their own
`create_app` and need no database, so `@requires_postgres` is applied per
test instead — see the `app` fixture's docstring for why a `skipif` mark on
the fixture itself would silently not skip anything.
"""

from __future__ import annotations

import types

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jobcannon.db._profiles import upsert_profile
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

CLERK_ID = "user_empty_states_test"


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring. NOT module-level
    `pytestmark`-gated: `requires_postgres` is a `pytest.mark.skipif`, which
    has no effect when applied to a fixture function (only test-item marks
    are evaluated for skip), so every test that uses this fixture instead
    carries `@requires_postgres` directly. The two 401-page tests below
    build their own `create_app` and need no database at all — a
    module-level mark would gate the PR's headline feature behind
    POSTGRES_ADMIN_DSN for no reason."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_empty_states")
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


def _seed_profile(dsn, user_id, **kwargs):
    with psycopg.connect(dsn) as conn:
        upsert_profile(conn, user_id, **kwargs)


def _feed_client(app, user_id=CLERK_ID, **profile_kwargs):
    """An authed test client past the handoff, with a real `users` row and a
    `profiles` row already committed — mirrors
    tests/host/test_feed_page.py::_feed_client (see that module's docstring
    for why the handoff itself is bypassed via the session marker rather
    than a throwaway priming request)."""
    dsn = app.config["_TEST_DSN"]
    _authed(app, user_id)
    _seed_user(dsn, user_id)
    _seed_profile(dsn, user_id, **profile_kwargs)
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
    dsn,
    dedup_key,
    company_id,
    *,
    title,
    company="Empty States Test Co",
    salary_min=None,
    structural_axes=None,
):
    columns = ["dedup_key", "company_id", "title", "company"]
    values = [dedup_key, company_id, title, company]
    if salary_min is not None:
        columns.append("salary_min")
        values.append(salary_min)
    if structural_axes is not None:
        columns.append("structural_axes")
        values.append(Jsonb(structural_axes))
    placeholders = ", ".join(["%s"] * len(values))
    cols_sql = ", ".join(columns)
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            f"INSERT INTO postings ({cols_sql}) VALUES ({placeholders}) RETURNING id",
            values,
        ).fetchone()[0]


@requires_postgres
def test_zero_match_profile_renders_zero_match_state(app):
    """A populated corpus plus filters that match nothing must render the
    zero-match copy — and must NOT render the corpus-empty copy, which is
    what an unopened pool (or any other "never actually queried" bug) would
    fail closed to instead. Both halves of that assertion are required: the
    two states look similar enough in isolation that a test only checking
    the zero-match string's presence would also pass against a database that
    was never reachable at all.

    The seeded row's premise — a populated corpus — is itself checked with a
    positive control: an unfiltered request for the same client must show
    the seeded title. Without this, a broken filter that matched nothing
    REGARDLESS of the query string would make this test pass for the wrong
    reason (the row was never inserted / visible at all, not "filtered out
    by title")."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Zero Match Co")
    _seed_posting(dsn, "empty-zero-match-1", company_id, title="Unrelated Posting Title")

    # Positive control: the seeded row is really there and really visible.
    unfiltered_html = client.get("/").get_data(as_text=True)
    assert "Unrelated Posting Title" in unfiltered_html

    html = client.get("/", query_string={"title": "no-such-title-xyz"}).get_data(as_text=True)

    assert "No postings match your selections yet." in html
    assert "The corpus is warming up" not in html


@requires_postgres
def test_null_structural_axes_row_renders_pending_marker_not_hidden_and_not_faked(app):
    """The pending marker must appear for a NULL-`structural_axes` row EVEN
    WHEN a real, independent chip (salary) is also present — proving the
    marker is keyed on the axes column itself, not on the chip list being
    empty (the old, coarser behavior this replaces would have suppressed the
    marker here). No axis-derived label may appear, since none is stored."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Pending Axes Co")
    _seed_posting(
        dsn,
        "empty-pending-axes-1",
        company_id,
        title="Pending Axes Posting",
        salary_min=100000,
        # structural_axes intentionally omitted -> stays NULL.
    )

    html = client.get("/").get_data(as_text=True)

    assert "Pending Axes Posting" in html
    assert "salary listed" in html
    assert "signals still computing for this posting" in html
    # Never a fabricated stand-in for the missing axis values.
    assert "posted within the last week" not in html
    assert "level stated in title" not in html
    assert "JD looks complete" not in html


@requires_postgres
def test_preview_also_renders_the_pending_marker_for_a_null_axes_row(app):
    """`_posting_row.html` is shared by `/` and `/preview` (through
    `_feed_list.html`) so the pending-signal marker renders identically on
    both without either route's Python duplicating the check. This is the
    stronger claim of the two consuming routes: `jobcannon/web/onboarding.py`
    never had any pending-marker fallback before this change (unlike `/`,
    which had a coarser, chip-emptiness-driven one), so this is new coverage
    of new behavior on that route, not a re-check of an existing one. No
    picker submission or auth is needed — /preview renders the unfiltered
    live feed for a visitor with no pending selections.

    Same shape as test_null_structural_axes_row_renders_pending_marker_*
    above: salary_min is set so a real chip is present alongside the marker.
    Without it, a NULL axes + no-selections row also yields zero chips, and
    the assertion below would pass under EITHER a NULL-keyed condition or a
    chip-emptiness-keyed one — proving nothing about which one is wired.
    """
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview Pending Axes Co")
    _seed_posting(
        dsn,
        "empty-preview-pending-axes-1",
        company_id,
        title="Preview Pending Axes Posting",
        salary_min=100000,
        # structural_axes intentionally omitted -> stays NULL.
    )

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "Preview Pending Axes Posting" in html
    assert "salary listed" in html
    assert "signals still computing for this posting" in html


@requires_postgres
def test_empty_corpus_state_still_renders_for_authed_and_guest(app):
    """No postings seeded at all (the true corpus-empty state), checked on
    both the authed feed (profile present, so the no-profile branch cannot
    be what produced this copy) and the public guest demo."""
    client_authed = _feed_client(app)
    html_authed = client_authed.get("/").get_data(as_text=True)
    assert "The corpus is warming up" in html_authed
    assert "Your feed isn't wired up yet" not in html_authed

    html_guest = app.test_client().get("/demo").get_data(as_text=True)
    assert "The corpus is warming up" in html_guest


def test_unauthed_root_returns_401_with_html_body_and_start_link():
    """No HOST_CONFIG injected — this only passes if create_app's TESTING
    branch really does populate app.config["HOST_CONFIG"] on every path."""
    from jobcannon.web import create_app

    app = create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": lambda r: None,
            "WEBHOOK_SECRET": "whsec_dGVzdA==",
        }
    )
    resp = app.test_client().get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 401
    assert 'href="/start"' in html
    assert app.config["HOST_CONFIG"].clerk_sign_up_url in html


def test_401_page_renders_without_a_signup_link_when_clerk_sign_up_url_is_blank():
    """A config double that carries an empty clerk_sign_up_url must still
    render an HTML body with no 500 and no bare href="" anchor."""
    from jobcannon.web import create_app

    host_config = types.SimpleNamespace(clerk_sign_up_url="")
    app = create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": lambda r: None,
            "WEBHOOK_SECRET": "whsec_dGVzdA==",
            "HOST_CONFIG": host_config,
        }
    )
    resp = app.test_client().get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 401
    assert 'href=""' not in html
    assert "Sign-in required" in html

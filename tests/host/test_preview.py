"""Pre-signup preview feed: GET /preview (jobcannon/web/onboarding.py).

Own throwaway database, same shape as tests/host/test_onboarding.py: this
module seeds durable, committed postings/companies directly (the Flask
app's pooled connections need to see them — they are on a different
connection than the session-scoped, rollback-isolated db_conn fixture every
other tests/host/ module uses).
"""

from __future__ import annotations

import ast
import pathlib

import psycopg
import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

_ONBOARDING_MODULE_PATH = "jobcannon/web/onboarding.py"


def _seed_company(dsn: str, name: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()[0]


def _seed_posting(
    dsn: str,
    dedup_key: str,
    company_id: int,
    *,
    title: str,
    company: str = "Preview Test Co",
    workplace_type: str | None = None,
    location: str | None = None,
) -> int:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company, workplace_type, location) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (dedup_key, company_id, title, company, workplace_type, location),
        ).fetchone()[0]


def _events_count(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT count(*) FROM events").fetchone()[0]


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_preview")
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


def _set_pending_picker(client, **selections):
    with client.session_transaction() as sess:
        sess["pending_picker"] = {"anon_id": "anon_test", **selections}


def test_preview_renders_postings_unauthenticated(app):
    company_id = _seed_company(app.config["_TEST_DSN"], "Preview Positive Control Co")
    _seed_posting(
        app.config["_TEST_DSN"],
        "preview-positive-1",
        company_id,
        title="Distinctive Preview Posting Title",
    )

    resp = app.test_client().get("/preview")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Distinctive Preview Posting Title" in html
    # Positive control (standard-gate obligation 2): the route's own
    # zero-match copy must be ABSENT — present would mean the seeded row
    # never reached the page (e.g. a fixture that forgot open_pool would
    # fail closed to an empty list and this string would appear instead).
    assert "No postings match your selections yet." not in html


def test_preview_is_driven_only_by_picker_selections(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Selection Filter Co")
    _seed_posting(dsn, "preview-alpha", company_id, title="Distinctive Title Alpha")
    _seed_posting(dsn, "preview-beta", company_id, title="Distinctive Title Beta")

    client = app.test_client()
    _set_pending_picker(client, titles=["Distinctive Title Alpha"])
    html_alpha = client.get("/preview").get_data(as_text=True)
    assert "Distinctive Title Alpha" in html_alpha
    assert "Distinctive Title Beta" not in html_alpha

    _set_pending_picker(client, titles=["Distinctive Title Beta"])
    html_beta = client.get("/preview").get_data(as_text=True)
    assert "Distinctive Title Beta" in html_beta
    assert "Distinctive Title Alpha" not in html_beta


def test_preview_shows_honest_ordering_label_when_all_rows_unranked(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Honest Label Co")
    _seed_posting(dsn, "preview-honest-1", company_id, title="Unranked Posting")

    html = app.test_client().get("/preview").get_data(as_text=True)

    # The seeded row must actually reach the page — without this the
    # ordering-label assertions below hold just as well on an empty result
    # set (e.g. a fixture that forgot open_pool), making the "when all rows
    # unranked" premise in the test name untested.
    assert "Unranked Posting" in html
    assert "Sorted by recency" in html
    assert "personalized ranking is not live yet" in html
    assert "Ranked by" not in html


def test_preview_emits_no_events(app):
    """Two assertions, both required: every account starts non-consenting
    by column default, and /preview's g.consent_granted is hardcoded
    False, so log_event would silently drop a write even if the route
    called it. A bare zero-count
    proves only that nothing wrote to `events`, not that no code tried to —
    this module's zero and the AST call-site absence together prove BOTH
    that instrumentation was never attempted and that the count-based check
    itself is capable of detecting a write (the control)."""
    source = pathlib.Path(_ONBOARDING_MODULE_PATH).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            assert name != "log_event", "onboarding.py must not call log_event directly"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "jobcannon.host.events", "onboarding.py must not import events"
            assert all(alias.name != "log_event" for alias in node.names)
        if isinstance(node, ast.Import):
            assert all(alias.name != "jobcannon.host.events" for alias in node.names)

    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "No Events Co")
    _seed_posting(dsn, "preview-no-events-1", company_id, title="No Events Posting")

    app.test_client().get("/preview")
    assert _events_count(dsn) == 0

    from jobcannon.host.events import log_event

    log_event(
        "consent_recorded",
        user_id=None,
        consent_granted=False,
        payload={"consent_type": "analytics", "granted": False, "consent_version": "v1"},
    )
    assert _events_count(dsn) == 1


def test_preview_without_picker_selections_renders_the_designed_prompt_not_a_500(app):
    resp = app.test_client().get("/preview")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "You haven't completed the picker yet" in html


def test_picker_submit_now_redirects_to_preview(app):
    client = app.test_client()
    resp = client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})

    assert resp.status_code in (302, 303)
    assert resp.headers["Location"].rstrip("/").endswith("/preview")

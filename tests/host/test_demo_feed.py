"""Public guest demo at GET /demo (jobcannon/web/pages.py): the guest
profile card plus a populated, filtered feed with literal why-chips,
unauthenticated.

Own throwaway database, same shape as tests/host/test_preview.py and
tests/host/test_feed_page.py: postings/users/profiles must be durably
committed on a different connection than the Flask app's pooled one.

The guest profile row (scripts/seed_guest_demo.py, jobcannon.db._profiles.
GUEST_USER_ID) is a manual operator step, and the live corpus additionally
depends on the corpus pre-seed (scripts/preseed_corpus.py) owner-gated
step — neither can be assumed to have run against any given deploy, so every
test here seeds its own company + postings + guest profile directly against
the throwaway database rather than depending on either step having executed.
"""

from __future__ import annotations

import ast
import pathlib

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jobcannon.db._profiles import GUEST_USER_ID, upsert_profile
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

_PAGES_MODULE_PATH = "jobcannon/web/pages.py"


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_demo_feed")
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


def _seed_guest_profile(dsn: str, **kwargs) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, plan_tier) VALUES (%s, 'free') ON CONFLICT (id) DO NOTHING",
            (GUEST_USER_ID,),
        )
    with psycopg.connect(dsn) as conn:
        upsert_profile(conn, GUEST_USER_ID, **kwargs)


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
    title: str = "Engineer",
    company: str = "Demo Feed Test Co",
    salary_min: int | None = None,
    structural_axes: dict | None = None,
) -> int:
    columns = ["dedup_key", "company_id", "title", "company", "salary_min"]
    values: list = [dedup_key, company_id, title, company, salary_min]
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


def _events_count(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT count(*) FROM events").fetchone()[0]


def test_demo_renders_guest_profile_card(app):
    dsn = app.config["_TEST_DSN"]
    _seed_guest_profile(
        dsn,
        target_titles=["Distinctive Guest Card Title"],
        skills=["distinctive-guest-skill"],
        seniority_level="senior",
        years_of_experience=6,
    )
    company_id = _seed_company(dsn, "Profile Card Co")
    _seed_posting(dsn, "demo-card-1", company_id, title="Distinctive Guest Card Title")

    html = app.test_client().get("/demo").get_data(as_text=True)

    assert "Seniority: senior" in html
    assert "6 years of experience" in html
    assert "distinctive-guest-skill" in html
    assert "Distinctive Guest Card Title" in html


def test_demo_renders_postings_with_why_chips_unauthenticated(app):
    dsn = app.config["_TEST_DSN"]
    _seed_guest_profile(dsn, target_titles=["Distinctive Demo Feed Title"])
    company_id = _seed_company(dsn, "Demo Feed Positive Control Co")
    _seed_posting(
        dsn,
        "demo-feed-positive-1",
        company_id,
        title="Distinctive Demo Feed Title",
        salary_min=100000,
    )

    resp = app.test_client().get("/demo")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Distinctive Demo Feed Title" in html
    assert "data-why-chips" in html
    assert "salary listed" in html
    # Positive control (standard-gate obligation 2): every empty-state
    # discriminator this route could plausibly fall back to must be ABSENT —
    # present would mean the seeded profile/postings never reached the page
    # (e.g. a fixture that forgot open_pool would fail closed to one of
    # these instead).
    assert "The corpus is warming up" not in html
    assert "The guest profile isn't seeded yet" not in html
    assert "The live corpus" not in html
    assert "No postings match your selections yet." not in html


def test_demo_titles_filter_is_exact_match_against_hand_authored_titles(app):
    """Documents a real characteristic of the current wiring, not a
    requirement this PR is asserting as correct: `_read_demo_feed_postings`
    passes the guest profile's `target_titles` straight into
    `list_feed_postings`'s `titles=` keyword, which jobcannon/db/_feed.py's
    own module docstring says is exact-match (`= ANY(%s)`) and is meant for
    corpus-derived strings (the picker's fixed options, sourced from
    distinct_titles(conn)) — not hand-authored ones. The guest profile's
    target_titles (scripts/seed_guest_demo.py) are hand-authored, so a real
    posting title that is a superset/variant of the canned string (a
    seniority prefix, a location or team suffix) will not match and the
    populated branch falls through to the same-corpus "no match" copy the
    authed feed already uses. Pinned here so this stays visible rather than
    silently assumed away; not fixed in this change because choosing a
    different match strategy is a filter-semantics decision beyond the
    read-only render this PR ships."""
    dsn = app.config["_TEST_DSN"]
    _seed_guest_profile(dsn, target_titles=["Product Data Scientist"])
    company_id = _seed_company(dsn, "Near Miss Co")
    _seed_posting(
        dsn, "demo-near-miss-1", company_id, title="Senior Product Data Scientist, Growth"
    )

    html = app.test_client().get("/demo").get_data(as_text=True)

    assert "Senior Product Data Scientist, Growth" not in html
    assert "No postings match your selections yet." in html


def test_demo_emits_no_events(app):
    """Two assertions, both required — same two-part structure as
    tests/host/test_preview.py::test_preview_emits_no_events, and for the
    same reason: /demo forces g.consent_granted = False
    (jobcannon/web/__init__.py) and every account defaults to
    analytics_consent = false, so a bare "zero events rows" assertion passes
    whether or not the route calls log_event and therefore tests nothing.

    The structural half is scoped to demo()'s own call graph, not the whole
    module: pages.py legitimately imports and calls log_event for the authed
    feed's per-row posting_impression logging, so a module-wide "never
    imports events" guard would outlaw the feed route's job. Instead this
    walks the transitive closure of module-local functions reachable from
    demo() (derived from the AST, so a new demo helper is covered
    automatically) and asserts none of them call log_event."""
    source = pathlib.Path(_PAGES_MODULE_PATH).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    def _called_names(fn: ast.FunctionDef) -> set[str]:
        names = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                callee = node.func
                name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
                if name is not None:
                    names.add(name)
        return names

    reachable: set[str] = set()
    frontier = ["demo"]
    while frontier:
        fn_name = frontier.pop()
        if fn_name in reachable or fn_name not in module_functions:
            continue
        reachable.add(fn_name)
        frontier.extend(_called_names(module_functions[fn_name]))
    assert "demo" in reachable, "demo() not found in pages.py — guard is scanning nothing"
    for fn_name in sorted(reachable):
        assert "log_event" not in _called_names(module_functions[fn_name]), (
            f"{fn_name}() is reachable from demo() and calls log_event — /demo must not emit events"
        )

    dsn = app.config["_TEST_DSN"]
    _seed_guest_profile(dsn, target_titles=["No Events Demo Title"])
    company_id = _seed_company(dsn, "No Events Demo Co")
    _seed_posting(dsn, "demo-no-events-1", company_id, title="No Events Demo Title")

    app.test_client().get("/demo")
    assert _events_count(dsn) == 0

    from jobcannon.host.events import log_event

    log_event(
        "consent_recorded",
        user_id=None,
        consent_granted=False,
        payload={"consent_type": "analytics", "granted": False, "consent_version": "v1"},
    )
    assert _events_count(dsn) == 1


def test_demo_without_guest_seed_renders_empty_state_not_500(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Unseeded Guest Co")
    _seed_posting(dsn, "demo-unseeded-1", company_id, title="Unseeded Guest Posting")

    resp = app.test_client().get("/demo")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "The guest profile isn't seeded yet" in html
    assert "Unseeded Guest Posting" not in html


def test_demo_requires_no_cookie_and_no_signup(app):
    """A brand-new client — no prior session cookie, no Clerk credentials —
    still gets the fully populated page on its very first request: /demo
    depends on nothing carried by the visitor (unlike GET /preview, which
    reads a `pending_picker` selection the session may hold), only on the
    guest profile and corpus state already committed in the database."""
    dsn = app.config["_TEST_DSN"]
    _seed_guest_profile(dsn, target_titles=["No Cookie Demo Title"])
    company_id = _seed_company(dsn, "No Cookie Demo Co")
    _seed_posting(dsn, "demo-no-cookie-1", company_id, title="No Cookie Demo Title")

    client = app.test_client()
    assert not client.get_cookie("session")

    resp = client.get("/demo")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "No Cookie Demo Title" in html
    # Positive control: the profile card alone also renders the target
    # title, so a feed-row discriminator is required to prove the posting
    # itself reached the page rather than the empty-state fallback.
    assert "data-why-chips" in html
    assert "No postings match your selections yet." not in html

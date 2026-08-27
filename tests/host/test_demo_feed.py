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
from jobcannon.web.auth import ClerkIdentity
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


# ---------------------------------------------------------------------------
# /start + sign-up CTA (issue #182, item 3): /demo previously had no path
# onward to /start or a Clerk account. demo.html renders the /start half of
# the CTA unconditionally, ahead of the corpus-empty/unseeded-profile/
# populated branch chain; the "sign up" half of both the demo CTA and the
# per-row CTA gate on signup_cta_url -- the single, identity-derived value
# jobcannon.web's _inject_auth_links context processor computes (same
# sign-up-preferred fallback as /preview's page-level (#145) and per-row
# (#174) CTAs, but None for an authed visitor regardless of what's
# configured). The per-row "Sign up to apply" CTA on a seeded posting is
# #174's own deliverable, exercised here end-to-end on the /demo route (the
# tests/host/test_preview.py module owns the /preview route's coverage);
# test_demo_hides_signup_cta_for_an_authed_visitor below is the negative
# control proving both CTA halves respect real identity, not just
# show_actions.
# ---------------------------------------------------------------------------


def test_demo_shows_start_and_row_signup_cta_on_populated_feed(app):
    """Positive control: this module's `app` fixture never overrides
    HOST_CONFIG, so TESTING's default configures BOTH clerk_sign_up_url
    and clerk_sign_in_url (jobcannon/web/__init__.py) -- both the
    page-level /start CTA and the per-row CTA on the seeded posting must
    render, sign-up winning the `or` fallback."""
    dsn = app.config["_TEST_DSN"]
    _seed_guest_profile(dsn, target_titles=["Demo CTA Title"])
    company_id = _seed_company(dsn, "Demo CTA Co")
    _seed_posting(dsn, "demo-cta-1", company_id, title="Demo CTA Title")

    html = app.test_client().get("/demo").get_data(as_text=True)

    assert "Demo CTA Title" in html
    assert "data-demo-cta" in html
    assert 'href="/start"' in html
    assert "Build your own feed" in html
    assert "data-action-signup" in html
    assert "Sign up to apply" in html
    assert 'href="https://clerk.test/sign-up"' in html


def test_demo_start_cta_present_when_corpus_empty(app):
    """The CTA sits ahead of the if/elif/else branch chain in demo.html,
    so it must survive even the corpus-empty state (no corpus pre-seed
    run yet against this deploy) -- the exact state the design direction
    calls out as needing a clear path to /start regardless."""
    html = app.test_client().get("/demo").get_data(as_text=True)

    assert "The corpus is warming up" in html
    assert "data-demo-cta" in html
    assert 'href="/start"' in html


def test_demo_start_cta_present_when_guest_profile_unseeded(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Demo CTA Unseeded Co")
    _seed_posting(dsn, "demo-cta-unseeded-1", company_id, title="Demo CTA Unseeded Posting")

    html = app.test_client().get("/demo").get_data(as_text=True)

    assert "The guest profile isn't seeded yet" in html
    assert "data-demo-cta" in html
    assert 'href="/start"' in html


def test_demo_hides_signup_cta_for_an_authed_visitor(app):
    """Issue #174 fix: /demo has no _current_identity() check and never
    passed show_actions, so before this fix an authed visitor still saw
    the anonymous "Sign up to apply" row CTA and the demo CTA's own "sign
    up" nudge -- the gate (formerly `not show_actions`, Undefined-is-falsy)
    never actually checked identity. /demo keeps serving authed visitors
    (it's a public showcase, so it stays in PUBLIC_PATHS and is never
    redirected the way /preview is) -- just without either signup CTA.

    Mutates VERIFY_REQUEST post-creation, same pattern as
    tests/host/test_feed_events.py's `_authed()` helper: /demo is a
    PUBLIC_PATHS route, so before_request's clerk_auth() sets g.clerk_user
    = None unconditionally without ever calling VERIFY_REQUEST -- the
    identity signal only reaches the page via
    jobcannon.web._visitor_is_anonymous()'s fallback to
    onboarding._current_identity(), which reads VERIFY_REQUEST fresh from
    app.config on every call, exactly like /preview's own authed-redirect
    check does.

    Assertions are scoped to the two signup_cta_url-gated blocks
    (data-action-signup / the demo CTA's "sign up" fragment), not a bare
    "sign-up href not anywhere on the page" check: base.html's header nav
    (issue #145) is gated on `not g.clerk_user`, a DIFFERENT, pre-existing
    mechanism this PR does not touch -- and g.clerk_user is force-None on
    every PUBLIC_PATHS render regardless of real identity, so the header
    nav renders its own sign-up link on /demo/ /preview unconditionally.
    That's an existing, out-of-scope gap (not introduced or widened by
    #174), flagged separately rather than asserted against here."""
    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(
        user_id="user_authed_demo", claims={"sub": "user_authed_demo"}
    )
    dsn = app.config["_TEST_DSN"]
    _seed_guest_profile(dsn, target_titles=["Authed Demo Title"])
    company_id = _seed_company(dsn, "Authed Demo Co")
    _seed_posting(dsn, "demo-authed-1", company_id, title="Authed Demo Title")

    resp = app.test_client().get("/demo")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    # Positive control: the authed visitor still gets the real populated
    # page, not an empty state or a 401 -- proving the absences below mean
    # "CTA correctly hidden," not "page never rendered."
    assert "Authed Demo Title" in html
    assert "data-why-chips" in html
    assert 'href="/start"' in html
    assert "Build your own feed" in html
    assert "data-action-signup" not in html
    assert "data-posting-signup" not in html
    assert "Sign up to apply" not in html
    assert "sign up</a> to save it" not in html


def test_demo_row_omits_signup_cta_when_both_urls_unset(app):
    """Tolerant-default floor, mirroring
    test_preview.py::test_preview_omits_signup_cta_when_both_urls_unset:
    both Clerk URLs unset must drop the per-row CTA and the demo CTA's own
    sign-up fragment, while /start (a plain internal link, never gated on
    either URL) keeps rendering."""
    from jobcannon.host.config import HostConfig

    dsn = app.config["_TEST_DSN"]
    _seed_guest_profile(dsn, target_titles=["Demo No CTA Title"])
    company_id = _seed_company(dsn, "Demo No CTA Co")
    _seed_posting(dsn, "demo-no-cta-1", company_id, title="Demo No CTA Title")

    app.config["HOST_CONFIG"] = HostConfig(database_url="", secret_key="testing-secret-key")

    html = app.test_client().get("/demo").get_data(as_text=True)

    assert "Demo No CTA Title" in html
    assert 'href="/start"' in html
    assert "data-action-signup" not in html
    assert "Sign up to apply" not in html
    assert "sign up</a> to save it" not in html
    assert 'href=""' not in html

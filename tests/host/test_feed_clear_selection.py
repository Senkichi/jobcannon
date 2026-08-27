"""#206: the saved-selection AND-collision fix's discoverability/
recoverability surface on the authed feed (jobcannon/web/pages.py) —

  * `_saved_selection_indicator`: the "Filtering to your saved picks (N
    titles / M companies)" banner + Clear control, rendered above the
    filter form ONLY when the profile has a non-empty saved titles/
    companies selection.
  * `POST /feed/clear-selection`: zeroes `profiles.target_titles`/
    `target_companies` (via `upsert_profile`'s literal-empty-list COALESCE
    behavior, #169) while preserving every other profile field, HX-aware
    (200 fragment vs. 303 redirect), CSRF-protected and auth-gated like
    every other mutation route in this app.
  * `_feed_empty_reason`: differentiated empty-state copy — "collision"
    (a saved selection AND a free-text search this render both apply, and
    together they zeroed the result set) vs. the pre-#206 flat "empty" copy
    for every other zero-result cause.

Own throwaway database, same shape as tests/host/test_feed_page.py /
tests/host/test_feed_pagination.py: postings/profiles must be durably
committed on a different connection than the Flask app's pooled one. The
anon-to-authed handoff (jobcannon/web/handoff.py) is bypassed the same way
test_feed_page.py's module docstring explains — this module seeds the
`users` row directly and presets the session handoff marker rather than
exercising the redirect.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.rows import dict_row

from jobcannon.db._profiles import get_profile, upsert_profile
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

CLERK_ID = "user_feed_clear_selection_test"


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_feed_clear_selection")
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
    kwargs.setdefault("workplace_type", None)
    with psycopg.connect(dsn) as conn:
        upsert_profile(conn, user_id, **kwargs)


def _feed_client(app, user_id=CLERK_ID, **profile_kwargs):
    """An authed test client past the handoff, with a real `users` row and a
    `profiles` row already committed — see tests/host/test_feed_page.py's
    module docstring for why the handoff itself is bypassed here."""
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


def _seed_posting(dsn, dedup_key, company_id, *, title="Engineer", company="Clear Test Co"):
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (dedup_key, company_id, title, company),
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# Indicator: gated on a non-empty saved selection, counts from
# selection_filter_kwargs.
# ---------------------------------------------------------------------------


def test_indicator_renders_with_counts_for_saved_selection(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(
        app, target_titles=["Engineer", "Manager"], target_companies=["Indicator Co"]
    )
    company_id = _seed_company(dsn, "Indicator Co")
    _seed_posting(dsn, "clear-indicator-1", company_id, title="Engineer", company="Indicator Co")

    html = client.get("/").get_data(as_text=True)

    assert "Filtering to your saved picks (2 titles" in html
    assert "1 company)" in html
    assert "data-saved-selection-indicator" in html
    assert "data-clear-selection" in html


def test_indicator_absent_for_fresh_profile(app):
    """Positive control (verification-ladder): a fresh profile with no saved
    titles/companies must show neither the indicator nor a Clear control —
    the gate this test guards is `_saved_selection_indicator` returning None,
    not a zeroed/empty dict the template would still render truthy."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "No Selection Co")
    _seed_posting(dsn, "clear-no-selection-1", company_id, title="Any Title")

    html = client.get("/").get_data(as_text=True)

    # The seeded row must actually reach the page (positive control), or the
    # absence assertions below would hold just as well on the wrong branch
    # (e.g. the corpus-empty state, which also has no indicator).
    assert "Any Title" in html
    assert "Filtering to your saved picks" not in html
    assert "data-saved-selection-indicator" not in html
    assert "data-clear-selection" not in html


def test_indicator_renders_for_titles_only_or_companies_only(app):
    """Either field alone is enough to trigger the indicator — matches
    `selection_filter_kwargs`'s own "titles OR companies" gate, not an
    AND-both-required rule this route might otherwise be tempted to write.
    Also pins pluralization (runner review finding on #226): a singular
    count ("1 title") must not render with a bare-`s` plural suffix
    ("1 titles") the way the pre-fix template did."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, target_titles=["Solo Title Co Role"], target_companies=[])
    company_id = _seed_company(dsn, "Titles Only Co")
    _seed_posting(dsn, "clear-titles-only-1", company_id, title="Solo Title Co Role")

    html = client.get("/").get_data(as_text=True)

    assert "Filtering to your saved picks (1 title" in html
    assert "1 titles" not in html
    assert "0 companies)" in html


# ---------------------------------------------------------------------------
# POST /feed/clear-selection: the write itself.
# ---------------------------------------------------------------------------


def test_clear_selection_zeroes_target_titles_and_companies(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, target_titles=["Engineer"], target_companies=["Clear Write Co"])

    resp = client.post("/feed/clear-selection")

    assert resp.status_code == 303
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        profile = get_profile(conn, CLERK_ID)
    assert profile["target_titles"] == []
    assert profile["target_companies"] == []


def test_clear_selection_titles_only_seed_clears_both_target_lists(app):
    """Devin review finding (#226): every other write test seeds BOTH
    target_titles and target_companies together, so a hypothetical reversed-
    COALESCE regression that clears titles but leaves a stale companies list
    (or vice versa) would only be caught incidentally. This isolates a
    titles-only seed (companies never set, so it starts NULL, not `[]`) and
    asserts both come back `[]` after Clear."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, target_titles=["Engineer"])

    resp = client.post("/feed/clear-selection")

    assert resp.status_code == 303
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        profile = get_profile(conn, CLERK_ID)
    assert profile["target_titles"] == []
    assert profile["target_companies"] == []


def test_clear_selection_is_idempotent_on_double_post(app):
    """A second Clear POST against an already-cleared profile must not error
    or change behavior — both `[]` -> `[]` writes and both responses look
    identical."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, target_titles=["Engineer"], target_companies=["Clear Write Co"])

    first = client.post("/feed/clear-selection")
    second = client.post("/feed/clear-selection")

    assert first.status_code == 303
    assert second.status_code == 303
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        profile = get_profile(conn, CLERK_ID)
    assert profile["target_titles"] == []
    assert profile["target_companies"] == []


def test_clear_selection_with_no_profile_row_skips_write_and_still_responds(app):
    """Devin review finding (#226): a signed-in user who never completed the
    picker has no `profiles` row at all — `_feed_client` normally seeds one
    via `_seed_profile`, so this test seeds only the `users` row directly,
    matching a real pre-onboarding visitor. Before the fix, POSTing here
    created a zeroed `profiles` row (workplace_type NULL, both target lists
    `[]`), which would flip `feed.html`'s `has_selections = profile is not
    None` gate true and silently suppress the "Set up your feed" onboarding
    CTA for someone who never ran the picker. Positive control below (a
    SEPARATE user who DOES have a profile) proves this test would actually
    detect a phantom-row regression rather than passing for an unrelated
    reason (e.g. the route 500ing before touching `profiles` at all)."""
    dsn = app.config["_TEST_DSN"]
    no_profile_user = "user_feed_clear_selection_no_profile"
    _authed(app, no_profile_user)
    _seed_user(dsn, no_profile_user)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True

    resp = client.post("/feed/clear-selection")

    assert resp.status_code == 303
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        profile = get_profile(conn, no_profile_user)
    assert profile is None

    # Positive control: a user who DOES have a profile still gets zeroed —
    # proves the guard is "skip when profile is None", not "never write".
    other_client = _feed_client(app, target_titles=["Engineer"])
    other_resp = other_client.post("/feed/clear-selection")
    assert other_resp.status_code == 303
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        other_profile = get_profile(conn, CLERK_ID)
    assert other_profile["target_titles"] == []


def test_clear_selection_preserves_other_coalesce_fields(app):
    """The clear must be surgical: workplace_type (the one non-COALESCE
    column, m0012) and every COALESCE-preserve field this route doesn't
    touch must survive unchanged."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(
        app,
        target_titles=["Engineer"],
        workplace_type="REMOTE",
        comp_floor_usd=120000,
        skills=["Python"],
        seniority_level="senior",
    )

    resp = client.post("/feed/clear-selection")

    assert resp.status_code == 303
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        profile = get_profile(conn, CLERK_ID)
    assert profile["target_titles"] == []
    assert profile["workplace_type"] == "REMOTE"
    assert profile["comp_floor_usd"] == 120000
    assert profile["skills"] == ["Python"]
    assert profile["seniority_level"] == "senior"


def test_clear_selection_hx_request_returns_feed_content_fragment(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, target_titles=["Engineer"])
    company_id = _seed_company(dsn, "HX Clear Co")
    _seed_posting(dsn, "clear-hx-1", company_id, title="HX Clear Posting")

    resp = client.post("/feed/clear-selection", headers={"HX-Request": "true"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "<html" not in body
    assert 'id="feed-content"' in body
    # The indicator must be gone from THIS same response — proves the
    # fragment was re-rendered against the post-clear profile, not a stale
    # pre-clear one.
    assert "Filtering to your saved picks" not in body


def test_clear_selection_hx_fragment_preserves_search_term_in_filter_form(app):
    """Companion to test_clear_selection_redirect_preserves_current_search_query_string
    for the OTHER response shape: the HX-Request branch re-renders
    _feed_content.html server-side (not a client-side redirect), so the
    search term's survival has to be proven in the re-rendered filter
    form's `value=` attribute, not a Location header — a regression in
    `clear_selection`'s own `filters=filters` pass-through (as opposed to
    `_carry_forward_filters`, which only the redirect/URL path uses) would
    not be caught by the redirect test above."""
    client = _feed_client(app, target_titles=["Engineer"])

    resp = client.post(
        "/feed/clear-selection", query_string={"title": "Manager"}, headers={"HX-Request": "true"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'name="title" value="Manager"' in body


def test_clear_selection_non_hx_redirects_to_feed(app):
    client = _feed_client(app, target_titles=["Engineer"])

    resp = client.post("/feed/clear-selection")

    assert resp.status_code == 303
    assert resp.headers["Location"] == "/"


def test_clear_selection_redirect_preserves_current_search_query_string(app):
    """The whole point of Clear's reachability from the collision empty
    state: the search that triggered it must survive the round trip, not be
    dropped along with the saved selection."""
    client = _feed_client(app, target_titles=["Engineer"])

    resp = client.post("/feed/clear-selection", query_string={"title": "Manager"})

    assert resp.status_code == 303
    assert "title=Manager" in resp.headers["Location"]


def test_anonymous_post_to_clear_selection_is_401():
    """Explicit, dedicated negative case (also covered generically by
    tests/host/test_routing_errors.py::test_gate_covers_every_registered_route_for_every_declared_method,
    which derives its route list from app.url_map.iter_rules() and would
    already fail if this route were ever added to PUBLIC_PATHS): clear_selection
    is not decorated or exempted, so clerk_auth's before_request gate 401s
    an unauthenticated POST before the view body — and therefore the DB
    write — ever runs. No Postgres needed: the gate aborts before any
    connection_factory() call."""
    from jobcannon.web import create_app

    app = create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": lambda r: None,
            "WEBHOOK_SECRET": "whsec_dGVzdA==",
        }
    )
    client = app.test_client()

    resp = client.post("/feed/clear-selection")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Differentiated empty-state copy.
# ---------------------------------------------------------------------------


def test_empty_state_collision_copy_when_search_would_match_without_selection(app):
    """Ground truth (#226 post-review follow-up on `_feed_empty_reason`):
    "collision" must mean dropping the saved selection actually WOULD
    surface a row, not merely "a selection and a search happen to
    coexist". `clear-collision-2` ("Product Manager") is the row that makes
    this a genuine collision — it matches the free-text `title=Manager`
    search but not the saved `target_titles=["Engineer"]` selection, so the
    ground-truth probe (re-running the same query with titles dropped) finds
    it and correctly reports "collision"."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, target_titles=["Engineer"])
    company_id = _seed_company(dsn, "Collision Co")
    # Matches the saved selection but NOT the free-text search below — the
    # AND (#206) zeroes the result set even though a matching row exists.
    _seed_posting(dsn, "clear-collision-1", company_id, title="Engineer")
    # Matches the free-text search but NOT the saved selection — this is
    # what the saved selection is actually excluding, i.e. the genuine
    # collision.
    _seed_posting(dsn, "clear-collision-2", company_id, title="Product Manager")

    resp = client.get("/", query_string={"title": "Manager"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "within your saved picks" in html
    assert "data-feed-empty-collision" in html
    assert "No postings match your selections yet." not in html


def test_empty_state_default_copy_when_genuinely_empty(app):
    """Negative control for the branch above: a saved selection with zero
    matches and NO search term typed is not a collision — the pre-#206 flat
    copy must still be the one that renders."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, target_titles=["Engineer"])
    company_id = _seed_company(dsn, "Genuinely Empty Co")
    _seed_posting(dsn, "clear-genuine-empty-1", company_id, title="Nonmatching Title")

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "No postings match your selections yet." in html
    assert "within your saved picks" not in html
    assert "data-feed-empty-collision" not in html


def test_empty_state_empty_copy_for_corpus_wide_miss_despite_selection_and_search(app):
    """Adversarial refuter's probe P1: a saved selection AND a free-text
    search both being present is NOT sufficient for "collision" when the
    search itself matches nothing anywhere in the corpus — clearing the
    saved selection would not produce a single additional row, so the old
    has-both-so-it-must-be-a-collision heuristic over-promised here. Only
    posting in the corpus is "Engineer" (matches the saved selection, not
    the search); no posting anywhere contains "Zzzznonexistent"."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, target_titles=["Engineer"])
    company_id = _seed_company(dsn, "Corpus Wide Miss Co")
    _seed_posting(dsn, "clear-corpus-miss-1", company_id, title="Engineer")

    resp = client.get("/", query_string={"title": "Zzzznonexistent"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "No postings match your selections yet." in html
    assert "within your saved picks" not in html
    assert "data-feed-empty-collision" not in html


def test_empty_state_empty_copy_for_cross_column_miss(app):
    """Devin review finding (#226): a saved TITLES selection and a free-text
    COMPANY search are independent AND clauses on different columns.
    Dropping the saved titles selection would not help a company search
    that itself matches zero rows — "Nonexistent Co" never appears in this
    corpus regardless of the titles filter — so this must stay "empty", not
    "collision" (the pre-fix `has_selection and has_search` heuristic OR'd
    across both columns and would have mislabeled this)."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, target_titles=["Engineer"])
    company_id = _seed_company(dsn, "Cross Column Co")
    _seed_posting(dsn, "clear-cross-column-1", company_id, title="Engineer")

    resp = client.get("/", query_string={"company": "Nonexistent Co"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "No postings match your selections yet." in html
    assert "within your saved picks" not in html
    assert "data-feed-empty-collision" not in html

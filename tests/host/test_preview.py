"""Pre-signup preview feed: GET /preview (jobcannon/web/onboarding.py).

Own throwaway database, same shape as tests/host/test_onboarding.py: this
module seeds durable, committed postings/companies directly (the Flask
app's pooled connections need to see them — they are on a different
connection than the session-scoped, rollback-isolated db_conn fixture every
other tests/host/ module uses).
"""

from __future__ import annotations

import ast
import html as html_lib
import pathlib
import re
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jobcannon.db._feed import FEED_PAGE_MAX
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
    salary_min: int | None = None,
    salary_currency: str | None = None,
    structural_axes: dict | None = None,
) -> int:
    columns = ["dedup_key", "company_id", "title", "company", "workplace_type", "location"]
    values: list = [dedup_key, company_id, title, company, workplace_type, location]
    if salary_min is not None:
        columns.append("salary_min")
        values.append(salary_min)
    if salary_currency is not None:
        columns.append("salary_currency")
        values.append(salary_currency)
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


# --- #156: keyset "Load more" pagination -----------------------------------

_LOAD_MORE_RE = re.compile(
    r'hx-get="([^"]+)"[^>]*data-load-more|data-load-more[^>]*hx-get="([^"]+)"'
)


def _extract_load_more_url(html: str) -> str | None:
    """Un-escapes the Jinja-autoescaped `&amp;` between query params back to
    a literal `&` — the same decode step a real browser performs on a DOM
    attribute before htmx ever issues the request. Skipping it splits the
    query string on the stray `&` inside `&amp;` and silently drops
    cursor_id/cursor_last_seen (see tests/host/test_feed_pagination.py's
    identical helper, where this was caught as a real test-harness bug)."""
    match = _LOAD_MORE_RE.search(html)
    if match is None:
        return None
    raw = match.group(1) or match.group(2)
    return html_lib.unescape(raw)


def _row_count(html: str) -> int:
    return len(re.findall(r"<article[^>]*data-posting-row[^>]*>", html))


def _seed_preview_pages_worth(dsn, company_id, count, *, title_prefix="Preview Page Row"):
    for i in range(count):
        _seed_posting(
            dsn, f"preview-page-{title_prefix}-{i}", company_id, title=f"{title_prefix} {i:03d}"
        )


def test_preview_load_more_button_appears_when_first_page_is_full(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview Full Page Co")
    _seed_preview_pages_worth(dsn, company_id, FEED_PAGE_MAX + 5)

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "data-load-more" in html
    assert _row_count(html) == FEED_PAGE_MAX


def test_preview_load_more_button_absent_when_fewer_than_a_full_page(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview Short Page Co")
    _seed_preview_pages_worth(dsn, company_id, 3)

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "data-load-more" not in html
    assert _row_count(html) == 3


def test_preview_load_more_hx_request_returns_only_the_next_batch(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview HX Fragment Co")
    _seed_preview_pages_worth(dsn, company_id, FEED_PAGE_MAX + 10)

    client = app.test_client()
    first_page_html = client.get("/preview").get_data(as_text=True)
    load_more_url = _extract_load_more_url(first_page_html)
    assert load_more_url is not None

    resp = client.get(load_more_url, headers={"HX-Request": "true"})
    fragment_html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert _row_count(fragment_html) == 10
    assert "Your preview feed" not in fragment_html
    assert "<nav" not in fragment_html
    first_titles = set(re.findall(r"<h2[^>]*>([^<]+)</h2>", first_page_html))
    second_titles = set(re.findall(r"<h2[^>]*>([^<]+)</h2>", fragment_html))
    assert first_titles & second_titles == set()
    assert len(first_titles) == FEED_PAGE_MAX
    assert len(second_titles) == 10
    assert "data-load-more" not in fragment_html


def test_preview_load_more_without_hx_request_returns_the_full_page(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview No HX Header Co")
    _seed_preview_pages_worth(dsn, company_id, FEED_PAGE_MAX + 5)

    client = app.test_client()
    first_page_html = client.get("/preview").get_data(as_text=True)
    load_more_url = _extract_load_more_url(first_page_html)
    assert load_more_url is not None

    resp = client.get(load_more_url)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Your preview feed" in html
    assert _row_count(html) == 5


def test_preview_load_more_removes_itself_when_exhausted(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview Exhausted Co")
    _seed_preview_pages_worth(dsn, company_id, FEED_PAGE_MAX)

    client = app.test_client()
    first_page_html = client.get("/preview").get_data(as_text=True)
    load_more_url = _extract_load_more_url(first_page_html)
    assert load_more_url is not None

    resp = client.get(load_more_url, headers={"HX-Request": "true"})
    fragment_html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert _row_count(fragment_html) == 0
    assert "data-load-more" not in fragment_html


def test_preview_load_more_preserves_the_current_location_filter(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview Filter Preserve Co")
    for i in range(FEED_PAGE_MAX + 3):
        _seed_posting(
            dsn,
            f"preview-loc-match-{i}",
            company_id,
            title=f"Matching Remote Row {i:03d}",
            location="Remote-Matching",
        )
    for i in range(5):
        _seed_posting(
            dsn,
            f"preview-loc-other-{i}",
            company_id,
            title=f"Other Onsite Row {i:03d}",
            location="Onsite-Other",
        )

    client = app.test_client()
    first_page = client.get("/preview", query_string={"location": "Remote-Matching"}).get_data(
        as_text=True
    )
    load_more_url = _extract_load_more_url(first_page)
    assert load_more_url is not None
    assert "location=Remote-Matching" in load_more_url

    resp = client.get(load_more_url, headers={"HX-Request": "true"})
    fragment_html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert _row_count(fragment_html) == 3
    assert "Other Onsite Row" not in fragment_html


def test_preview_malformed_cursor_degrades_to_first_page_not_500(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview Malformed Cursor Co")
    _seed_preview_pages_worth(dsn, company_id, 3)

    resp = app.test_client().get("/preview", query_string={"cursor_id": "not-a-number"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert _row_count(html) == 3


def test_preview_malformed_cursor_last_seen_degrades_to_first_page_not_500(app):
    """Mirrors test_malformed_cursor_last_seen_degrades_to_first_page_not_500
    in test_feed_pagination.py for the anonymous /preview route: a valid
    cursor_id with a non-ISO cursor_last_seen exercises the branch the
    id-only malformed test above never reaches."""
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview Malformed Timestamp Co")
    _seed_preview_pages_worth(dsn, company_id, 3)

    resp = app.test_client().get(
        "/preview", query_string={"cursor_id": "1", "cursor_last_seen": "not-a-timestamp"}
    )
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert _row_count(html) == 3


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


def test_preview_renders_a_why_chip_for_a_row_with_axes_and_salary(app):
    """Route-level coverage for the chip block in _posting_row.html:
    previously every seeded posting in this module had neither
    structural_axes nor salary fields, so the chip block (and the
    signals-still-computing marker it is mutually exclusive with) was never
    exercised end-to-end through /preview."""
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Chip Coverage Co")
    _seed_posting(
        dsn,
        "preview-chip-1",
        company_id,
        title="Chip Coverage Posting",
        salary_min=120000,
        structural_axes={"seniority_clarity": {"value": True}},
    )

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "Chip Coverage Posting" in html
    assert "data-why-chips" in html
    assert "level stated in title" in html
    assert "salary listed" in html
    # Positive control: proves structural_axes actually landed as a real
    # value rather than silently arriving NULL (e.g. a fixture that forgot
    # to pass Jsonb(...)) -- the pending marker and the chip block are
    # mutually exclusive per-row states in _posting_row.html.
    assert "signals still computing for this posting" not in html


def test_preview_redirects_signed_in_visitor_to_the_real_feed(app):
    """The deliberate redirect half of onboarding.py's _current_identity
    contract: a visitor whose Clerk credentials verify lands on GET / (the
    real feed), never the pre-signup preview."""
    from jobcannon.web.auth import ClerkIdentity

    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(
        user_id="user_preview_signed_in", claims={"sub": "user_preview_signed_in"}
    )

    resp = app.test_client().get("/preview")

    assert resp.status_code in (302, 303)
    assert urlsplit(resp.headers["Location"]).path == "/"


def test_preview_fails_open_to_anonymous_when_identity_check_raises(app):
    """The deliberate fail-open half of the same contract: when the
    verifier itself raises (a Clerk outage, a malformed request), /preview
    must render its own page as an anonymous visitor -- not 500, and not
    redirect as though signed in. Guards the fail-open *direction*
    specifically: a future "fix" that flips this to fail closed (redirect
    or error on a verifier exception) would still return SOME response, so
    asserting only status_code == 200 would not catch that regression --
    this asserts the actual /preview content rendered, not just any 200."""

    def _boom(request):
        raise RuntimeError("simulated Clerk verification outage")

    app.config["VERIFY_REQUEST"] = _boom

    resp = app.test_client().get("/preview")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Location" not in resp.headers
    assert "Your preview feed" in html
    assert "data-ordering-label" in html


def test_preview_hides_an_unknown_salary_currency_but_still_shows_the_amount(app):
    """postings.salary_currency permits the literal 'UNKNOWN' value (schema
    CHECK constraint) -- the row partial guards the whole salary block on
    salary presence but, before this fix, interpolated the currency
    unconditionally, so an UNKNOWN-currency row rendered "UNKNOWN 120000"."""
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Unknown Currency Co")
    _seed_posting(
        dsn,
        "preview-unknown-currency-1",
        company_id,
        title="Unknown Currency Posting",
        salary_min=120000,
        salary_currency="UNKNOWN",
    )

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "Unknown Currency Posting" in html
    # Positive control: the amount must still render -- proves the salary
    # block itself rendered at all, so the "UNKNOWN" absence below reflects
    # the currency guard, not a block that failed to render for some other
    # reason.
    assert "120000" in html
    assert "UNKNOWN" not in html


def test_preview_shows_a_real_salary_currency_label(app):
    """Regression guard alongside the UNKNOWN-suppression fix above: a real
    currency code must still render, proving the new guard only suppresses
    the 'UNKNOWN' sentinel and not currency labels generally."""
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Real Currency Co")
    _seed_posting(
        dsn,
        "preview-real-currency-1",
        company_id,
        title="Real Currency Posting",
        salary_min=95000,
        salary_currency="GBP",
    )

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "Real Currency Posting" in html
    assert "GBP" in html
    assert "95000" in html


# ---------------------------------------------------------------------------
# Sign-up CTA (issue #145): /preview is the pre-signup feed a visitor
# reaches after the picker, with no path onward to an account before this
# fix. Tolerant-default gating, same shape as the header nav
# (tests/host/test_auth_nav.py) — mutating app.config["HOST_CONFIG"] after
# the fixture creates the app works here because
# jobcannon.web._inject_auth_links reads app.config["HOST_CONFIG"] fresh on
# every render rather than closing over a value captured at create_app time.
# ---------------------------------------------------------------------------


def test_preview_shows_signup_cta_when_sign_up_url_configured(app):
    """The `app` fixture never overrides HOST_CONFIG, so TESTING's default
    (clerk_sign_up_url="https://clerk.test/sign-up",
    jobcannon/web/__init__.py) applies -- this is the positive control."""
    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "data-signup-cta" in html
    assert "Sign up to keep this feed" in html
    assert 'href="https://clerk.test/sign-up"' in html


def test_preview_shows_header_signup_nav_and_hides_authed_nav_for_anonymous_visitor(app):
    """Issue #205 negative control on /preview -- the fourth PUBLIC_PATHS
    route named in the issue. Only the anonymous case is directly testable
    at the response level here: test_preview_redirects_signed_in_visitor_
    to_the_real_feed (below) already proves an authed visitor never sees
    this page's body at all (onboarding.preview() redirects to / first,
    via the same _current_identity() check visitor_is_authed's PUBLIC_PATHS
    fallback reuses), so there is no authed /preview render to assert nav
    content against."""
    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "data-auth-nav" in html
    assert "data-postings-history-nav-link" not in html
    assert ">My postings<" not in html
    assert ">Export your data<" not in html
    assert ">Delete account<" not in html


def test_preview_omits_signup_cta_when_both_urls_unset(app):
    """Both clerk_sign_up_url and clerk_sign_in_url unset (the bare
    HostConfig default) -- issue #174 added a sign-in fallback to this CTA,
    so the tolerant-default floor is now "both blank", not just sign-up
    alone. A seeded row proves the page still renders content; the row's
    own per-row CTA (issue #174) must degrade the same way as the
    page-level one, never a bare href=""."""
    from jobcannon.host.config import HostConfig

    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "No CTA Co")
    _seed_posting(dsn, "preview-no-cta-1", company_id, title="No CTA Posting")

    app.config["HOST_CONFIG"] = HostConfig(database_url="", secret_key="testing-secret-key")

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "No CTA Posting" in html
    assert "data-signup-cta" not in html
    assert "Sign up to keep this feed" not in html
    assert "data-action-signup" not in html
    assert "Sign up to apply" not in html
    assert 'href=""' not in html


@pytest.fixture()
def app_with_clerk_key():
    """Same throwaway-DB shape as `app` above, but HOST_CONFIG carries a
    real-shaped Clerk publishable key at create_app() call time. Issue
    #158's gate (jobcannon/web/__init__.py's inject_clerk_frontend) derives
    clerk_publishable_key/clerk_frontend_api_host ONCE at app-factory time
    from the HOST_CONFIG closure, not per-request -- unlike
    _auth_link_context (used by the CTA test above), which re-reads
    app.config["HOST_CONFIG"] on every render. A test proving the #158 gate
    holds on /preview therefore must configure the key here, not by
    mutating app.config["HOST_CONFIG"] after create_app() returns."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.host.config import HostConfig
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_preview_clerk")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        flask_app = create_app(
            config={
                "TESTING": True,
                "HOST_CONFIG": HostConfig(
                    database_url="",
                    secret_key="testing-secret-key",
                    clerk_publishable_key="pk_test_ZXhhbXBsZS5jb20k",
                    clerk_sign_up_url="https://clerk.test/sign-up",
                ),
                "VERIFY_REQUEST": lambda r: None,
                "WEBHOOK_SECRET": "whsec_dGVzdA==",
            }
        )
        flask_app.config["_TEST_DSN"] = dsn
        yield flask_app
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def test_preview_omits_clerk_js_even_when_publishable_key_configured(app_with_clerk_key):
    """Closes the end-to-end gap flagged in test_clerk_loader_template.py's
    test_is_public_request_path_matches_every_public_path docstring:
    /preview needs Postgres to render end-to-end, so the #158 regression
    test there only proves _is_public_request_path() is True for it, not
    that the real /preview route (jobcannon/web/onboarding.py) actually
    omits the loader. This fixture supplies the DB /privacy and /terms
    (both DB-free) don't need, closing the inference gap end-to-end."""
    html = app_with_clerk_key.test_client().get("/preview").get_data(as_text=True)

    assert "clerk-publishable-key" not in html
    assert "clerk.browser.js" not in html
    assert "Clerk.load" not in html
    # Positive control within the same response: the CTA (gated on
    # signup_cta_url via _inject_auth_links, unaffected by #158's gate) IS
    # present -- proves this is a real 200 render of the route, not an
    # empty/error page that would vacuously lack clerk-js too.
    assert "data-signup-cta" in html


# ---------------------------------------------------------------------------
# Per-row sign-up CTA (issue #174): _posting_row.html's save/dismiss/apply
# block only renders when the caller passes show_actions=True (the authed
# feed) -- /preview never does, so an anonymous visitor previously had a
# fully actionless row: no apply link, no hint that signing up unlocks one.
# The new block is a pure addition in _posting_row.html gated on
# `signup_cta_url`, the single identity-derived value
# jobcannon.web._inject_auth_links computes (sign-up preferred, sign-in
# fallback, None for any authed visitor regardless of URL config) -- the
# same value the page-level CTA above gates on.
# ---------------------------------------------------------------------------


def test_preview_row_shows_signup_cta_for_anonymous_visitor(app):
    """Positive control: the `app` fixture's TESTING default configures
    BOTH clerk_sign_up_url and clerk_sign_in_url
    (jobcannon/web/__init__.py), so the row CTA must render with the
    sign-up URL (sign-up wins the `or` fallback) and the page-level #145
    CTA must ALSO still be present -- the >= 2 count proves both CTAs
    rendered rather than one masking as the other via an identical href."""
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Row CTA Co")
    _seed_posting(dsn, "preview-row-cta-1", company_id, title="Row CTA Posting")

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "Row CTA Posting" in html
    assert "data-action-signup" in html
    assert "Sign up to apply" in html
    assert "data-posting-signup" in html
    assert html.count('href="https://clerk.test/sign-up"') >= 2


def test_preview_row_falls_back_to_sign_in_url_when_sign_up_unset(app):
    """Mirror of the page-level fallback test pair in test_auth_nav.py:
    clerk_sign_up_url unset, clerk_sign_in_url configured -- both the
    page-level (#145) and per-row (#174) CTAs must fall back to the
    sign-in URL rather than disappearing."""
    from jobcannon.host.config import HostConfig

    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Row CTA Fallback Co")
    _seed_posting(dsn, "preview-row-cta-fallback-1", company_id, title="Row CTA Fallback Posting")

    app.config["HOST_CONFIG"] = HostConfig(
        database_url="",
        secret_key="testing-secret-key",
        clerk_sign_in_url="https://clerk.test/sign-in",
    )

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "Row CTA Fallback Posting" in html
    assert "data-signup-cta" in html
    assert "data-action-signup" in html
    assert html.count('href="https://clerk.test/sign-in"') >= 2
    assert 'href="https://clerk.test/sign-up"' not in html
    assert 'href=""' not in html


def test_preview_load_more_fragment_includes_row_signup_cta(app):
    """The Load-more HX fragment (#156/#192) re-renders _posting_row.html
    through a bare render_template("_feed_page.html", ...) call
    (jobcannon/web/onboarding.py), a different code path from the full
    page -- the #165 context-processor globals this CTA depends on must
    still reach it there. Every row in an anonymous fragment must carry
    its own CTA (count equals row count), not just the first."""
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Preview Fragment CTA Co")
    _seed_preview_pages_worth(dsn, company_id, FEED_PAGE_MAX + 10, title_prefix="Fragment CTA Row")

    client = app.test_client()
    first_page_html = client.get("/preview").get_data(as_text=True)
    load_more_url = _extract_load_more_url(first_page_html)
    assert load_more_url is not None

    resp = client.get(load_more_url, headers={"HX-Request": "true"})
    fragment_html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    row_count = _row_count(fragment_html)
    assert row_count == 10
    assert fragment_html.count("data-action-signup") == row_count
    assert 'href="https://clerk.test/sign-up"' in fragment_html

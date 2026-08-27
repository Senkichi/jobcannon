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
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg.types.json import Jsonb

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


def test_preview_omits_signup_cta_when_sign_up_url_unset(app):
    from jobcannon.host.config import HostConfig

    app.config["HOST_CONFIG"] = HostConfig(database_url="", secret_key="testing-secret-key")

    html = app.test_client().get("/preview").get_data(as_text=True)

    assert "data-signup-cta" not in html
    assert "Sign up to keep this feed" not in html

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


def _seed_profile(dsn, user_id, *, skills=("python",)):
    from jobcannon.db._profiles import upsert_profile

    with psycopg.connect(dsn) as conn:
        upsert_profile(conn, user_id, skills=list(skills))


def _feed_client(app, user_id=CLERK_ID, *, consent=False, skills=("python",)):
    dsn = app.config["_TEST_DSN"]
    _authed(app, user_id)
    _seed_user(dsn, user_id)
    _seed_profile(dsn, user_id, skills=skills)
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
    # Exact value, not a substring check: "://" / "?" absence is a
    # tautology for any urlsplit-based hostname extraction (urlsplit always
    # strips the scheme and terminates the host at the first "/", "?", or
    # "#"), so it proves nothing about correctness on its own.
    assert clicks[0]["payload"]["apply_destination"] == "boards.greenhouse.io"


def test_apply_destination_strips_port_and_userinfo_end_to_end(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Apply URL Port Co")
    posting_id = _seed_posting(
        dsn,
        "apply-url-port-1",
        company_id,
        title="Apply URL Port Row",
        source_urls=["https://user:pw@jobs.example.com:8443/apply?ref=1"],
    )

    resp = client.post(f"/postings/{posting_id}/apply")
    assert resp.status_code == 200

    clicks = _events(dsn, "posting_apply_clicked")
    assert len(clicks) == 1
    assert clicks[0]["payload"]["apply_destination"] == "jobs.example.com"


def test_apply_with_a_malformed_stored_url_does_not_500(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Malformed URL Co")
    posting_id = _seed_posting(
        dsn,
        "malformed-url-1",
        company_id,
        title="Malformed URL Row",
        source_urls=["https://[oops/x"],
    )

    resp = client.post(f"/postings/{posting_id}/apply")
    assert resp.status_code == 200

    # The mutation still lands even though no usable destination could be
    # extracted for the event.
    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(
            "SELECT status FROM pipeline_status WHERE user_id = %s AND posting_id = %s",
            (CLERK_ID, posting_id),
        ).fetchone()
    assert row["status"] == "applied"
    assert _events(dsn, "posting_apply_clicked") == []


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


def test_apply_control_renders_the_seeded_outbound_link(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Outbound Link Co")
    posting_id = _seed_posting(
        dsn,
        "outbound-link-1",
        company_id,
        title="Outbound Link Row",
        source_urls=["https://boards.greenhouse.io/acme/jobs/123?utm_source=test"],
    )

    html = client.get("/").get_data(as_text=True)
    assert 'href="https://boards.greenhouse.io/acme/jobs/123?utm_source=test"' in html
    assert "data-action-apply" in html

    # Regression guard: an <a href> carrying hx-post gets its default click
    # action (the navigation) cancelled by htmx's own click handler, so the
    # href would render but a real click would never leave the page. The
    # apply route must be reachable only via the fire-and-forget hx-on:click
    # fetch, never via hx-post on the anchor itself.
    apply_path = f"/postings/{posting_id}/apply"
    assert f'hx-post="{apply_path}"' not in html
    assert f"fetch('{apply_path}'" in html


def test_apply_control_handler_guards_double_click_and_stale_swap(app):
    """LOW findings from review-1 (F4) and Devin (F3/F4): a rapid double
    click on Apply must not fire the mutation twice, and a response the
    handler can't turn into a usable row must never silently no-op. Both
    fixes live entirely in the inline hx-on:click string _posting_row.html
    renders (JS execution itself is out of reach for a server-side test —
    see that template's own comment for why), so this pins the markers on
    the ACTUAL rendered handler rather than a hand-copied JS literal."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Double Click Co")
    _seed_posting(
        dsn,
        "double-click-1",
        company_id,
        title="Double Click Row",
        source_urls=["https://boards.greenhouse.io/acme/jobs/1"],
    )

    html = client.get("/").get_data(as_text=True)

    assert "data-action-apply" in html
    assert "row.dataset.applyPending" in html  # pending guard
    assert "row.parentNode === parent" in html  # stale-swap guard
    assert "Apply recorded, but the row did not update" in html  # F4: no silent no-op


# --- #177: applied state + undo -------------------------------------------


def test_apply_response_fragment_renders_applied_state_immediately(app):
    """The apply route's OWN response fragment (not a second GET /) must
    already reflect entry.applied=True: _fetch_entry re-reads the row AFTER
    mark_applied commits, so the same request that records the apply also
    returns the post-mutation row. This is the gap #177 closes -- previously
    this fragment was rendered but never applied to the DOM at all."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Applied Fragment Co")
    posting_id = _seed_posting(
        dsn,
        "applied-fragment-1",
        company_id,
        title="Applied Fragment Row",
        source_urls=["https://boards.greenhouse.io/acme/jobs/1"],
    )

    resp = client.post(f"/postings/{posting_id}/apply")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "data-apply-applied" in html
    assert ">Applied<" in html
    undo_path = f"/postings/{posting_id}/undo-apply"
    assert f'hx-post="{undo_path}"' in html
    assert "data-action-apply>" not in html  # the outbound Apply link is gone


def test_undo_apply_reverts_the_row_to_its_normal_apply_control(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Undo Apply Co")
    posting_id = _seed_posting(
        dsn,
        "undo-apply-1",
        company_id,
        title="Undo Apply Row",
        source_urls=["https://boards.greenhouse.io/acme/jobs/2"],
    )
    assert client.post(f"/postings/{posting_id}/apply").status_code == 200

    resp = client.post(f"/postings/{posting_id}/undo-apply")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "data-apply-applied" not in html
    apply_path = f"/postings/{posting_id}/apply"
    assert f"fetch('{apply_path}'" in html  # back to the normal Apply anchor

    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(
            "SELECT status FROM pipeline_status WHERE user_id = %s AND posting_id = %s",
            (CLERK_ID, posting_id),
        ).fetchone()
    assert row is None  # the row is deleted entirely, not set to a third status


def test_undo_apply_emits_posting_apply_undone_event(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Undo Event Co")
    posting_id = _seed_posting(dsn, "undo-event-1", company_id, title="Undo Event Row")
    assert client.post(f"/postings/{posting_id}/apply").status_code == 200

    assert client.post(f"/postings/{posting_id}/undo-apply").status_code == 200

    undone = _events(dsn, "posting_apply_undone")
    assert len(undone) == 1
    assert undone[0]["posting_id"] == posting_id
    assert undone[0]["payload"] is None


def test_undo_apply_on_a_never_applied_posting_is_a_no_op(app):
    """Idempotent under a double-submit or a stray click, matching
    unsave_posting's contract: no ForeignKeyViolation, no crash, and the
    (still-nonexistent) pipeline_status row stays absent."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Never Applied Co")
    posting_id = _seed_posting(dsn, "never-applied-1", company_id, title="Never Applied Row")

    resp = client.post(f"/postings/{posting_id}/undo-apply")
    assert resp.status_code == 200

    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(
            "SELECT status FROM pipeline_status WHERE user_id = %s AND posting_id = %s",
            (CLERK_ID, posting_id),
        ).fetchone()
    assert row is None


def test_undo_apply_on_a_posting_that_does_not_exist_is_a_404(app):
    """Unlike save/dismiss/apply's INSERT/UPDATE (whose ForeignKeyViolation
    IS the 404 mechanism, actions.py's module docstring), undo-apply's bare
    DELETE never raises one -- unmark_applied's own return value drives the
    404 here instead, matching the shared contract without a pre-check on
    the common (posting exists) path (Devin F1, verified)."""
    client = _feed_client(app, consent=True)

    resp = client.post("/postings/999999999/undo-apply")

    assert resp.status_code == 404


def test_undo_apply_does_not_touch_a_dismissed_posting(app):
    """Scoped delete (status = 'applied' in the WHERE clause): undo-apply
    against a posting this user separately dismissed must never clear that
    dismissal -- proves the DELETE can't be broadened to "this user's
    pipeline_status row for this posting" by accident."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True)
    company_id = _seed_company(dsn, "Dismissed Untouched Co")
    posting_id = _seed_posting(dsn, "dismissed-untouched-1", company_id, title="Dismissed Row")
    assert client.post(f"/postings/{posting_id}/dismiss").status_code == 200

    resp = client.post(f"/postings/{posting_id}/undo-apply")
    assert resp.status_code == 200

    with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute(
            "SELECT status FROM pipeline_status WHERE user_id = %s AND posting_id = %s",
            (CLERK_ID, posting_id),
        ).fetchone()
    assert row["status"] == "dismissed"


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
    """User A's save is invisible to user B — both directions: the raw
    `watchlists` row count (below) AND the rendered `saved` flag each user's
    own feed shows for the same posting. A user B that is only ever proven
    absent from the count query would pass this test for `false AS saved` or
    for a join with no `user_id` predicate at all (which would leak A's save
    into every user's feed as `saved = true`); rendering B's feed and
    checking the button text rules both of those out."""
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Per User Co")
    posting_id = _seed_posting(dsn, "per-user-1", company_id, title="Per User Row")

    client_a = _feed_client(app, user_id="user_a_actions", consent=True)
    assert client_a.post(f"/postings/{posting_id}/save").status_code == 200
    # Render A's view BEFORE creating client_b: _authed (called by
    # _feed_client) overwrites the shared app.config["VERIFY_REQUEST"]
    # callback the identity resolver reads on EVERY request regardless of
    # which test client issued it, so a second _feed_client() call
    # re-authenticates every client, not just its own.
    html_a = client_a.get("/").get_data(as_text=True)

    client_b = _feed_client(app, user_id="user_b_actions", consent=True)
    html_b = client_b.get("/").get_data(as_text=True)
    assert "Per User Row" in html_a and "Per User Row" in html_b
    assert "Saved" in html_a  # A sees their own save reflected
    assert "Saved" not in html_b  # B never sees A's save

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


def test_dismissed_posting_disappears_from_the_dismissers_feed_but_not_anothers(app):
    dsn = app.config["_TEST_DSN"]
    company_id = _seed_company(dsn, "Dismiss Visibility Co")
    posting_id = _seed_posting(
        dsn, "dismiss-visibility-1", company_id, title="Dismiss Visibility Row"
    )

    client_a = _feed_client(app, user_id="user_a_dismiss", consent=True)
    resp = client_a.post(f"/postings/{posting_id}/dismiss")
    assert resp.status_code == 200
    # _fetch_entry returns None for a row the dismissing user can no longer
    # see, so the re-rendered fragment for THIS route is an empty body.
    assert resp.get_data() == b""

    html_a = client_a.get("/").get_data(as_text=True)
    assert "Dismiss Visibility Row" not in html_a

    client_b = _feed_client(app, user_id="user_b_dismiss", consent=True)
    html_b = client_b.get("/").get_data(as_text=True)
    assert "Dismiss Visibility Row" in html_b


def test_the_overlap_chip_survives_a_save_mutation_swap(app):
    """Regression for the actions.py / pages.py build_entry drift: a
    save/dismiss/apply swap must re-render the SAME entry shape the initial
    page render showed for that row (chips included), not a stripped-down
    one built with no profile. Skills must actually overlap the seeded
    title's tokens (default fixture skills=["python"] never overlaps a
    default "Engineer" title, which would make the divergence invisible on
    both the page render AND the swap)."""
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app, consent=True, skills=("engineer",))
    company_id = _seed_company(dsn, "Overlap Chip Co")
    posting_id = _seed_posting(dsn, "overlap-chip-1", company_id, title="Engineer")

    page_html = client.get("/").get_data(as_text=True)
    assert "title matches your selections: engineer" in page_html

    resp = client.post(f"/postings/{posting_id}/save")
    assert resp.status_code == 200
    fragment_html = resp.get_data(as_text=True)
    assert "Engineer" in fragment_html
    assert "Saved" in fragment_html
    assert "title matches your selections: engineer" in fragment_html


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

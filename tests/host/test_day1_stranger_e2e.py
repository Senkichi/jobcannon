"""End-to-end acceptance test for the day-one visitor journey: anonymous
landing through a saved posting, against a real Postgres database, driven by
a synthetic identity that is provably not the product owner's.

Own throwaway database, same shape as tests/host/test_handoff.py and
tests/host/test_feed_events.py: this test drives eight sequential HTTP
requests across several routes that each open their own pooled connection —
the event logger and the consent-resolution read in particular never join
whatever connection a test fixture might be holding — so every seed or
setup write below is committed on its own connection before the next request
is issued, rather than relying on an enclosing rollback to make it visible.
"""

from __future__ import annotations

import re
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from jobcannon.db._profiles import GUEST_USER_ID
from jobcannon.host.structural_axes import score_pending_structural_axes
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

# stranger_<uuid4hex>: a fresh, disposable id minted once per test run. The
# regex proves-by-construction that the identity asserted against the
# database below is synthetic, never a hardcoded or real one.
_SYNTHETIC_ID_RE = re.compile(r"^stranger_[0-9a-f]{32}$")

_JD_TEXT = (
    "We are hiring for a role focused on backend systems and distributed data "
    "pipelines. Responsibilities include designing APIs, owning on-call "
    "rotations, and partnering with product on the roadmap. Qualifications: "
    "several years of professional software experience, strong SQL and Python "
    "fundamentals, and experience operating production services at scale."
)


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_day1_stranger")
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


@pytest.fixture()
def seeded_feed_corpus(app):
    """One company plus three postings with real job-description text,
    committed on their own connection (the Flask app's pooled connections need
    to see them on a different connection than this fixture's write). Each
    posting carries a salary range, which guarantees at least one literal
    "why" chip independent of any picker selection, and a precise, recent
    posted date. Runs score_pending_structural_axes afterward so every row's
    structural-axes columns are populated before any page renders — skipping
    that call leaves them NULL and silently drops whichever "why" chip or
    ordering logic reads them. Returns the seeded titles and posting ids."""
    dsn = app.config["_TEST_DSN"]
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES (%s) RETURNING id",
            ("Day One Stranger Test Co",),
        ).fetchone()["id"]
        titles = [
            "Distinctive Day One Backend Engineer",
            "Distinctive Day One Platform Engineer",
            "Distinctive Day One Data Engineer",
        ]
        posting_ids = []
        for i, title in enumerate(titles):
            row = conn.execute(
                "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, "
                "salary_min, salary_max, posted_date, posted_date_precision) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, 'exact') RETURNING id",
                (
                    f"day1-stranger-{i}",
                    company_id,
                    title,
                    "Day One Stranger Test Co",
                    _JD_TEXT,
                    110000 + i * 5000,
                    140000 + i * 5000,
                ),
            ).fetchone()
            posting_ids.append(row["id"])

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        score_pending_structural_axes(conn, {})

    return {"titles": titles, "posting_ids": posting_ids}


def _authed(app, user_id: str) -> None:
    from jobcannon.web.auth import ClerkIdentity

    app.config["VERIFY_REQUEST"] = lambda _req: ClerkIdentity(
        user_id=user_id, claims={"sub": user_id}
    )


def _events(dsn: str, user_id: str, event_type: str) -> list:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT * FROM events WHERE user_id = %s AND event_type = %s ORDER BY id",
            (user_id, event_type),
        ).fetchall()


def test_day_one_stranger_journey_end_to_end(app, seeded_feed_corpus):
    dsn = app.config["_TEST_DSN"]
    stranger_id = f"stranger_{uuid4().hex}"
    client = app.test_client()

    # 2. GET /start, unauthenticated.
    start_resp = client.get("/start", query_string={"ref": "producthunt"})
    assert start_resp.status_code == 200

    # 3. POST /start with picker selections -> a profiles row exists under a
    # freshly minted anonymous user id.
    submit_resp = client.post(
        "/start",
        data={
            "skills": ["python", "sql"],
            "seniority_level": "senior",
            "years_of_experience": "6",
            "workplace_type": "any",
        },
    )
    assert submit_resp.status_code in (302, 303)
    with client.session_transaction() as sess:
        anon_id = sess["pending_picker"]["anon_id"]
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        anon_profile = conn.execute(
            "SELECT * FROM profiles WHERE user_id = %s", (anon_id,)
        ).fetchone()
    assert anon_profile is not None
    assert anon_profile["seniority_level"] == "senior"

    # 4. GET /preview -> seeded titles appear (with the route's own
    # zero-match copy absent — the positive control that proves the corpus
    # actually reached the page), at least one "why" chip renders, the
    # NULL-axes marker is absent (the only assertion that proves the
    # fixture's structural-axes pass ran — the salary chip renders without
    # it), and the ordering label honestly says nothing has been ranked yet.
    preview_resp = client.get("/preview")
    assert preview_resp.status_code == 200
    preview_html = preview_resp.get_data(as_text=True)
    for title in seeded_feed_corpus["titles"]:
        assert title in preview_html
    assert "No postings match your selections yet." not in preview_html
    assert "salary listed" in preview_html
    assert "signals still computing for this posting" not in preview_html
    assert "Sorted by recency" in preview_html
    assert "personalized ranking is not live yet" in preview_html
    assert "Ranked by" not in preview_html

    # 5. Simulate the authenticated return via the app's own auth-verification
    # seam. This triggers the anon-to-authed handoff: the profile is re-keyed
    # onto the real account, the anonymous placeholder user row is gone,
    # exactly one signup event exists with a channel and a wave value, and
    # the response redirects once to the consent route.
    _authed(app, stranger_id)
    handoff_resp = client.get("/")
    assert handoff_resp.status_code == 302
    assert handoff_resp.headers["Location"].endswith("/consent")

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        anon_user_count = conn.execute(
            "SELECT count(*) AS n FROM users WHERE id = %s", (anon_id,)
        ).fetchone()["n"]
        stranger_profile = conn.execute(
            "SELECT * FROM profiles WHERE user_id = %s", (stranger_id,)
        ).fetchone()
    assert anon_user_count == 0
    assert stranger_profile is not None
    assert stranger_profile["seniority_level"] == "senior"
    # The identity is provably synthetic (matches the fresh-uuid4 pattern
    # this test minted above) and is a separate assertion from proving it is
    # not the guest-demo sentinel — two different non-goals, not one check
    # doing double duty. Both anchor on the database row, not the local
    # variable, so they prove what actually landed.
    assert _SYNTHETIC_ID_RE.match(stranger_profile["user_id"])
    assert stranger_profile["user_id"] != GUEST_USER_ID

    signup_events = _events(dsn, stranger_id, "user_signed_up")
    assert len(signup_events) == 1
    assert signup_events[0]["payload"]["channel"] == "producthunt"
    assert signup_events[0]["payload"]["wave"] is not None

    # A brand-new account is non-consenting by default, and no consent
    # decision has been recorded yet — the next step is what produces it.
    assert _events(dsn, stranger_id, "consent_recorded") == []

    # 6. POST /consent with choice=grant, through the real route (not a
    # fixture or a direct SQL write). No longer a redirect (issue #182):
    # the route re-renders the full page in place with an inline
    # confirmation instead of bouncing to the feed.
    consent_resp = client.post("/consent", data={"choice": "grant"})
    assert consent_resp.status_code == 200
    assert "Analytics enabled." in consent_resp.get_data(as_text=True)

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        user_row = conn.execute(
            "SELECT analytics_consent FROM users WHERE id = %s", (stranger_id,)
        ).fetchone()
    assert user_row["analytics_consent"] is True
    consent_events = _events(dsn, stranger_id, "consent_recorded")
    assert len(consent_events) == 1
    assert consent_events[0]["payload"]["granted"] is True

    # 7. GET / -> the ranked list renders, and one impression event exists
    # per rendered row, with feed_position values 1..N and a non-null
    # ranker-version value on every one.
    feed_resp = client.get("/")
    assert feed_resp.status_code == 200
    feed_html = feed_resp.get_data(as_text=True)
    for title in seeded_feed_corpus["titles"]:
        assert title in feed_html
    assert "signals still computing for this posting" not in feed_html

    impressions = _events(dsn, stranger_id, "posting_impression")
    n = len(seeded_feed_corpus["posting_ids"])
    assert len(impressions) == n
    assert sorted(row["feed_position"] for row in impressions) == list(range(1, n + 1))
    assert all(row["ranker_version"] is not None for row in impressions)
    assert {row["posting_id"] for row in impressions} == set(seeded_feed_corpus["posting_ids"])

    # 8. POST /postings/<id>/save -> 200, a watchlists row exists, and
    # exactly one posting-saved event exists.
    saved_posting_id = seeded_feed_corpus["posting_ids"][0]
    save_resp = client.post(f"/postings/{saved_posting_id}/save")
    assert save_resp.status_code == 200
    assert "Saved" in save_resp.get_data(as_text=True)

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        watchlist_rows = conn.execute(
            "SELECT * FROM watchlists WHERE user_id = %s AND posting_id = %s",
            (stranger_id, saved_posting_id),
        ).fetchall()
    assert len(watchlist_rows) == 1

    saved_events = _events(dsn, stranger_id, "posting_saved")
    assert len(saved_events) == 1
    assert saved_events[0]["posting_id"] == saved_posting_id

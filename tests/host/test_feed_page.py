"""Authed ranked list at GET / (jobcannon/web/pages.py): server-validated
filters, per-row why-chips, and the honest ordering label.

Own throwaway database, same shape as tests/host/test_preview.py and
tests/host/test_handoff.py: postings/feed_state must be durably committed
on a different connection than the Flask app's pooled one.

Every test's client bypasses the anon-to-authed handoff
(jobcannon/web/handoff.py) by presetting its session marker directly rather
than making a throwaway priming request: the handoff redirects a brand-new
authed user's first request to GET /consent (no prior consent choice), which
would make every test here about the redirect instead of about the feed
route. The handoff's own behavior is covered by tests/host/test_handoff.py;
this module seeds the `users` row itself and skips straight past it.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from jobcannon.db._profiles import upsert_profile
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

CLERK_ID = "user_feed_page_test"


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_feed_page")
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
    `profiles` row already committed — the state the feed route needs to
    render past its no-profile empty state (see module docstring for why the
    handoff itself is bypassed rather than exercised)."""
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
    title="Engineer",
    company="Feed Test Co",
    workplace_type=None,
    location=None,
    salary_min=None,
    salary_max=None,
    structural_axes=None,
    last_seen=None,
):
    columns = [
        "dedup_key",
        "company_id",
        "title",
        "company",
        "workplace_type",
        "location",
        "salary_min",
        "salary_max",
    ]
    values = [
        dedup_key,
        company_id,
        title,
        company,
        workplace_type,
        location,
        salary_min,
        salary_max,
    ]
    if structural_axes is not None:
        columns.append("structural_axes")
        values.append(Jsonb(structural_axes))
    if last_seen is not None:
        columns.append("last_seen")
        values.append(last_seen)
    placeholders = ", ".join(["%s"] * len(values))
    cols_sql = ", ".join(columns)
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(
            f"INSERT INTO postings ({cols_sql}) VALUES ({placeholders}) RETURNING id",
            values,
        ).fetchone()[0]


def _seed_feed_state(dsn, user_id, posting_id, rank_score, ranker_version):
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO feed_state (user_id, posting_id, rank_score, ranker_version, computed_at) "
            "VALUES (%s, %s, %s, %s, now())",
            (user_id, posting_id, rank_score, ranker_version),
        )


def test_authed_feed_renders_postings_not_counts(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Feed Positive Control Co")
    _seed_posting(dsn, "feed-positive-1", company_id, title="Distinctive Feed Posting Title")

    html = client.get("/").get_data(as_text=True)

    assert "Distinctive Feed Posting Title" in html
    # Positive control (standard-gate obligation 2): the two empty-state
    # discriminators must be ABSENT — present would mean the seeded row
    # never reached the page (e.g. a fixture that forgot open_pool, or a
    # profile that never got seeded, would fail closed to one of these
    # instead).
    assert "Your feed isn't wired up yet" not in html
    assert "The corpus is warming up" not in html


def test_feed_reads_rank_score_and_ranker_version_from_feed_state(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Rank Co")
    ranked_id = _seed_posting(dsn, "feed-ranked-1", company_id, title="Ranked Posting")
    _seed_posting(dsn, "feed-unranked-1", company_id, title="Unranked Posting")
    _seed_feed_state(dsn, CLERK_ID, ranked_id, 0.9, "ranker-test-v7")

    html = client.get("/").get_data(as_text=True)

    assert "Ranked Posting" in html
    assert "Unranked Posting" in html
    assert html.index("Ranked Posting") < html.index("Unranked Posting")
    assert "Ranked by ranker-test-v7." in html


def test_feed_labels_ordering_honestly_when_unranked(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Honest Feed Label Co")
    _seed_posting(dsn, "feed-honest-1", company_id, title="Unranked Feed Posting")

    html = client.get("/").get_data(as_text=True)

    # The seeded row must actually reach the page — without this the
    # ordering-label assertions below would hold just as well on an empty
    # result set, making the "when unranked" premise in the test name
    # untested.
    assert "Unranked Feed Posting" in html
    assert "Sorted by recency" in html
    assert "unranked-v0" in html
    assert "Ranked by" not in html


def test_each_row_renders_at_least_one_why_chip_or_the_pending_marker(app):
    dsn = app.config["_TEST_DSN"]
    # No titles/skills on the seeded profile -> no overlap chip is possible.
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Chip Co")
    # No structural_axes, no salary -> why_chips() returns [] for this row.
    _seed_posting(dsn, "feed-chip-1", company_id, title="Chip Test Posting")

    html = client.get("/").get_data(as_text=True)

    assert "Chip Test Posting" in html
    assert "why: not yet available for this posting" in html


def test_filters_apply_and_unknown_sort_token_is_rejected_without_500(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Filter Co")
    _seed_posting(dsn, "feed-filter-alpha", company_id, title="Distinctive Filter Alpha")
    _seed_posting(dsn, "feed-filter-beta", company_id, title="Distinctive Filter Beta")

    # "Alpha" (not the full seeded title) is deliberate: it is a strict
    # substring of "Distinctive Filter Alpha" and not equal to it, so this
    # only passes under a title-contains filter — an exact-match filter
    # would match zero rows on this input and fail the assertion below.
    filtered = client.get("/", query_string={"title": "Alpha"})
    html_filtered = filtered.get_data(as_text=True)
    assert filtered.status_code == 200
    assert "Distinctive Filter Alpha" in html_filtered
    assert "Distinctive Filter Beta" not in html_filtered
    assert "No postings match your selections yet." not in html_filtered

    bogus_sort = client.get("/", query_string={"sort": "bogus-token-xyz"})
    html_bogus = bogus_sort.get_data(as_text=True)
    assert bogus_sort.status_code == 200
    assert "Distinctive Filter Alpha" in html_bogus
    assert "Distinctive Filter Beta" in html_bogus


def test_title_filter_matches_on_substring_not_full_title(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Substring Title Co")
    _seed_posting(dsn, "feed-substr-1", company_id, title="Senior Backend Engineer")
    _seed_posting(dsn, "feed-substr-2", company_id, title="Product Manager")

    resp = client.get("/", query_string={"title": "Backend"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Senior Backend Engineer" in html
    assert "Product Manager" not in html
    assert "No postings match your selections yet." not in html


def test_company_filter_excludes_non_matching_company(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    match_co = _seed_company(dsn, "Match Co")
    other_co = _seed_company(dsn, "Other Co")
    _seed_posting(dsn, "feed-company-match", match_co, title="Match Row", company="Match Co")
    _seed_posting(dsn, "feed-company-other", other_co, title="Other Row", company="Other Co")

    resp = client.get("/", query_string={"company": "Match Co"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Match Row" in html
    assert "Other Row" not in html
    assert "No postings match your selections yet." not in html


def test_location_filter_matches_on_substring(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Location Filter Co")
    _seed_posting(
        dsn, "feed-loc-match", company_id, title="Remote Row", location="Austin, TX (Remote)"
    )
    _seed_posting(dsn, "feed-loc-other", company_id, title="Onsite Row", location="Denver, CO")

    resp = client.get("/", query_string={"location": "Austin"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Remote Row" in html
    assert "Onsite Row" not in html
    assert "No postings match your selections yet." not in html


def test_workplace_type_filter_excludes_non_matching_type(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Workplace Filter Co")
    _seed_posting(
        dsn, "feed-wt-remote", company_id, title="Remote Type Row", workplace_type="REMOTE"
    )
    _seed_posting(
        dsn, "feed-wt-onsite", company_id, title="Onsite Type Row", workplace_type="ONSITE"
    )

    resp = client.get("/", query_string={"workplace_type": "remote"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Remote Type Row" in html
    assert "Onsite Type Row" not in html
    assert "No postings match your selections yet." not in html


def test_filters_are_additive_not_independently_matched(app):
    dsn = app.config["_TEST_DSN"]
    client = _feed_client(app)
    company_id = _seed_company(dsn, "Additive Filter Co")
    _seed_posting(
        dsn,
        "feed-additive-both",
        company_id,
        title="Backend Engineer",
        company="Additive Match Co",
    )
    _seed_posting(
        dsn,
        "feed-additive-title-only",
        company_id,
        title="Backend Analyst",
        company="Additive Match Co",
    )
    _seed_posting(
        dsn,
        "feed-additive-company-only",
        company_id,
        title="Backend Engineer",
        company="Different Co",
    )

    resp = client.get("/", query_string={"title": "Engineer", "company": "Additive Match Co"})
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "feed-additive-both" not in html  # sanity: dedup_key never rendered
    assert "Backend Engineer" in html
    assert "Backend Analyst" not in html
    # Both postings titled "Backend Engineer" share that title string; the
    # company-only row must still be excluded by the additive title filter.
    # Distinguish via the (unique) company name, which only the
    # both-match row shares with the filter value.
    assert "Different Co" not in html
    assert "No postings match your selections yet." not in html


def test_db_failure_degrades_to_empty_state_not_500(app, monkeypatch):
    from jobcannon.web import pages

    client = _feed_client(app)

    def _raise_stats(conn):
        raise RuntimeError("corpus stats read failed")

    monkeypatch.setattr(pages, "corpus_stats", _raise_stats)

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Your feed isn't wired up yet" in html


def test_feed_postings_read_failure_degrades_to_empty_list_not_500(app, monkeypatch):
    """Exercises `_read_feed_postings` specifically (as opposed to the
    `_read_page_data` path covered above): profile present, corpus non-empty
    -> the route reaches the feed query, which then fails. Must still 200
    with the designed empty-feed copy, not the no-profile / empty-corpus
    copy above (those would prove the wrong helper's fail-closed branch) and
    not a 500."""
    dsn = app.config["_TEST_DSN"]
    from jobcannon.web import pages

    client = _feed_client(app)
    company_id = _seed_company(dsn, "Feed Read Failure Co")
    _seed_posting(dsn, "feed-read-failure-1", company_id, title="Should Not Render")

    def _raise_postings(conn, *, user_id, **kwargs):
        raise RuntimeError("feed postings read failed")

    monkeypatch.setattr(pages, "list_feed_postings", _raise_postings)

    resp = client.get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Should Not Render" not in html
    assert "No postings match your selections yet." in html
    # Proves the failure was caught inside the feed-postings path, not the
    # page-data path: the page-data read still succeeded, so this is NOT the
    # no-profile / empty-corpus branch.
    assert "Your feed isn't wired up yet" not in html
    assert "The corpus is warming up" not in html

"""Feed read DAL: list_feed_postings / count_feed_postings / distinct_titles
/ distinct_companies (jobcannon/db/_feed.py). The first postings-list query
and the first feed_state read in this repo's history — feed_state has no
writer anywhere, so every seed in this file writes it directly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _seed_company(conn, name):
    return conn.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status) "
        "VALUES (%s, %s, 'jobvite', %s, 'hit') RETURNING id",
        (name, name, name.lower().replace(" ", "-")),
    ).fetchone()["id"]


def _seed_posting(
    conn,
    dedup_key,
    company_id,
    *,
    title="Engineer",
    company="Acme",
    location=None,
    workplace_type=None,
    last_seen=None,
):
    columns = ["dedup_key", "company_id", "title", "company", "location", "workplace_type"]
    values = [dedup_key, company_id, title, company, location, workplace_type]
    if last_seen is not None:
        columns.append("last_seen")
        values.append(last_seen)
    placeholders = ", ".join(["%s"] * len(values))
    cols_sql = ", ".join(columns)
    return conn.execute(
        f"INSERT INTO postings ({cols_sql}) VALUES ({placeholders}) RETURNING id",
        values,
    ).fetchone()["id"]


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def _seed_feed_state(conn, user_id, posting_id, rank_score, ranker_version="test-v1"):
    conn.execute(
        "INSERT INTO feed_state (user_id, posting_id, rank_score, ranker_version, computed_at) "
        "VALUES (%s, %s, %s, %s, now())",
        (user_id, posting_id, rank_score, ranker_version),
    )


def test_returns_rows_ordered_newest_first_when_all_unranked(db_conn):
    from jobcannon.db._feed import list_feed_postings

    company_id = _seed_company(db_conn, "Acme")
    oldest = _seed_posting(db_conn, "p-old", company_id, last_seen=_BASE_TIME)
    middle = _seed_posting(db_conn, "p-mid", company_id, last_seen=_BASE_TIME + timedelta(hours=1))
    newest = _seed_posting(db_conn, "p-new", company_id, last_seen=_BASE_TIME + timedelta(hours=2))

    rows = list_feed_postings(db_conn)

    assert [r["id"] for r in rows] == [newest, middle, oldest]
    assert all(r["rank_score"] is None for r in rows)


def test_rank_score_orders_ahead_of_recency_when_present(db_conn):
    from jobcannon.db._feed import list_feed_postings

    _seed_user(db_conn, "u-rank")
    company_id = _seed_company(db_conn, "Acme")
    stale_but_ranked = _seed_posting(db_conn, "p-ranked", company_id, last_seen=_BASE_TIME)
    fresh_unranked = _seed_posting(
        db_conn, "p-fresh", company_id, last_seen=_BASE_TIME + timedelta(hours=5)
    )
    old_unranked = _seed_posting(
        db_conn, "p-old", company_id, last_seen=_BASE_TIME - timedelta(hours=5)
    )
    _seed_feed_state(db_conn, "u-rank", stale_but_ranked, 0.9)

    rows = list_feed_postings(db_conn, user_id="u-rank")

    assert [r["id"] for r in rows] == [stale_but_ranked, fresh_unranked, old_unranked]
    assert rows[0]["rank_score"] == pytest.approx(0.9)
    assert rows[0]["ranker_version"] == "test-v1"
    assert rows[1]["rank_score"] is None


def test_anonymous_and_authed_shapes_return_identical_columns(db_conn):
    from jobcannon.db._feed import list_feed_postings

    _seed_user(db_conn, "u-shape")
    company_id = _seed_company(db_conn, "Acme")
    _seed_posting(db_conn, "p-shape", company_id, last_seen=_BASE_TIME)

    anon_rows = list_feed_postings(db_conn)
    authed_rows = list_feed_postings(db_conn, user_id="u-shape")

    assert len(anon_rows) == 1
    assert len(authed_rows) == 1
    assert set(anon_rows[0].keys()) == set(authed_rows[0].keys())
    assert anon_rows[0]["rank_score"] is None
    assert anon_rows[0]["ranker_version"] is None
    assert authed_rows[0]["rank_score"] is None
    assert authed_rows[0]["ranker_version"] is None


def test_unknown_sort_token_raises_valueerror(db_conn):
    from jobcannon.db._feed import list_feed_postings

    with pytest.raises(ValueError):
        list_feed_postings(db_conn, sort="bogus-token")


def test_like_metacharacters_in_filter_are_escaped(db_conn):
    from jobcannon.db._feed import list_feed_postings

    company_id = _seed_company(db_conn, "Acme")
    percent_row = _seed_posting(
        db_conn, "p-percent", company_id, location="50% Travel", last_seen=_BASE_TIME
    )
    _seed_posting(db_conn, "p-plain", company_id, location="Remote", last_seen=_BASE_TIME)
    underscore_row = _seed_posting(
        db_conn, "p-underscore", company_id, location="New_York", last_seen=_BASE_TIME
    )
    _seed_posting(db_conn, "p-noscore", company_id, location="NewYYork", last_seen=_BASE_TIME)

    percent_matches = list_feed_postings(db_conn, location_contains="%")
    assert [r["id"] for r in percent_matches] == [percent_row]

    underscore_matches = list_feed_postings(db_conn, location_contains="_")
    assert [r["id"] for r in underscore_matches] == [underscore_row]


def test_limit_is_capped_at_feed_page_max(db_conn):
    from jobcannon.db._feed import FEED_PAGE_MAX, list_feed_postings

    company_id = _seed_company(db_conn, "Acme")
    for i in range(FEED_PAGE_MAX + 5):
        _seed_posting(db_conn, f"p-{i}", company_id, last_seen=_BASE_TIME + timedelta(minutes=i))

    rows = list_feed_postings(db_conn, limit=1000)

    assert len(rows) == FEED_PAGE_MAX


def test_null_structural_axes_row_is_returned_not_filtered_out(db_conn):
    from jobcannon.db._feed import list_feed_postings

    company_id = _seed_company(db_conn, "Acme")
    posting_id = _seed_posting(db_conn, "p-null-axes", company_id, last_seen=_BASE_TIME)

    rows = list_feed_postings(db_conn)

    assert [r["id"] for r in rows] == [posting_id]
    assert rows[0]["structural_axes"] is None


def test_distinct_titles_and_companies_are_corpus_derived_and_bounded(db_conn):
    from jobcannon.db._feed import distinct_companies, distinct_titles

    company_id = _seed_company(db_conn, "Acme")
    for i in range(60):
        _seed_posting(
            db_conn,
            f"p-title-{i}",
            company_id,
            title=f"Title {i:03d}",
            company=f"Company {i:03d}",
            last_seen=_BASE_TIME,
        )

    titles = distinct_titles(db_conn, limit=50)
    companies = distinct_companies(db_conn, limit=50)

    assert len(titles) == 50
    assert len(companies) == 50
    assert titles == sorted(titles)
    assert companies == sorted(companies)
    assert set(titles) <= {f"Title {i:03d}" for i in range(60)}
    assert set(companies) <= {f"Company {i:03d}" for i in range(60)}

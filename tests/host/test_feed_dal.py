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


def _after_from_row(row):
    """The (rank_score, last_seen, id) `after` tuple `list_feed_postings`
    expects, taken directly from a row — bypasses the query-string round
    trip so pagination tests below are about `_cursor_predicate` itself, not
    about `parse_cursor`'s string parsing (covered separately)."""
    return (row["rank_score"], row["last_seen"], row["id"])


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


def test_dismissed_posting_is_excluded_for_the_dismissing_user_but_not_others(db_conn):
    from jobcannon.db._feed import list_feed_postings
    from jobcannon.db._user_actions import dismiss_posting

    _seed_user(db_conn, "u-dismisser")
    _seed_user(db_conn, "u-other")
    company_id = _seed_company(db_conn, "Acme")
    posting_id = _seed_posting(db_conn, "p-dismissed", company_id, last_seen=_BASE_TIME)

    dismiss_posting(db_conn, "u-dismisser", posting_id)

    dismisser_ids = [r["id"] for r in list_feed_postings(db_conn, user_id="u-dismisser")]
    other_ids = [r["id"] for r in list_feed_postings(db_conn, user_id="u-other")]
    anon_ids = [r["id"] for r in list_feed_postings(db_conn)]

    assert posting_id not in dismisser_ids
    assert posting_id in other_ids
    assert posting_id in anon_ids


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


# --- #148: distinct_titles/distinct_companies `q` search ------------------


def test_distinct_titles_q_finds_a_title_outside_the_alphabetical_window(db_conn):
    """The exact bug #148 reports: with 60 alphabetically-early titles ahead
    of it, "Software Engineer" would never appear in an unfiltered
    limit=50 window — q="Software" must still surface it."""
    from jobcannon.db._feed import distinct_titles

    company_id = _seed_company(db_conn, "Acme")
    for i in range(60):
        _seed_posting(
            db_conn,
            f"p-alpha-{i}",
            company_id,
            title=f"Aardvark Role {i:03d}",
            last_seen=_BASE_TIME,
        )
    _seed_posting(db_conn, "p-target", company_id, title="Software Engineer", last_seen=_BASE_TIME)

    unfiltered = distinct_titles(db_conn, limit=50)
    assert "Software Engineer" not in unfiltered  # sanity: reproduces the bug first

    matches = distinct_titles(db_conn, q="Software", limit=50)
    assert matches == ["Software Engineer"]


def test_distinct_titles_q_orders_prefix_matches_before_substring_matches(db_conn):
    from jobcannon.db._feed import distinct_titles

    company_id = _seed_company(db_conn, "Acme")
    _seed_posting(
        db_conn, "p-substr", company_id, title="Senior Product Manager", last_seen=_BASE_TIME
    )
    _seed_posting(db_conn, "p-prefix", company_id, title="Product Manager", last_seen=_BASE_TIME)

    matches = distinct_titles(db_conn, q="Product", limit=50)

    assert matches == ["Product Manager", "Senior Product Manager"]


def test_distinct_titles_q_is_case_insensitive(db_conn):
    from jobcannon.db._feed import distinct_titles

    company_id = _seed_company(db_conn, "Acme")
    _seed_posting(db_conn, "p-case", company_id, title="Data Engineer", last_seen=_BASE_TIME)

    assert distinct_titles(db_conn, q="data engineer", limit=50) == ["Data Engineer"]
    assert distinct_titles(db_conn, q="DATA", limit=50) == ["Data Engineer"]


def test_distinct_titles_q_escapes_percent_and_underscore(db_conn):
    """Same LIKE-metacharacter hazard `_build_filters` already guards against
    for title_contains/location_contains (test_like_metacharacters_in_filter_are_escaped
    above) — q="%" or q="_" must match only a title literally containing
    that character, not every row."""
    from jobcannon.db._feed import distinct_titles

    company_id = _seed_company(db_conn, "Acme")
    percent_title = "50% Remote Engineer"
    underscore_title = "Back_End Developer"
    _seed_posting(db_conn, "p-pct", company_id, title=percent_title, last_seen=_BASE_TIME)
    _seed_posting(db_conn, "p-under", company_id, title=underscore_title, last_seen=_BASE_TIME)
    _seed_posting(
        db_conn, "p-plain", company_id, title="Plain Title With No Metachars", last_seen=_BASE_TIME
    )

    percent_matches = distinct_titles(db_conn, q="%", limit=50)
    assert percent_matches == [percent_title]

    underscore_matches = distinct_titles(db_conn, q="_", limit=50)
    assert underscore_matches == [underscore_title]


def test_distinct_titles_q_no_match_returns_empty_list(db_conn):
    from jobcannon.db._feed import distinct_titles

    company_id = _seed_company(db_conn, "Acme")
    _seed_posting(db_conn, "p-nomatch", company_id, title="Product Manager", last_seen=_BASE_TIME)

    assert distinct_titles(db_conn, q="Zzzznonexistent", limit=50) == []


def test_distinct_companies_q_finds_and_ranks_prefix_first(db_conn):
    """distinct_companies reads postings.company (like distinct_titles reads
    postings.title) — not the companies table, which only companies with an
    actual posting need seeding here."""
    from jobcannon.db._feed import distinct_companies

    company_id = _seed_company(db_conn, "Acme")
    _seed_posting(
        db_conn, "p-co-prefix", company_id, company="Zeta Acquisitions", last_seen=_BASE_TIME
    )
    _seed_posting(
        db_conn, "p-co-substr", company_id, company="Beta Zeta Holdings", last_seen=_BASE_TIME
    )

    matches = distinct_companies(db_conn, q="Zeta", limit=50)

    assert matches == ["Zeta Acquisitions", "Beta Zeta Holdings"]


# --- #156: keyset cursor pagination ----------------------------------------


def test_cursor_pagination_across_three_pages_no_duplicates_no_skips(db_conn):
    """26 unranked postings at 1-second last_seen increments, paged at
    limit=10: three pages (10, 10, 6) must union back to exactly the
    original 26 ids with no duplicate and no gap, in the same order a
    single unpaged limit=26 read would return them."""
    from jobcannon.db._feed import cursor_from_row, list_feed_postings

    company_id = _seed_company(db_conn, "Acme")
    ids = [
        _seed_posting(
            db_conn, f"p-cur-{i}", company_id, last_seen=_BASE_TIME + timedelta(seconds=i)
        )
        for i in range(26)
    ]
    expected_order = list(reversed(ids))  # newest last_seen first

    page1 = list_feed_postings(db_conn, limit=10)
    assert [r["id"] for r in page1] == expected_order[0:10]

    page2 = list_feed_postings(db_conn, limit=10, after=_after_from_row(page1[-1]))
    assert [r["id"] for r in page2] == expected_order[10:20]

    page3 = list_feed_postings(db_conn, limit=10, after=_after_from_row(page2[-1]))
    assert [r["id"] for r in page3] == expected_order[20:26]

    all_seen = [r["id"] for r in page1] + [r["id"] for r in page2] + [r["id"] for r in page3]
    assert all_seen == expected_order  # no duplicates, no skips, order preserved
    assert len(set(all_seen)) == 26

    # cursor_from_row's own output round-trips through parse_cursor the same
    # way a real HTTP request would (exercised separately below); this just
    # confirms it produces the shape list_feed_postings actually consumes.
    assert set(cursor_from_row(page1[-1])) == {"cursor_rank_score", "cursor_last_seen", "cursor_id"}


def test_cursor_pagination_is_deterministic_with_last_seen_ties(db_conn):
    """Multiple postings sharing the exact same last_seen (a real corpus
    scenario — a scan tick can insert many rows in the same instant) must
    still page deterministically: the `id` tiebreaker in `_SORTS["default"]`
    means paging by (last_seen, id) never duplicates or skips a tied row
    across a page boundary."""
    from jobcannon.db._feed import list_feed_postings

    company_id = _seed_company(db_conn, "Acme")
    tied_time = _BASE_TIME + timedelta(hours=1)
    ids = sorted(
        _seed_posting(db_conn, f"p-tie-{i}", company_id, last_seen=tied_time) for i in range(6)
    )
    expected_order = list(reversed(ids))  # tied last_seen -> id DESC

    page1 = list_feed_postings(db_conn, limit=3)
    assert [r["id"] for r in page1] == expected_order[0:3]

    page2 = list_feed_postings(db_conn, limit=3, after=_after_from_row(page1[-1]))
    assert [r["id"] for r in page2] == expected_order[3:6]

    assert set(r["id"] for r in page1) & set(r["id"] for r in page2) == set()


def test_cursor_pagination_orders_ranked_ahead_of_unranked_across_pages(db_conn):
    """Cursor pagination must respect the SAME ordering
    (rank_score DESC NULLS LAST, last_seen DESC, id DESC) an unpaged read
    does — a ranked-but-stale row must still page ahead of a fresh unranked
    one, exactly as test_rank_score_orders_ahead_of_recency_when_present
    proves for a single unpaged read."""
    from jobcannon.db._feed import list_feed_postings

    _seed_user(db_conn, "u-cursor-rank")
    company_id = _seed_company(db_conn, "Acme")
    ranked = _seed_posting(db_conn, "p-cur-ranked", company_id, last_seen=_BASE_TIME)
    fresh_unranked = _seed_posting(
        db_conn, "p-cur-fresh", company_id, last_seen=_BASE_TIME + timedelta(hours=5)
    )
    _seed_feed_state(db_conn, "u-cursor-rank", ranked, 0.9)

    page1 = list_feed_postings(db_conn, user_id="u-cursor-rank", limit=1)
    assert [r["id"] for r in page1] == [ranked]

    page2 = list_feed_postings(
        db_conn, user_id="u-cursor-rank", limit=1, after=_after_from_row(page1[-1])
    )
    assert [r["id"] for r in page2] == [fresh_unranked]


def test_cursor_with_non_default_sort_raises(db_conn):
    from jobcannon.db._feed import list_feed_postings

    with pytest.raises(ValueError):
        list_feed_postings(db_conn, sort="bogus-token", after=(None, _BASE_TIME, 1))


def test_parse_cursor_round_trips_cursor_from_row(db_conn):
    """cursor_from_row's dict, serialized to strings the way a query string
    would carry them, must parse back to the exact `after` tuple that same
    row would produce for `_cursor_predicate` — including the None
    rank_score case, which is every real page today (feed_state has no
    writer anywhere in this codebase)."""
    from jobcannon.db._feed import cursor_from_row, list_feed_postings, parse_cursor

    company_id = _seed_company(db_conn, "Acme")
    _seed_posting(db_conn, "p-roundtrip", company_id, last_seen=_BASE_TIME)
    row = list_feed_postings(db_conn, limit=1)[0]

    cursor_dict = cursor_from_row(row)
    after = parse_cursor(cursor_dict)

    assert after == (None, row["last_seen"], row["id"])


def test_parse_cursor_round_trips_a_real_rank_score(db_conn):
    from jobcannon.db._feed import cursor_from_row, list_feed_postings, parse_cursor

    _seed_user(db_conn, "u-roundtrip-rank")
    company_id = _seed_company(db_conn, "Acme")
    posting_id = _seed_posting(db_conn, "p-roundtrip-rank", company_id, last_seen=_BASE_TIME)
    _seed_feed_state(db_conn, "u-roundtrip-rank", posting_id, 0.42)
    row = list_feed_postings(db_conn, user_id="u-roundtrip-rank", limit=1)[0]

    after = parse_cursor(cursor_from_row(row))

    assert after == (pytest.approx(0.42), row["last_seen"], row["id"])


def test_parse_cursor_missing_id_returns_none():
    from jobcannon.db._feed import parse_cursor

    assert parse_cursor({}) is None
    assert parse_cursor({"cursor_id": ""}) is None


def test_parse_cursor_malformed_values_degrade_to_none_not_raise():
    """A tampered/malformed cursor must never 500 the route consuming it —
    parse_cursor degrades to None (render a first page) instead."""
    from jobcannon.db._feed import parse_cursor

    assert (
        parse_cursor({"cursor_id": "not-a-number", "cursor_last_seen": "2026-01-01T00:00:00+00:00"})
        is None
    )
    assert parse_cursor({"cursor_id": "1", "cursor_last_seen": "not-a-timestamp"}) is None
    assert (
        parse_cursor(
            {
                "cursor_id": "1",
                "cursor_last_seen": "2026-01-01T00:00:00+00:00",
                "cursor_rank_score": "nan-ish",
            }
        )
        is None
    )

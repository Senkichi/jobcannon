"""jobcannon.db._feed's L-0076 sort tokens (title/company/location/
salary_min/salary_max/recency) -- ported subset of private's
job_finder/db/_queries.py::get_filtered_jobs allowed_sort_cols allowlist,
see jobcannon/db/_feed.py's module docstring for full scoping rationale."""

from __future__ import annotations

from datetime import date, datetime, timezone

from jobcannon.db._feed import list_feed_postings
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


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
    salary_min=None,
    salary_max=None,
    posted_date=None,
    first_seen=None,
):
    columns = ["dedup_key", "company_id", "title", "company"]
    values = [dedup_key, company_id, title, company]
    for col, val in (
        ("location", location),
        ("salary_min", salary_min),
        ("salary_max", salary_max),
        ("first_seen", first_seen),
    ):
        if val is not None:
            columns.append(col)
            values.append(val)
    if posted_date is not None:
        # m0001 CHECK: (posted_date IS NULL) = (posted_date_precision IS NULL)
        columns.extend(["posted_date", "posted_date_precision"])
        values.extend([posted_date, "exact"])
    placeholders = ", ".join(["%s"] * len(values))
    cols_sql = ", ".join(columns)
    return conn.execute(
        f"INSERT INTO postings ({cols_sql}) VALUES ({placeholders}) RETURNING id",
        values,
    ).fetchone()["id"]


def test_title_sort_is_alphabetical_ascending(db_conn):
    company = _seed_company(db_conn, "sort-title-co")
    _seed_posting(db_conn, "sort-title|1", company, title="Zebra Engineer")
    _seed_posting(db_conn, "sort-title|2", company, title="Alpha Engineer")

    rows = list_feed_postings(db_conn, sort="title")

    assert [r["title"] for r in rows] == ["Alpha Engineer", "Zebra Engineer"]


def test_company_sort_is_alphabetical_ascending(db_conn):
    zebra = _seed_company(db_conn, "sort-zebra-co")
    alpha = _seed_company(db_conn, "sort-alpha-co")
    _seed_posting(db_conn, "sort-co|1", zebra, company="sort-zebra-co")
    _seed_posting(db_conn, "sort-co|2", alpha, company="sort-alpha-co")

    rows = list_feed_postings(db_conn, sort="company", companies=["sort-zebra-co", "sort-alpha-co"])

    assert [r["company"] for r in rows] == ["sort-alpha-co", "sort-zebra-co"]


def test_location_sort_nulls_last(db_conn):
    company = _seed_company(db_conn, "sort-loc-co")
    _seed_posting(db_conn, "sort-loc|1", company, location=None)
    _seed_posting(db_conn, "sort-loc|2", company, location="Austin, TX")

    rows = list_feed_postings(db_conn, sort="location")

    assert rows[0]["location"] == "Austin, TX"
    assert rows[-1]["location"] is None


def test_salary_min_sort_is_descending_nulls_last(db_conn):
    company = _seed_company(db_conn, "sort-sal-co")
    _seed_posting(db_conn, "sort-sal|1", company, salary_min=None)
    _seed_posting(db_conn, "sort-sal|2", company, salary_min=90000)
    _seed_posting(db_conn, "sort-sal|3", company, salary_min=150000)

    rows = list_feed_postings(db_conn, sort="salary_min")

    assert [float(r["salary_min"]) if r["salary_min"] is not None else None for r in rows] == [
        150000.0,
        90000.0,
        None,
    ]


def test_recency_sort_prefers_posted_date_falls_back_to_first_seen(db_conn):
    company = _seed_company(db_conn, "sort-rec-co")
    # No posted_date -> falls back to first_seen (older).
    _seed_posting(
        db_conn,
        "sort-rec|1",
        company,
        first_seen=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    # Explicit posted_date newer than the other row's first_seen fallback.
    _seed_posting(
        db_conn,
        "sort-rec|2",
        company,
        posted_date=date(2026, 6, 1),
        first_seen=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    rows = list_feed_postings(db_conn, sort="recency")

    assert [r["posted_date"] for r in rows] == [date(2026, 6, 1), None]


def test_unknown_sort_token_still_raises(db_conn):
    import pytest

    with pytest.raises(ValueError, match="unknown sort token"):
        list_feed_postings(db_conn, sort="not-a-real-token")

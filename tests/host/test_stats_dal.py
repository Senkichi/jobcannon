"""corpus_stats DAL: read-only counts feeding the demo/feed empty-state
shells (1B Wave 3 PR 11, Step 4b)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def _seed_company(conn, name):
    return conn.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status) "
        "VALUES (%s, %s, 'jobvite', %s, 'hit') RETURNING id",
        (name, name, name.lower()),
    ).fetchone()["id"]


def _seed_posting(conn, dedup_key, company_id, last_seen):
    conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, last_seen) "
        "VALUES (%s, %s, 'Engineer', 'Acme', %s)",
        (dedup_key, company_id, last_seen),
    )


def test_corpus_stats_empty(db_conn):
    from jobcannon.db._stats import corpus_stats

    assert corpus_stats(db_conn) == {
        "postings": 0,
        "companies": 0,
        "freshest_last_seen": None,
    }


def test_corpus_stats_with_data_returns_counts_and_freshest(db_conn):
    from jobcannon.db._stats import corpus_stats

    now = datetime.now(timezone.utc)
    c1 = _seed_company(db_conn, "Acme")
    c2 = _seed_company(db_conn, "Globex")
    _seed_posting(db_conn, "p1", c1, now - timedelta(hours=2))
    _seed_posting(db_conn, "p2", c1, now - timedelta(hours=1))
    freshest = now
    _seed_posting(db_conn, "p3", c2, freshest)

    stats = corpus_stats(db_conn)
    assert stats["postings"] == 3
    assert stats["companies"] == 2
    assert stats["freshest_last_seen"] == freshest


@pytest.fixture()
def opened_pool():
    """Own throwaway database, pool-backed — needed to exercise pool.py's
    actual `configure` hook (the UTC session-timezone pin). The
    rollback-isolated `db_conn` fixture other tests in this module use is a
    raw psycopg.connect(), never routed through
    jobcannon.db.pool.ConnectionPool, so it would not pick up the pin.
    Mirrors tests/host/test_connection_factory.py's fixture of the same
    name/shape."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations

    dsn, db_name = create_throwaway_db("jobcannon_stats_tz")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        yield pool_mod
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def test_corpus_stats_freshest_last_seen_is_utc(opened_pool):
    """Pins pool.py's `_configure` hook (SET TIME ZONE 'UTC' on every new
    pooled connection): the local Postgres service here defaults its
    session TimeZone GUC to America/Los_Angeles, so without the pin this
    would come back with a non-zero utcoffset — silently contradicting the
    hardcoded " UTC" suffix feed.html/demo.html render next to this value."""
    from jobcannon.db._stats import corpus_stats
    from jobcannon.db.pool import connection_factory

    with connection_factory() as conn:
        company_id = _seed_company(conn.raw, "Acme")
        _seed_posting(conn.raw, "tz-check", company_id, datetime.now(timezone.utc))
        stats = corpus_stats(conn)

    freshest = stats["freshest_last_seen"]
    assert freshest.tzinfo is not None
    assert freshest.utcoffset() == timedelta(0)

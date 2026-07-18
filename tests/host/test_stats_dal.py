"""corpus_stats DAL: read-only counts feeding the demo/feed empty-state
shells (1B Wave 3 PR 11, Step 4b)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


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

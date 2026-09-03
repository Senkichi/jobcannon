# PORTED from tests/test_dashboard_crawl_latency.py @ 694ac4d08d0f98c322f050b2804894917cdeb64a (private job-cannon). Ledger L-0485.
"""Tests for crawl latency SLI dashboard query.

# PORT-SEAM: test_pre_m095_db_graceful dropped -- it targets private's
# pre-migration "missing posted_date_precision column" except/fallback
# branch, which get_crawl_latency_sli's OWN docstring explicitly documents
# as dropped on this host ("posted_date_precision has existed on this
# host's postings table since m0001, there is no pre-migration DB state to
# guard against here" -- jobcannon/db/_scan_observability.py). Kept tests
# partially overlap tests/host/test_scan_observability.py's pre-existing
# test_crawl_latency_sli_computes_percentiles /
# test_crawl_latency_sli_excludes_same_day_copy_artifacts /
# test_crawl_latency_sli_respects_config_cold_start_days; ported anyway per
# the L-0509 precedent (port despite overlap, note it), and this file adds
# genuinely new coverage those 3 don't have (proxy/approximate exclusion,
# negative-latency/clock-skew exclusion, empty-set graceful zeroing).
"""

from datetime import date, datetime, timezone

from jobcannon.db._scan_observability import get_crawl_latency_sli
# PORT-SEAM: sqlite3 dropped -- Postgres via db_conn; only
# test_pre_m095_db_graceful used it directly, and that test is dropped
# (see module docstring).

# PORT-SEAM: db_conn/postgres_test_dsn/requires_postgres imported directly
# from tests.host.conftest -- no root tests/conftest.py exists to make
# tests/host/'s fixtures visible outside that subtree.
from tests.host.conftest import db_conn, postgres_test_dsn, requires_postgres  # noqa: F401

pytestmark = requires_postgres


def _insert_company(conn, name):
    # PORT-SEAM: companies.id is a real bigserial PK + postings.company_id
    # is a real FK on this host (unlike private's untyped sqlite3 jobs
    # table), so every posting row needs a real companies row first.
    row = conn.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    return row["id"]


def _insert_posting(conn, dedup_key, company_id, posted_date, first_seen, precision="exact"):
    # PORT-SEAM: private inserted into jobs(...) (dedup_key, title, company,
    # location, first_seen, last_seen, posted_date, posted_date_precision,
    # sources) as bare TEXT date strings via executemany(); this host's
    # postings(...) needs a real company_id FK, and first_seen/posted_date
    # are typed timestamptz/date -- Python date/datetime objects are passed
    # directly rather than TEXT strings.
    conn.execute(
        "INSERT INTO postings"
        " (dedup_key, company_id, title, company, posted_date, posted_date_precision, first_seen, last_seen)"
        " VALUES (%s, %s, 'Job', 'Co', %s, %s, %s, %s)",
        (dedup_key, company_id, posted_date, precision, first_seen, first_seen),
    )
    # PORT-SEAM: conn.commit() dropped -- db_conn fixture owns transaction
    # lifecycle for the whole test; explicit commit() is not permitted.


# PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
def test_p50_p95_p99_from_known_ladder(db_conn):  # noqa: F811
    """Percentiles computed correctly from known latency ladder."""
    conn = db_conn
    # PORT-SEAM: private's 5-row executemany() (identical latency ladder)
    # replaced with 5 _insert_posting() calls against a shared company.
    company_id = _insert_company(conn, "ladder-co")
    posted = date(2026, 1, 1)
    for dedup_key, days in (("job1", 1), ("job2", 1), ("job3", 2), ("job4", 5), ("job5", 10)):
        _insert_posting(
            conn, dedup_key, company_id, posted, datetime(2026, 1, 1 + days, tzinfo=timezone.utc)
        )

    config = {}
    result = get_crawl_latency_sli(conn, config)

    # Latencies: [1, 1, 2, 5, 10] → p50=2, p95=10, p99=10
    assert result["sample_n"] == 5
    assert result["p50_days"] == 2.0
    assert result["p95_days"] == 10.0
    assert result["p99_days"] == 10.0
    # No mean/avg key (percentiles only)
    assert "mean" not in result
    assert "avg" not in result


# PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
def test_copy_row_excluded(db_conn):  # noqa: F811
    """Rows where posted_date == first_seen are excluded (m095-copy guard)."""
    conn = db_conn
    # PORT-SEAM: private's 2-row executemany() replaced with 2
    # _insert_posting() calls against a shared company.
    company_id = _insert_company(conn, "copyrow-co")
    _insert_posting(
        conn, "copy", company_id, date(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    _insert_posting(
        conn, "real", company_id, date(2026, 1, 1), datetime(2026, 1, 2, tzinfo=timezone.utc)
    )

    config = {}
    result = get_crawl_latency_sli(conn, config)

    # Only the real row qualifies
    assert result["sample_n"] == 1
    assert result["p50_days"] == 1.0
    # Copy row does NOT create a 0-day floor
    assert result["p50_days"] != 0.0


# PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
def test_proxy_and_approximate_excluded_from_headline(db_conn):  # noqa: F811
    """proxy/approximate precision rows excluded from headline, counted in total_dated."""
    conn = db_conn
    # PORT-SEAM: private's 4-row executemany() (mix of exact/proxy/approximate)
    # replaced with 4 _insert_posting() calls against a shared company.
    company_id = _insert_company(conn, "precision-co")
    posted = date(2026, 1, 1)
    _insert_posting(conn, "exact1", company_id, posted, datetime(2026, 1, 2, tzinfo=timezone.utc))
    _insert_posting(conn, "exact2", company_id, posted, datetime(2026, 1, 3, tzinfo=timezone.utc))
    _insert_posting(
        conn,
        "proxy",
        company_id,
        posted,
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        precision="proxy",
    )
    _insert_posting(
        conn,
        "approx",
        company_id,
        posted,
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        precision="approximate",
    )

    config = {}
    result = get_crawl_latency_sli(conn, config)

    # Only exact rows qualify for headline
    assert result["sample_n"] == 2
    assert result["total_dated"] == 4
    # Coverage reflects the drop
    assert result["exact_coverage_pct"] == 50.0


# PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
def test_cold_start_backlog_excluded(db_conn):  # noqa: F811
    """Rows with latency > cold_start_exclude_days are excluded."""
    conn = db_conn
    # PORT-SEAM: private's 2-row executemany() (normal + 90-day backlog)
    # replaced with 2 _insert_posting() calls against a shared company.
    company_id = _insert_company(conn, "backlog-co")
    posted = date(2026, 1, 1)
    _insert_posting(conn, "normal", company_id, posted, datetime(2026, 1, 2, tzinfo=timezone.utc))
    _insert_posting(conn, "backlog", company_id, posted, datetime(2026, 4, 1, tzinfo=timezone.utc))

    # Default 30-day window excludes backlog
    config = {}
    result = get_crawl_latency_sli(conn, config)
    assert result["sample_n"] == 1
    assert result["cold_start_exclude_days"] == 30

    # 120-day window includes backlog
    config = {"metrics": {"crawl_latency": {"cold_start_exclude_days": 120}}}
    result = get_crawl_latency_sli(conn, config)
    assert result["sample_n"] == 2
    assert result["cold_start_exclude_days"] == 120


# PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
def test_negative_latency_excluded(db_conn):  # noqa: F811
    """Negative latencies (clock skew: posted_date > first_seen) are excluded."""
    conn = db_conn
    # PORT-SEAM: private's 2-row executemany() (normal + clock-skew row)
    # replaced with 2 _insert_posting() calls against a shared company.
    company_id = _insert_company(conn, "skew-co")
    _insert_posting(
        conn, "normal", company_id, date(2026, 1, 1), datetime(2026, 1, 2, tzinfo=timezone.utc)
    )
    _insert_posting(
        conn, "skew", company_id, date(2026, 1, 2), datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    config = {}
    result = get_crawl_latency_sli(conn, config)

    # Only normal row qualifies
    assert result["sample_n"] == 1
    assert result["p50_days"] == 1.0


# PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).
def test_empty_set_returns_none_percentiles(db_conn):  # noqa: F811
    """No qualifying rows → None percentiles, sample_n=0, no crash."""
    conn = db_conn  # PORT-SEAM: migrated_db_mem -> db_conn (shared Postgres fixture).

    config = {}
    result = get_crawl_latency_sli(conn, config)

    assert result["sample_n"] == 0
    assert result["total_dated"] == 0
    assert result["p50_days"] is None
    assert result["p95_days"] is None
    assert result["p99_days"] is None
    assert result["exact_coverage_pct"] == 0.0


# PORT-SEAM: test_pre_m095_db_graceful (raw sqlite3.connect against a
# hand-built pre-m095 schema) dropped here -- see module docstring.

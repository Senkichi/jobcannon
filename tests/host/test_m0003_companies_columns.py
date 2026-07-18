import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres

_EXPECTED = {
    "name_raw": "text",
    "retry_count": "integer",
    "retry_after": "timestamp with time zone",
    "miss_reason": "text",
    "careers_crawl_last_at": "timestamp with time zone",
    "jobs_found_total": "integer",
    "last_scan_postings_json": "jsonb",
    "last_scan_cached_at": "timestamp with time zone",
}


def test_companies_scan_columns_exist(db_conn):
    rows = db_conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'companies'"
    ).fetchall()
    actual = {r["column_name"]: r["data_type"] for r in rows}
    for col, dtype in _EXPECTED.items():
        assert actual.get(col) == dtype, f"{col}: expected {dtype}, got {actual.get(col)}"


def test_ats_probe_status_allows_error(db_conn):
    db_conn.execute(
        "INSERT INTO companies (name, name_raw, ats_probe_status) "
        "VALUES ('ErrCo', 'ErrCo', 'error')"
    )
    row = db_conn.execute("SELECT ats_probe_status FROM companies WHERE name = 'ErrCo'").fetchone()
    assert row["ats_probe_status"] == "error"


def test_ats_probe_status_still_rejects_garbage(db_conn):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO companies (name, name_raw, ats_probe_status) "
            "VALUES ('BadCo', 'BadCo', 'bogus')"
        )

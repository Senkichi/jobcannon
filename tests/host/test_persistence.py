"""Host-dialect tests for jobcannon.db._persistence (ledger L-0073).

Scope note: covers log_run / persist_job_expiry_state only -- the two
functions this port lands. persist_job_notes / update_pipeline_status /
set_job_flag are not ported (see the module's own docstring for why), so
they have no tests here either.
"""

from __future__ import annotations

from jobcannon.db._persistence import log_run, persist_job_expiry_state
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _svc_conn(db_conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(db_conn)


def _posting(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('persist-co')")
    cid = db_conn.execute("SELECT id FROM companies WHERE name='persist-co'").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES ('persist-co|engineer', %s, 'Engineer', 'persist-co')",
        (cid,),
    )
    return "persist-co|engineer"


# --- log_run ------------------------------------------------------------


def test_log_run_writes_a_row(db_conn):
    conn = _svc_conn(db_conn)
    log_run(conn, "serpapi", fetched=10, new=3, scored=2, metadata={"batch": "a"})

    row = db_conn.execute(
        "SELECT source, jobs_fetched, jobs_new, jobs_scored, metadata FROM runs"
    ).fetchone()
    assert row["source"] == "serpapi"
    assert row["jobs_fetched"] == 10
    assert row["jobs_new"] == 3
    assert row["jobs_scored"] == 2
    assert row["metadata"] == {"batch": "a"}


def test_log_run_defaults_metadata_to_empty_dict(db_conn):
    conn = _svc_conn(db_conn)
    log_run(conn, "gmail", fetched=1, new=1, scored=0)

    row = db_conn.execute("SELECT metadata FROM runs WHERE source = 'gmail'").fetchone()
    assert row["metadata"] == {}


def test_log_run_appends_multiple_rows(db_conn):
    conn = _svc_conn(db_conn)
    log_run(conn, "serpapi", fetched=1, new=1, scored=0)
    log_run(conn, "serpapi", fetched=2, new=2, scored=1)

    count = db_conn.execute("SELECT COUNT(*) AS n FROM runs WHERE source = 'serpapi'").fetchone()
    assert count["n"] == 2


# --- persist_job_expiry_state --------------------------------------------


def test_persist_expiry_state_live_refreshes_last_seen_and_clears_stale(db_conn):
    conn = _svc_conn(db_conn)
    dedup_key = _posting(db_conn)
    db_conn.execute("UPDATE postings SET is_stale = true WHERE dedup_key = %s", (dedup_key,))

    persist_job_expiry_state(conn, dedup_key, "live", "2026-09-01T12:00:00+00:00")

    row = db_conn.execute(
        "SELECT expiry_status, expiry_checked_at, is_stale FROM postings WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()
    assert row["expiry_status"] == "live"
    assert row["expiry_checked_at"] is not None
    assert row["is_stale"] is False


def test_persist_expiry_state_expired_sets_status_and_checked_at(db_conn):
    conn = _svc_conn(db_conn)
    dedup_key = _posting(db_conn)

    persist_job_expiry_state(conn, dedup_key, "expired", "2026-09-01T12:00:00+00:00")

    row = db_conn.execute(
        "SELECT expiry_status, expiry_checked_at FROM postings WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()
    assert row["expiry_status"] == "expired"
    assert row["expiry_checked_at"] is not None


def test_persist_expiry_state_inconclusive_does_not_advance_checked_at(db_conn):
    conn = _svc_conn(db_conn)
    dedup_key = _posting(db_conn)
    persist_job_expiry_state(conn, dedup_key, "expired", "2026-09-01T12:00:00+00:00")
    row_before = db_conn.execute(
        "SELECT expiry_checked_at FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()

    persist_job_expiry_state(conn, dedup_key, "inconclusive", "2026-09-02T12:00:00+00:00")

    row_after = db_conn.execute(
        "SELECT expiry_status, expiry_checked_at FROM postings WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()
    assert row_after["expiry_status"] == "inconclusive"
    assert row_after["expiry_checked_at"] == row_before["expiry_checked_at"]


def test_persist_expiry_state_missing_dedup_key_is_a_noop(db_conn):
    conn = _svc_conn(db_conn)
    # No row exists for this key -- the UPDATE matches zero rows and the
    # function returns without raising (matches Postgres UPDATE-no-match
    # semantics, no existence pre-check).
    persist_job_expiry_state(conn, "does-not-exist", "live", "2026-09-01T12:00:00+00:00")

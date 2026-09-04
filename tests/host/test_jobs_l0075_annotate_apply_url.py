"""Tests for L-0075's flat re-adaptation: annotate_posting_apply_url
(jobcannon/db/_jobs.py), ported from job_finder/db/_postings.py.

The private original keyed the write on (ats_platform, source_id) to find
one descriptor inside the jobs.postings JSON array. This host's postings
table has no descriptor sub-entity (dedup_key alone identifies the single
target row -- see the PORT-SEAM block above the ported function), so this
suite exercises the flat signature: annotate_posting_apply_url(conn,
dedup_key, aggregator_apply_url).
"""

import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _parsed(
    title="Staff Data Engineer",
    company="Acme",
    source="ashby",
    source_url="https://jobs.ashbyhq.com/acme/1",
    **meta,
):
    from jobcannon.engine.models import Job
    from jobcannon.engine.parsed_job import ParsedJob

    job = Job(
        title=title,
        company=company,
        location=meta.pop("location", "Remote"),
        source=source,
        source_url=source_url,
    )
    parsed = ParsedJob.from_job(job, source_meta=meta or None)
    assert isinstance(parsed, ParsedJob), f"clean inputs came back unresolved: {parsed}"
    return parsed


@pytest.fixture()
def company_id(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('acme')")
    return db_conn.execute("SELECT id FROM companies WHERE name='acme'").fetchone()["id"]


def _svc_conn(db_conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(db_conn)


def test_annotate_writes_when_row_matches(db_conn, company_id):
    from jobcannon.db._jobs import annotate_posting_apply_url, upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed()
    upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")

    ok = annotate_posting_apply_url(conn, parsed.dedup_key, "https://www.linkedin.com/jobs/view/1")
    assert ok is True
    row = db_conn.execute(
        "SELECT aggregator_apply_url FROM postings WHERE dedup_key = %s", (parsed.dedup_key,)
    ).fetchone()
    assert row["aggregator_apply_url"] == "https://www.linkedin.com/jobs/view/1"


def test_annotate_overwrites_existing_value(db_conn, company_id):
    """Re-sighting the same aggregator link (or a changed one) overwrites, not merges --
    there is no sibling descriptor to preserve on the flat table (see PORT-SEAM note)."""
    from jobcannon.db._jobs import annotate_posting_apply_url, upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed()
    upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")

    assert annotate_posting_apply_url(conn, parsed.dedup_key, "https://old.example/1") is True
    assert annotate_posting_apply_url(conn, parsed.dedup_key, "https://new.example/1") is True

    row = db_conn.execute(
        "SELECT aggregator_apply_url FROM postings WHERE dedup_key = %s", (parsed.dedup_key,)
    ).fetchone()
    assert row["aggregator_apply_url"] == "https://new.example/1"


def test_annotate_noop_when_no_row_matches(db_conn):
    from jobcannon.db._jobs import annotate_posting_apply_url

    conn = _svc_conn(db_conn)
    assert annotate_posting_apply_url(conn, "no-such-dedup-key", "https://x.example/1") is False


def test_annotate_noop_when_inputs_missing(db_conn, company_id):
    from jobcannon.db._jobs import annotate_posting_apply_url, upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed()
    upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")

    assert annotate_posting_apply_url(conn, "", "https://x.example/1") is False
    assert annotate_posting_apply_url(conn, parsed.dedup_key, "") is False
    assert annotate_posting_apply_url(conn, parsed.dedup_key, None) is False

    row = db_conn.execute(
        "SELECT aggregator_apply_url FROM postings WHERE dedup_key = %s", (parsed.dedup_key,)
    ).fetchone()
    assert row["aggregator_apply_url"] is None


def test_annotate_does_not_disturb_sibling_row(db_conn, company_id):
    from jobcannon.db._jobs import annotate_posting_apply_url, upsert_job

    conn = _svc_conn(db_conn)
    p1 = _parsed(title="Staff Data Engineer", source_url="https://jobs.ashbyhq.com/acme/1")
    p2 = _parsed(title="Principal Backend Engineer", source_url="https://jobs.ashbyhq.com/acme/2")
    upsert_job(conn, p1, company_id=company_id, ats_platform="ashby")
    upsert_job(conn, p2, company_id=company_id, ats_platform="ashby")

    assert annotate_posting_apply_url(conn, p1.dedup_key, "https://agg.example/1") is True

    row2 = db_conn.execute(
        "SELECT aggregator_apply_url FROM postings WHERE dedup_key = %s", (p2.dedup_key,)
    ).fetchone()
    assert row2["aggregator_apply_url"] is None

"""Tests for L-0070's ported auxiliary helpers: get_job, load_job_context,
set_source_id_if_free (jobcannon/db/_jobs.py)."""

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


@pytest.fixture()
def other_company_id(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('globex')")
    return db_conn.execute("SELECT id FROM companies WHERE name='globex'").fetchone()["id"]


def _svc_conn(db_conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(db_conn)


def test_get_job_found(db_conn, company_id):
    from jobcannon.db._jobs import get_job, upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed()
    upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")

    job = get_job(conn, parsed.dedup_key)
    assert job is not None
    assert job["dedup_key"] == parsed.dedup_key
    assert job["title"] == "Staff Data Engineer"


def test_get_job_not_found(db_conn):
    from jobcannon.db._jobs import get_job

    conn = _svc_conn(db_conn)
    assert get_job(conn, "no-such-dedup-key") is None


def test_load_job_context_found(db_conn, company_id):
    from jobcannon.db._jobs import load_job_context, upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed()
    upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")

    ctx = load_job_context(conn, parsed.dedup_key)
    assert ctx is not None
    assert ctx["job"]["dedup_key"] == parsed.dedup_key


def test_load_job_context_not_found(db_conn):
    from jobcannon.db._jobs import load_job_context

    conn = _svc_conn(db_conn)
    assert load_job_context(conn, "no-such-dedup-key") is None


def test_set_source_id_if_free_writes_when_free(db_conn, company_id):
    from jobcannon.db._jobs import set_source_id_if_free, upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed()
    upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")

    ok = set_source_id_if_free(conn, parsed.dedup_key, company_id, "req-99")
    assert ok is True
    row = db_conn.execute(
        "SELECT source_id FROM postings WHERE dedup_key = %s", (parsed.dedup_key,)
    ).fetchone()
    assert row["source_id"] == "req-99"


def test_set_source_id_if_free_noop_when_already_set(db_conn, company_id):
    from jobcannon.db._jobs import set_source_id_if_free, upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed()
    upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")
    assert set_source_id_if_free(conn, parsed.dedup_key, company_id, "req-1") is True

    # Second write attempt must no-op — row already carries a source_id.
    assert set_source_id_if_free(conn, parsed.dedup_key, company_id, "req-2") is False
    row = db_conn.execute(
        "SELECT source_id FROM postings WHERE dedup_key = %s", (parsed.dedup_key,)
    ).fetchone()
    assert row["source_id"] == "req-1"


def test_set_source_id_if_free_noop_when_inputs_missing(db_conn, company_id):
    from jobcannon.db._jobs import set_source_id_if_free, upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed()
    upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")

    # Missing source_id.
    assert set_source_id_if_free(conn, parsed.dedup_key, company_id, None) is False
    assert set_source_id_if_free(conn, parsed.dedup_key, company_id, "") is False
    # Missing company_id.
    assert set_source_id_if_free(conn, parsed.dedup_key, None, "req-1") is False
    # Missing dedup_key.
    assert set_source_id_if_free(conn, "", company_id, "req-1") is False


def test_set_source_id_if_free_noop_when_held_by_another_row(db_conn, company_id):
    from jobcannon.db._jobs import set_source_id_if_free, upsert_job

    conn = _svc_conn(db_conn)
    p1 = _parsed(title="Staff Data Engineer", source_url="https://jobs.ashbyhq.com/acme/1")
    p2 = _parsed(title="Principal Backend Engineer", source_url="https://jobs.ashbyhq.com/acme/2")
    upsert_job(conn, p1, company_id=company_id, ats_platform="ashby")
    upsert_job(conn, p2, company_id=company_id, ats_platform="ashby")

    assert set_source_id_if_free(conn, p1.dedup_key, company_id, "req-shared") is True
    # p2 tries to claim the same (company_id, source_id) pair already held by p1.
    assert set_source_id_if_free(conn, p2.dedup_key, company_id, "req-shared") is False
    row = db_conn.execute(
        "SELECT source_id FROM postings WHERE dedup_key = %s", (p2.dedup_key,)
    ).fetchone()
    assert row["source_id"] is None

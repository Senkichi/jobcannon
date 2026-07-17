import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _parsed(title="Staff Data Engineer", company="Acme", source="ashby",
            source_url="https://jobs.ashbyhq.com/acme/1", **meta):
    from jobcannon.engine.models import Job
    from jobcannon.engine.parsed_job import ParsedJob

    # Job's required fields verified against jobcannon/engine/models.py:21-26
    # (source and source_url have NO defaults — they are required).
    job = Job(title=title, company=company, location=meta.pop("location", "Remote"),
              source=source, source_url=source_url)
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


def test_insert_then_unchanged(db_conn, company_id):
    from jobcannon.db._jobs import upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed()
    r1 = upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")
    assert r1.kind == "inserted"
    assert r1.dedup_key == parsed.dedup_key
    r2 = upsert_job(conn, parsed, company_id=company_id, ats_platform="ashby")
    assert r2.kind == "unchanged"


def test_new_source_is_touched_not_updated(db_conn, company_id):
    from jobcannon.db._jobs import upsert_job

    conn = _svc_conn(db_conn)
    upsert_job(conn, _parsed(), company_id=company_id)
    r = upsert_job(
        conn,
        _parsed(source="lever", source_url="https://jobs.lever.co/acme/2"),
        company_id=company_id,
    )
    assert r.kind == "touched"
    row = db_conn.execute("SELECT sources FROM postings").fetchone()
    assert set(row["sources"]) == {"ashby", "lever"}


def test_posted_date_strict_precedence(db_conn, company_id):
    from datetime import datetime, timezone

    from jobcannon.db._jobs import upsert_job

    conn = _svc_conn(db_conn)
    p1 = _parsed()
    p1.posted_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p1.posted_date_precision = "approximate"
    upsert_job(conn, p1, company_id=company_id)

    # Equal precision must NOT overwrite (strict >).
    p2 = _parsed()
    p2.posted_date = datetime(2026, 2, 2, tzinfo=timezone.utc)
    p2.posted_date_precision = "approximate"
    upsert_job(conn, p2, company_id=company_id)
    row = db_conn.execute("SELECT posted_date FROM postings").fetchone()
    assert str(row["posted_date"]) == "2026-01-01"

    # Higher precision DOES overwrite.
    p3 = _parsed()
    p3.posted_date = datetime(2026, 3, 3, tzinfo=timezone.utc)
    p3.posted_date_precision = "exact"
    r3 = upsert_job(conn, p3, company_id=company_id)
    assert r3.kind == "updated"
    row = db_conn.execute("SELECT posted_date, posted_date_precision FROM postings").fetchone()
    assert str(row["posted_date"]) == "2026-03-03"
    assert row["posted_date_precision"] == "exact"


def test_secondary_match_by_company_source_id(db_conn, company_id):
    from jobcannon.db._jobs import upsert_job

    conn = _svc_conn(db_conn)
    p1 = _parsed(title="Data Engineer II")
    p1.source_id = "req-42"
    upsert_job(conn, p1, company_id=company_id)
    # Same requisition, retitled — dedup_key differs but (company_id, source_id) matches.
    p2 = _parsed(title="Data Engineer 2")
    p2.source_id = "req-42"
    r = upsert_job(conn, p2, company_id=company_id)
    assert r.kind in ("updated", "touched", "unchanged")  # merged, NOT a second insert
    count = db_conn.execute("SELECT count(*) AS n FROM postings").fetchone()["n"]
    assert count == 1


def test_upsert_result_bool_raises(db_conn, company_id):
    from jobcannon.db._jobs import upsert_job

    r = upsert_job(_svc_conn(db_conn), _parsed(), company_id=company_id)
    with pytest.raises(TypeError):
        bool(r)


def test_sightings_recorded_per_source(db_conn, company_id):
    from jobcannon.db._jobs import upsert_job

    conn = _svc_conn(db_conn)
    upsert_job(conn, _parsed(), company_id=company_id)
    upsert_job(conn, _parsed(), company_id=company_id)
    row = db_conn.execute("SELECT sightings FROM postings").fetchone()
    assert len(row["sightings"]) == 1  # re-sight updates last_seen, does not append a duplicate
    entry = row["sightings"][0]
    assert entry["source"] == "ashby"
    assert "ashbyhq.com" in entry["source_url"]  # canonicalized form of the helper's default URL
    assert entry["first_seen"] <= entry["last_seen"]

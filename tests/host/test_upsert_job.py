from urllib.parse import urlsplit

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

    # Job's required fields verified against jobcannon/engine/models.py:21-26
    # (source and source_url have NO defaults — they are required).
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
    # Same source/source_url/description/location as p1 and title is not a
    # persisted column, so nothing canonical or source-merge-worthy changed
    # (hand-traced: no new source, no longer description, no date/salary/
    # location delta) — this is a straight re-sighting, not a merge.
    assert r.kind == "unchanged"  # merged into the SAME row, NOT a second insert
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
    # Canonicalized form of the helper's default URL — host must survive intact.
    assert urlsplit(entry["source_url"]).hostname == "jobs.ashbyhq.com"
    assert entry["first_seen"] <= entry["last_seen"]


def test_unresolved_reasons_persist_only_on_canonical_change(db_conn, company_id):
    from jobcannon.db._jobs import upsert_job
    from jobcannon.engine.parsed_job import ParsedJob

    conn = _svc_conn(db_conn)
    dedup_key = "acme|f1 unresolved reasons role"

    # Direct construction is sanctioned for unit tests (ParsedJob docstring):
    # bypasses from_job()'s validators so unresolved_reasons can be set
    # directly regardless of what would organically trigger them.
    p1 = ParsedJob(
        title="F1 Unresolved Reasons Role",
        company="Acme",
        dedup_key=dedup_key,
        description="Short description.",
        unresolved_reasons=["salary_implausible"],
    )
    r1 = upsert_job(conn, p1, company_id=company_id)
    assert r1.kind == "inserted"
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["salary_implausible"]

    # Canonical change (strictly longer description) + different reasons —
    # the new reasons MUST be persisted.
    p2 = ParsedJob(
        title="F1 Unresolved Reasons Role",
        company="Acme",
        dedup_key=dedup_key,
        description="Short description, now much longer than the original one.",
        unresolved_reasons=["title_invalid_shape"],
    )
    r2 = upsert_job(conn, p2, company_id=company_id)
    assert r2.kind == "updated"
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["title_invalid_shape"]

    # No canonical change (identical content) + yet-different reasons — the
    # stored column MUST NOT be clobbered by a touch/no-op re-ingest.
    p3 = ParsedJob(
        title="F1 Unresolved Reasons Role",
        company="Acme",
        dedup_key=dedup_key,
        description="Short description, now much longer than the original one.",
        unresolved_reasons=["jd_full_junk"],
    )
    r3 = upsert_job(conn, p3, company_id=company_id)
    assert r3.kind == "unchanged"
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["title_invalid_shape"]  # unchanged from p2


def test_salary_observations_dedup_and_capped(db_conn, company_id):
    from jobcannon.db._jobs import upsert_job

    conn = _svc_conn(db_conn)

    def _with_obs(observations):
        p = _parsed(title="Salary Observations Role")
        p.salary_observations = observations
        return p

    obs = {
        "provenance": "ats_structured",
        "raw_text": "$100k-$120k",
        "min_value": 100000,
        "max_value": 120000,
    }
    upsert_job(conn, _with_obs([obs]), company_id=company_id)
    # Re-sighting the identical observation must NOT grow the array.
    upsert_job(conn, _with_obs([dict(obs)]), company_id=company_id)

    row = db_conn.execute("SELECT dedup_key, salary_observations FROM postings").fetchone()
    dedup_key = row["dedup_key"]
    assert len(row["salary_observations"]) == 1

    # A run of >20 DISTINCT observations must cap the stored array at 20.
    for i in range(25):
        distinct_obs = {
            "provenance": "ats_structured",
            "raw_text": f"obs-{i}",
            "min_value": i,
            "max_value": i + 1,
        }
        upsert_job(conn, _with_obs([distinct_obs]), company_id=company_id)

    row = db_conn.execute(
        "SELECT salary_observations FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    assert len(row["salary_observations"]) == 20


def test_write_failure_leaves_connection_usable(db_conn, company_id):
    import psycopg

    from jobcannon.db._jobs import upsert_job

    conn = _svc_conn(db_conn)
    parsed = _parsed(title="F3 Savepoint Role")

    # postings.company_id is NOT NULL — a NEW posting with company_id=None
    # must raise, not silently corrupt the row.
    with pytest.raises(psycopg.errors.NotNullViolation):
        upsert_job(conn, parsed, company_id=None)

    # The connection must still be usable afterwards — not left in
    # Postgres's aborted-transaction state (the regression this fix exists
    # for: one bad write used to kill every later job in the scan loop).
    row = db_conn.execute("SELECT 1 AS ok").fetchone()
    assert row["ok"] == 1

    r = upsert_job(conn, parsed, company_id=company_id)
    assert r.kind == "inserted"

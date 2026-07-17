import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres

GOOD_JD = (
    "We are hiring a Staff Data Engineer to build our analytics platform. "
    "Responsibilities include designing pipelines, mentoring engineers, and "
    "partnering with product teams on experimentation infrastructure. "
    "Qualifications: 8+ years of data engineering, strong SQL and Python, "
    "experience with batch and streaming systems at scale."
)


@pytest.fixture()
def posting(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('jd-co')")
    cid = db_conn.execute("SELECT id FROM companies WHERE name='jd-co'").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES ('jd-co|staff data engineer', %s, 'Staff Data Engineer', 'jd-co')",
        (cid,),
    )
    return "jd-co|staff data engineer"


def _svc_conn(db_conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(db_conn)


def test_writes_good_jd(db_conn, posting):
    from jobcannon.db._jd_full import set_jd_full

    assert set_jd_full(_svc_conn(db_conn), posting, GOOD_JD, source="test") is True
    row = db_conn.execute(
        "SELECT jd_full FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["jd_full"] == GOOD_JD


def test_rejects_short_junk(db_conn, posting):
    from jobcannon.db._jd_full import set_jd_full

    assert set_jd_full(_svc_conn(db_conn), posting, "Sign in to view", source="test") is False
    row = db_conn.execute(
        "SELECT jd_full FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["jd_full"] is None


def test_rejects_empty(db_conn, posting):
    from jobcannon.db._jd_full import set_jd_full

    assert set_jd_full(_svc_conn(db_conn), posting, "", source="test") is False
    assert set_jd_full(_svc_conn(db_conn), posting, None, source="test") is False

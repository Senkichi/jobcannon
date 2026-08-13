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

# >=200 chars (clears the I-13 density gate) but shares ZERO content stems
# with "Staff Data Engineer" — trips the I-17 title-zero-overlap signal
# (jd_content_reject's title cross-field reject) instead of I-13. Verified
# via a REPL check: _is_jd_junk(...) is False, jd_content_reject(..., "Staff
# Data Engineer") returns ("jd_full_offsite", "title_zero_overlap").
TITLE_ZERO_OVERLAP_JD = (
    "We regret to inform you that this job posting is no longer available. "
    "The hiring team has closed this requisition after filling the role "
    "with a strong internal candidate last month. Please check back for "
    "future openings that may match your interests and background, and "
    "thank you for your continued interest in joining our growing team "
    "here at the company over the next several quarters ahead."
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


def test_set_jd_full_signature_accepts_config():
    """Signature pin: the private chokepoint takes a keyword-only
    `config: dict | None = None`; the port must match so the gate call can
    thread it without another signature change."""
    import inspect

    from jobcannon.db._jd_full import set_jd_full

    params = inspect.signature(set_jd_full).parameters
    assert "config" in params
    assert params["config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["config"].default is None


def test_config_governs_content_gate(db_conn, posting):
    """The keyword-only `config` is threaded into jd_content_reject, not
    decorative: a config demanding an absurd min-chars floor rejects a body
    that stores fine under defaults. (The permissive direction cannot be
    probed here — the I-13 junk gate's own hardcoded floor runs first.)"""
    from jobcannon.db._jd_full import set_jd_full

    strict = {"enrichment": {"jd_full": {"min_chars": 10_000}}}
    assert set_jd_full(_svc_conn(db_conn), posting, GOOD_JD, source="test", config=strict) is False
    row = db_conn.execute(
        "SELECT jd_full FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["jd_full"] is None
    # Same body, default thresholds: stored.
    assert set_jd_full(_svc_conn(db_conn), posting, GOOD_JD, source="test") is True


def test_rejects_title_zero_overlap_content(db_conn, posting):
    """I-17 content-contract rejection (jd_content_reject), distinct from
    the I-13 short-junk gate covered by test_rejects_short_junk above."""
    from jobcannon.db._jd_full import set_jd_full

    result = set_jd_full(
        _svc_conn(db_conn),
        posting,
        TITLE_ZERO_OVERLAP_JD,
        source="test",
        title="Staff Data Engineer",
    )
    assert result is False
    row = db_conn.execute(
        "SELECT jd_full FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["jd_full"] is None

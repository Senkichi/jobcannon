import pytest
from psycopg.types.json import Jsonb

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres

# Trailing-ellipsis snippet: clears the I-13 density gate (>=200 chars, no
# junk prefix) but trips jd_content_reject's truncation signal. Verified via
# a REPL check: jd_content_reject(snippet, None, None) ==
# ("jd_full_truncated", "trailing_ellipsis").
TRUNCATED_JD = "A" * 227 + "..."

# Clears the I-13 gate and doesn't start with a junk prefix, but trips the
# I-17 head-block/wiki signal. Verified via REPL:
# jd_content_reject(WIKI_JD, None, None) == ("jd_full_offsite", "head_block_or_wiki").
WIKI_JD = "From Wikipedia, the free encyclopedia. City in California. " * 8

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

# >=600 chars (clears _CLEAN_MIN_CHARS), JD-shaped, grounded in both the
# posting's title AND company -- classify_jd_content(CLEAN_JD, "Staff Data
# Engineer", "jd-co", None) verifies to JdVerdict.CLEAN / "shape+grounded"
# (verified via a REPL check, #152).
CLEAN_JD = (
    "We are hiring a Staff Data Engineer at jd-co to build our analytics platform. "
    "Responsibilities include designing pipelines, mentoring engineers, and "
    "partnering with product teams on experimentation infrastructure. This is a "
    "senior role reporting to the Director of Data Engineering, working closely "
    "with product and analytics stakeholders across the organization. "
    "Qualifications: 8+ years of data engineering experience, strong SQL and "
    "Python skills, and hands-on experience with batch and streaming systems "
    "at scale. You will own the roadmap for our core data platform, working "
    "with distributed systems, Kafka, Spark, and cloud data warehouses. "
    "What you will bring: a track record of shipping reliable, well-tested "
    "systems in production and mentoring more junior engineers on your team. "
    "In this role you will collaborate cross-functionally with product, "
    "design, and go-to-market teams to unlock new data-driven capabilities "
    "for jd-co customers worldwide."
)
# A second, still-CLEAN body (verified the same way) used to exercise the
# content-changed invalidation path without also flipping the verdict.
CLEAN_JD_V2 = CLEAN_JD + " Additional detail: this role also owns our streaming ingestion SLAs."


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


def test_content_reject_truncated_appends_reason(db_conn, posting):
    """A trailing-ellipsis snippet is rejected and the row is flagged for review."""
    from jobcannon.db._jd_full import set_jd_full

    assert set_jd_full(_svc_conn(db_conn), posting, TRUNCATED_JD, source="test") is False
    row = db_conn.execute(
        "SELECT jd_full, unresolved_reasons FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["jd_full"] is None
    assert "jd_full_truncated" in row["unresolved_reasons"]


def test_content_reject_offsite_appends_reason(db_conn, posting):
    """An offsite body rejected at the write gate is flagged in unresolved_reasons."""
    from jobcannon.db._jd_full import set_jd_full

    assert set_jd_full(_svc_conn(db_conn), posting, WIKI_JD, source="test") is False
    row = db_conn.execute(
        "SELECT jd_full, unresolved_reasons FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["jd_full"] is None
    assert "jd_full_offsite" in row["unresolved_reasons"]


def test_content_reject_clears_jd_content_reason_on_success(db_conn, posting):
    """A successful write heals a prior jd_full_truncated quarantine flag."""
    from jobcannon.db._jd_full import set_jd_full

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = %s WHERE dedup_key = %s",
        (Jsonb(["jd_full_truncated"]), posting),
    )
    assert set_jd_full(_svc_conn(db_conn), posting, GOOD_JD, source="test") is True
    row = db_conn.execute(
        "SELECT jd_full, unresolved_reasons FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["jd_full"] is not None
    assert row["unresolved_reasons"] == []


def test_content_reject_keeps_non_jd_content_reasons_on_success(db_conn, posting):
    """A successful write clears only I-18 reason codes, preserving others."""
    from jobcannon.db._jd_full import set_jd_full

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = %s WHERE dedup_key = %s",
        (Jsonb(["jd_full_truncated", "location_missing"]), posting),
    )
    assert set_jd_full(_svc_conn(db_conn), posting, GOOD_JD, source="test") is True
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["location_missing"]


# --- D5 jd-content verdict stamping (#152, m0009) ---------------------------


def test_stamps_clean_verdict_and_signal_on_write(db_conn, posting):
    """A successful write of a CLEAN-shaped body stamps jd_content_verdict /
    jd_content_signal at the write chokepoint, leaving jd_adjudicated_version
    NULL (nothing has adjudicated this row)."""
    from jobcannon.db._jd_full import set_jd_full

    assert (
        set_jd_full(
            _svc_conn(db_conn), posting, CLEAN_JD, source="test", title="Staff Data Engineer"
        )
        is True
    )
    row = db_conn.execute(
        "SELECT jd_content_verdict, jd_content_signal, jd_adjudicated_version "
        "FROM postings WHERE dedup_key = %s",
        (posting,),
    ).fetchone()
    assert row["jd_content_verdict"] == "clean"
    assert row["jd_content_signal"] == "shape+grounded"
    assert row["jd_adjudicated_version"] is None


def test_stamps_ambiguous_verdict_for_needs_adjudication_body(db_conn, posting):
    """A body that clears the write gate (jd_content_reject) but is too
    short to clear the CLEAN bar is stamped AMBIGUOUS, not left NULL -- the
    D5 gate must see a verdict to defer this row for adjudication."""
    from jobcannon.db._jd_full import set_jd_full

    assert set_jd_full(_svc_conn(db_conn), posting, GOOD_JD, source="test") is True
    row = db_conn.execute(
        "SELECT jd_content_verdict, jd_content_signal FROM postings WHERE dedup_key = %s",
        (posting,),
    ).fetchone()
    assert row["jd_content_verdict"] == "ambiguous"
    assert row["jd_content_signal"] == "needs_adjudication"


def test_rewrite_with_identical_content_preserves_adjudicated_version(db_conn, posting):
    """CAS / no-overwrite (#152): re-storing the SAME body (content
    unchanged, e.g. an idempotent re-fetch) must not clobber a
    jd_adjudicated_version the adjudicator already stamped -- only a
    genuine content change may null it."""
    from jobcannon.db._jd_full import set_jd_full

    assert (
        set_jd_full(
            _svc_conn(db_conn), posting, CLEAN_JD, source="test", title="Staff Data Engineer"
        )
        is True
    )
    db_conn.execute(
        "UPDATE postings SET jd_adjudicated_version = 8 WHERE dedup_key = %s", (posting,)
    )

    assert (
        set_jd_full(
            _svc_conn(db_conn), posting, CLEAN_JD, source="test", title="Staff Data Engineer"
        )
        is True
    )
    row = db_conn.execute(
        "SELECT jd_content_verdict, jd_adjudicated_version FROM postings WHERE dedup_key = %s",
        (posting,),
    ).fetchone()
    assert row["jd_content_verdict"] == "clean"
    assert row["jd_adjudicated_version"] == 8


def test_content_change_nulls_adjudicated_version_and_restamps_verdict(db_conn, posting):
    """A materially different body must re-arm the D5 gate: a stale
    adjudication no longer vouches for text it never saw, so
    jd_adjudicated_version is nulled in the same write that re-stamps the
    verdict against the new text."""
    from jobcannon.db._jd_full import set_jd_full

    assert (
        set_jd_full(
            _svc_conn(db_conn), posting, CLEAN_JD, source="test", title="Staff Data Engineer"
        )
        is True
    )
    db_conn.execute(
        "UPDATE postings SET jd_adjudicated_version = 8 WHERE dedup_key = %s", (posting,)
    )

    assert (
        set_jd_full(
            _svc_conn(db_conn), posting, CLEAN_JD_V2, source="test", title="Staff Data Engineer"
        )
        is True
    )
    row = db_conn.execute(
        "SELECT jd_full, jd_content_verdict, jd_adjudicated_version FROM postings "
        "WHERE dedup_key = %s",
        (posting,),
    ).fetchone()
    assert row["jd_full"] == CLEAN_JD_V2
    assert row["jd_content_verdict"] == "clean"
    assert row["jd_adjudicated_version"] is None


def test_persisted_reject_verdict_gates_scoring_precheck_until_adjudicated(db_conn, posting):
    """Integration (#152): a full `SELECT * FROM postings` row -- the exact
    shape a host passes to scoring_precheck, since this repo has no
    JOBS_ALL_COLUMNS-style explicit projection -- carries the three D5
    columns and the gate reads them correctly end to end.

    set_jd_full itself can never persist a REJECT verdict: jd_content_reject
    (the write gate) already blocks storage of anything classify_jd_content
    would REJECT, since both run the identical deterministic check on the
    identical (text, title, config). This simulates the state a re-sweep /
    adjudicator write path would leave on the row instead -- a REJECT
    verdict with no adjudication on record -- directly via UPDATE."""
    from jobcannon.engine.jd_content_contract import JD_CONTENT_VERSION
    from jobcannon.engine.job_scorer import scoring_precheck

    db_conn.execute(
        "UPDATE postings SET jd_full = %s, location = 'Remote', "
        "jd_content_verdict = 'reject', jd_content_signal = 'head_block_or_wiki', "
        "jd_adjudicated_version = NULL WHERE dedup_key = %s",
        (GOOD_JD, posting),
    )
    row = db_conn.execute("SELECT * FROM postings WHERE dedup_key = %s", (posting,)).fetchone()
    assert scoring_precheck(row) == "awaiting_jd_adjudication"

    db_conn.execute(
        "UPDATE postings SET jd_adjudicated_version = %s WHERE dedup_key = %s",
        (JD_CONTENT_VERSION, posting),
    )
    row = db_conn.execute("SELECT * FROM postings WHERE dedup_key = %s", (posting,)).fetchone()
    assert scoring_precheck(row) is None

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


def test_missing_posting_row_returns_false(db_conn):
    """dedup_key with no postings row is a no-op: both UPDATEs would affect
    0 rows silently (Postgres raises nothing), so without this guard the
    function returned a false 'wrote' signal and ran classify_jd_content for
    nothing. Mirrors _record_jd_content_reject's existing `is None` guard."""
    from jobcannon.db._jd_full import set_jd_full

    assert set_jd_full(_svc_conn(db_conn), "no-such-dedup-key", GOOD_JD, source="test") is False


def test_unchanged_content_with_no_verdict_stamps_without_nulling_adjudicated_version(
    db_conn, posting
):
    """Self-heal branch: content unchanged but no verdict on record (e.g. a
    legacy pre-m0009 row, or one whose verdict columns were wiped, re-touched
    with the identical body). Stamps a fresh verdict but must NOT null
    jd_adjudicated_version -- content_changed is False on this branch, so
    the invalidation condition (a genuine content change) never fires."""
    from jobcannon.db._jd_full import set_jd_full

    assert (
        set_jd_full(
            _svc_conn(db_conn), posting, CLEAN_JD, source="test", title="Staff Data Engineer"
        )
        is True
    )
    db_conn.execute(
        "UPDATE postings SET jd_content_verdict = NULL, jd_content_signal = NULL, "
        "jd_adjudicated_version = 8 WHERE dedup_key = %s",
        (posting,),
    )

    assert (
        set_jd_full(
            _svc_conn(db_conn), posting, CLEAN_JD, source="test", title="Staff Data Engineer"
        )
        is True
    )
    row = db_conn.execute(
        "SELECT jd_full, jd_content_verdict, jd_content_signal, jd_adjudicated_version "
        "FROM postings WHERE dedup_key = %s",
        (posting,),
    ).fetchone()
    assert row["jd_full"] == CLEAN_JD
    assert row["jd_content_verdict"] == "clean"
    assert row["jd_content_signal"] == "shape+grounded"
    assert row["jd_adjudicated_version"] == 8


def test_concurrent_delete_between_select_and_update_returns_false(db_conn, posting, monkeypatch):
    """L1 (refuter-1, PR #214): the SELECT at the top of set_jd_full can be
    followed by a concurrent DELETE before the UPDATE runs. Without a
    rowcount check, the UPDATE would affect 0 rows while the function still
    returned True -- a false "wrote" signal. Uses the same monkeypatch
    injection point as the interleaved-write test below (classify_jd_content
    runs after the SELECT and before the UPDATE) to land a same-connection
    DELETE in that window."""
    from jobcannon.db import _jd_full as jd_full_mod
    from jobcannon.db._jd_full import set_jd_full

    real_classify = jd_full_mod.classify_jd_content

    def _delete_then_classify(text, title, company, config):
        db_conn.execute("DELETE FROM postings WHERE dedup_key = %s", (posting,))
        return real_classify(text, title, company, config)

    monkeypatch.setattr(jd_full_mod, "classify_jd_content", _delete_then_classify)

    assert (
        set_jd_full(
            _svc_conn(db_conn), posting, CLEAN_JD, source="test", title="Staff Data Engineer"
        )
        is False
    )
    row = db_conn.execute("SELECT 1 FROM postings WHERE dedup_key = %s", (posting,)).fetchone()
    assert row is None


def test_interleaved_write_during_classify_still_lands_self_consistent(
    db_conn, posting, monkeypatch
):
    """#184 (was the CAS-guard test for #152's now-removed two-statement
    design): jd_full and its verdict/adjudicated_version are set together by
    ONE UPDATE (see the module docstring), so the mismatch the old CAS guard
    protected against -- a verdict landing next to text it doesn't
    describe -- is no longer representable, and there is nothing left for a
    WHERE-clause CAS to guard. This exercises the same interleaving the old
    test did (classify_jd_content is still called before the write, so a
    same-connection statement run inside it still lands before our own
    UPDATE) and asserts the PAIR stays consistent regardless of which write
    ends up as the row's current state: our own UPDATE runs last on this
    connection, so it wins outright, and jd_full/jd_content_verdict describe
    the exact same (our own) text together. The real cross-connection
    invariant -- what a truly concurrent writer's reader observes -- is
    proven by test_race_two_connections_never_observe_torn_jd_full_and_verdict
    below."""
    from jobcannon.db import _jd_full as jd_full_mod
    from jobcannon.db._jd_full import set_jd_full

    real_classify = jd_full_mod.classify_jd_content
    interleaved_body = "z" * 250

    def _interleaving_classify(text, title, company, config):
        db_conn.execute(
            "UPDATE postings SET jd_full = %s WHERE dedup_key = %s",
            (interleaved_body, posting),
        )
        return real_classify(text, title, company, config)

    monkeypatch.setattr(jd_full_mod, "classify_jd_content", _interleaving_classify)

    assert (
        set_jd_full(
            _svc_conn(db_conn), posting, CLEAN_JD, source="test", title="Staff Data Engineer"
        )
        is True
    )
    row = db_conn.execute(
        "SELECT jd_full, jd_content_verdict FROM postings WHERE dedup_key = %s",
        (posting,),
    ).fetchone()
    # Our own UPDATE is the last statement on this connection, so it wins --
    # and jd_full/jd_content_verdict describe the SAME text together,
    # structurally, because one statement set both.
    assert row["jd_full"] == CLEAN_JD
    assert row["jd_content_verdict"] == "clean"


def test_race_two_connections_never_observe_torn_jd_full_and_verdict(postgres_test_dsn):
    """Two-connection proof for #184: a reader on connection B polling
    during connection A's write must never observe NEW jd_full paired with
    a STALE verdict describing the OLD text. Uses a real committed seed row
    plus two independent (non-rollback) connections -- the rollback-isolated
    `db_conn` fixture would make this vacuous, since nothing A writes
    through it is ever visible to another connection.

    classify_jd_content is the injection point: on the pre-fix two-statement
    code it runs strictly BETWEEN the jd_full UPDATE's commit and the
    verdict UPDATE, so pausing inside it parks connection A right in the
    torn window -- B polls a committed (NEW jd_full, OLD verdict) pair the
    whole time it's paused. On the fixed single-UPDATE code, classify runs
    BEFORE any write, so pausing there leaves the OLD row (jd_full, verdict)
    fully intact and committed until A resumes and the ONE UPDATE lands, at
    which point B's next poll sees the NEW pair whole -- never a mix.
    """
    import threading
    import time
    import unittest.mock

    import psycopg
    from psycopg.rows import dict_row

    from jobcannon.db import _jd_full as jd_full_mod
    from jobcannon.db._jd_full import set_jd_full
    from jobcannon.db.pool import EngineCompatConnection

    dedup_key = "race-co|staff data engineer"
    conn_a = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    conn_b = psycopg.connect(postgres_test_dsn, row_factory=dict_row, autocommit=True)
    stop = threading.Event()
    release = threading.Event()
    poller: threading.Thread | None = None
    writer: threading.Thread | None = None
    try:
        cid = conn_a.execute(
            "INSERT INTO companies (name) VALUES ('race-co') RETURNING id"
        ).fetchone()["id"]
        conn_a.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, "
            "jd_content_verdict, jd_content_signal) VALUES (%s, %s, "
            "'Staff Data Engineer', 'race-co', %s, 'clean', 'shape+grounded')",
            (dedup_key, cid, CLEAN_JD),
        )
        conn_a.commit()

        real_classify = jd_full_mod.classify_jd_content
        entered = threading.Event()

        def _paused_classify(text, title, company, config):
            entered.set()
            release.wait(timeout=10)
            return real_classify(text, title, company, config)

        pre = (CLEAN_JD, "clean")
        post = (GOOD_JD, "ambiguous")
        samples: list[tuple] = []
        observed_post = threading.Event()

        def _poll():
            while not stop.is_set():
                row = conn_b.execute(
                    "SELECT jd_full, jd_content_verdict FROM postings WHERE dedup_key = %s",
                    (dedup_key,),
                ).fetchone()
                sample = (row["jd_full"], row["jd_content_verdict"])
                samples.append(sample)
                if sample == post:
                    observed_post.set()
                time.sleep(0.01)

        poller = threading.Thread(target=_poll, daemon=True)
        poller.start()

        writer_result: dict = {}

        def _write():
            writer_result["ok"] = set_jd_full(
                EngineCompatConnection(conn_a), dedup_key, GOOD_JD, source="test"
            )

        with unittest.mock.patch.object(jd_full_mod, "classify_jd_content", _paused_classify):
            writer = threading.Thread(target=_write)
            writer.start()
            assert entered.wait(timeout=10), "classify_jd_content was never entered"
            time.sleep(0.3)  # let the poller sample repeatedly during the pause
            release.set()
            writer.join(timeout=10)
            assert not writer.is_alive(), "writer thread did not finish within timeout"

        # Bounded poll-until instead of a fixed sleep (refuter-2/refuter-3
        # LOW): wait for the poller thread itself to observe the post-write
        # committed state, signaled via observed_post, rather than assuming
        # a fixed sleep duration is enough on any given box. Still bounded
        # (5s timeout) so a genuinely broken poller/write fails fast instead
        # of hanging; the positive-control assertion below still catches a
        # timeout (observed_post never set -> post not in samples).
        observed_post.wait(timeout=5)
        stop.set()
        poller.join(timeout=10)
        assert not poller.is_alive(), "poller thread did not finish within timeout"

        assert writer_result.get("ok") is True

        bad = [s for s in samples if s not in (pre, post)]
        assert not bad, f"observed a torn (jd_full, jd_content_verdict) pair: {bad[:5]}"
        # Positive control: without this, an empty/no-op poll would pass
        # vacuously against ANY code, fixed or not.
        assert post in samples, f"poller never observed the post-write state; samples={samples[:5]}"
    finally:
        # Release any paused/looping worker threads BEFORE touching conn_a/conn_b
        # from this thread -- psycopg connections aren't safe for concurrent use,
        # and a thread still parked in release.wait()/still polling would race
        # the cleanup below. Only after both threads are confirmed joined do we
        # touch the connections they were using.
        release.set()
        stop.set()
        if writer is not None:
            writer.join(timeout=10)
        if poller is not None:
            poller.join(timeout=10)
        # Each cleanup step below is independent of the others (refuter-3
        # LOW): a raise from conn_a.rollback()/DELETE/commit must not skip
        # conn_a.close(), and neither of those must skip conn_b.close() --
        # both connections always get released even if the row cleanup
        # itself fails partway through.
        try:
            try:
                conn_a.rollback()
                conn_a.execute("DELETE FROM postings WHERE dedup_key = %s", (dedup_key,))
                conn_a.execute("DELETE FROM companies WHERE name = 'race-co'")
                conn_a.commit()
            finally:
                conn_a.close()
        finally:
            conn_b.close()


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

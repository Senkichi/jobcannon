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


# --- #216 HTML-signal normalization: entity-encoded write-path integration --


def test_entity_encoded_only_signal_is_normalized_before_storage(db_conn, posting):
    """A body that signals HTML ONLY via entity-encoded tags (no literal
    `<...>` anywhere) must be stripped to plain text before storage, not
    left with the tags decoded-but-unstripped. Regression coverage for the
    call-site fix alongside the #216 regex widening: html_to_plain_text
    unescapes BEFORE stripping tags, whereas calling strip_html_to_text
    directly on entity-encoded input leaves literal `<p>...</p>` in the
    output (empirically confirmed while implementing this fix)."""
    from jobcannon.db._jd_full import set_jd_full

    entity_body = "&lt;p&gt;" + GOOD_JD + "&lt;/p&gt;"
    assert set_jd_full(_svc_conn(db_conn), posting, entity_body, source="test") is True
    row = db_conn.execute(
        "SELECT jd_full FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["jd_full"] is not None
    assert "<p>" not in row["jd_full"]
    assert "&lt;" not in row["jd_full"]
    assert "We are hiring a Staff Data Engineer" in row["jd_full"]


# --- #234 chokepoint fix: mixed HTML + entity-escaped comparison operator ---


def test_mixed_body_with_escaped_comparison_operator_stores_intact_prose(db_conn, posting):
    """#234 regression pin, via the REAL set_jd_full write path (unit
    coverage for the same fix lives in
    tests/engine/test_description_formatter.py). A body mixing real HTML
    tags with an entity-escaped comparison operator in the prose (the
    refuter's exact probe on PR #232) must store the full prose intact in
    jd_full -- pre-#234, html_to_plain_text's unescape-before-strip
    ordering decoded `&lt;` into a real `<` that fused with the later real
    `</p>` tag, and the greedy stripper swallowed everything in between."""
    from jobcannon.db._jd_full import set_jd_full

    mixed_body = (
        "<p>" + GOOD_JD + " Base salary &lt; $100k and role requires &gt; 5 years of "
        "experience building data platforms.</p>"
    )
    assert set_jd_full(_svc_conn(db_conn), posting, mixed_body, source="test") is True
    row = db_conn.execute(
        "SELECT jd_full, jd_content_verdict, jd_content_signal, jd_adjudicated_version "
        "FROM postings WHERE dedup_key = %s",
        (posting,),
    ).fetchone()
    assert row["jd_full"] is not None
    assert "<p>" not in row["jd_full"]
    assert "&lt;" not in row["jd_full"]
    assert "Base salary < $100k" in row["jd_full"]
    assert "5 years of experience building data platforms" in row["jd_full"]

    # Idempotent re-fetch of the IDENTICAL body must not change the row at
    # all -- same jd_full, same stamped verdict (the self-heal / no-op
    # branch in set_jd_full's UPDATE, unaffected by this fix).
    stored = dict(row)
    assert set_jd_full(_svc_conn(db_conn), posting, mixed_body, source="test") is True
    row2 = db_conn.execute(
        "SELECT jd_full, jd_content_verdict, jd_content_signal, jd_adjudicated_version "
        "FROM postings WHERE dedup_key = %s",
        (posting,),
    ).fetchone()
    assert dict(row2) == stored


def test_mixed_body_with_escaped_tag_like_token_stores_literal_text(db_conn, posting):
    """#234 regression pin for fix (a) specifically, via the REAL
    set_jd_full write path. The sibling test above
    (test_mixed_body_with_escaped_comparison_operator_stores_intact_prose)
    only exercises fix (b) -- reverting fix (a) alone (restoring
    `strip_html_to_text(_html.unescape(raw))` unconditionally in
    html_to_plain_text, while keeping fix (b)'s tightened stripper) still
    PASSES that test, because a bare `< $100k` never looks like a tag-open
    regardless of decode order. This test uses the other #234 shape instead:
    real HTML tags in the markup, PLUS an entity-escaped TAG-LIKE token
    (`&lt;div&gt;`, `&lt;table&gt;`) in the prose. Pre-fix-(a),
    html_to_plain_text unescapes before stripping, so `&lt;div&gt;` becomes
    a literal, validly-tag-shaped `<div>` that fix (b)'s tightened stripper
    then legitimately strips as if it were real markup -- losing the literal
    text the author wrote. Fix (a) prevents the early unescape by
    discriminating real-HTML input via _html_tag_re first, so the escaped
    tokens survive stripping as literal visible text."""
    from jobcannon.db._jd_full import set_jd_full

    mixed_body = (
        "<p>" + GOOD_JD + " Experience with &lt;div&gt; layouts and "
        "&lt;table&gt; markup is a plus.</p>"
    )
    assert set_jd_full(_svc_conn(db_conn), posting, mixed_body, source="test") is True
    row = db_conn.execute(
        "SELECT jd_full FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["jd_full"] is not None
    assert "<p>" not in row["jd_full"]
    assert "<div>" in row["jd_full"]
    assert "<table>" in row["jd_full"]
    assert "Experience with <div> layouts and <table> markup is a plus" in row["jd_full"]


# --- #217 unresolved_reasons: malformed/NULL-value tolerance ----------------


def test_success_write_tolerates_malformed_object_unresolved_reasons(db_conn, posting):
    """The SQL removal expression mirrors remove_reasons's malformed-value
    tolerance: a non-array jsonb value (e.g. a stray object) normalizes to
    an empty array via the jsonb_typeof guard rather than erroring the
    UPDATE."""
    from jobcannon.db._jd_full import set_jd_full

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = %s WHERE dedup_key = %s",
        (Jsonb({"not": "a list"}), posting),
    )
    assert set_jd_full(_svc_conn(db_conn), posting, GOOD_JD, source="test") is True
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["unresolved_reasons"] == []


def test_content_reject_tolerates_malformed_object_unresolved_reasons(db_conn, posting):
    """Same malformed-value tolerance, exercised on _record_jd_content_reject's
    append expression via the reject path."""
    from jobcannon.db._jd_full import set_jd_full

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = %s WHERE dedup_key = %s",
        (Jsonb({"not": "a list"}), posting),
    )
    assert set_jd_full(_svc_conn(db_conn), posting, TRUNCATED_JD, source="test") is False
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["jd_full_truncated"]


def test_success_write_tolerates_json_null_unresolved_reasons(db_conn, posting):
    """A jsonb `null` scalar (distinct from SQL NULL -- the column is
    NOT NULL DEFAULT '[]') is the real-world analog of the Python helpers'
    `None` input case and must be tolerated the same way (falls back to
    treating the row as if it had no prior reasons)."""
    from jobcannon.db._jd_full import set_jd_full

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = 'null'::jsonb WHERE dedup_key = %s",
        (posting,),
    )
    assert set_jd_full(_svc_conn(db_conn), posting, GOOD_JD, source="test") is True
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["unresolved_reasons"] == []


def test_content_reject_tolerates_json_null_unresolved_reasons(db_conn, posting):
    from jobcannon.db._jd_full import set_jd_full

    db_conn.execute(
        "UPDATE postings SET unresolved_reasons = 'null'::jsonb WHERE dedup_key = %s",
        (posting,),
    )
    assert set_jd_full(_svc_conn(db_conn), posting, TRUNCATED_JD, source="test") is False
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["jd_full_truncated"]


def test_content_reject_dedupes_repeated_reason(db_conn, posting):
    """append_reason's dedupe contract, mirrored in SQL: rejecting the same
    body shape twice in a row must not duplicate the reason code."""
    from jobcannon.db._jd_full import set_jd_full

    assert set_jd_full(_svc_conn(db_conn), posting, TRUNCATED_JD, source="test") is False
    assert set_jd_full(_svc_conn(db_conn), posting, TRUNCATED_JD, source="test") is False
    row = db_conn.execute(
        "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (posting,)
    ).fetchone()
    assert row["unresolved_reasons"] == ["jd_full_truncated"]


# --- #217 lost-update proof (two independent, non-rollback connections) -----


def test_set_jd_full_lost_update_closed_competing_addition_survives(postgres_test_dsn):
    """DETERMINISTIC lost-update proof for set_jd_full's unresolved_reasons
    removal (#217). Connection A calls set_jd_full with a body that
    succeeds and clears the stale `jd_full_truncated` I-18 code. While A is
    paused inside classify_jd_content (the same injection point
    test_interleaved_write_during_classify_still_lands_self_consistent and
    test_race_two_connections_never_observe_torn_jd_full_and_verdict use --
    it runs strictly between A's initial SELECT and its final UPDATE),
    connection B commits an UNRELATED addition (`location_missing`, not a
    JD_CONTENT_REASON_CODE, so A's removal must never touch it) directly to
    the same row's unresolved_reasons.

    A's own SELECT read the row BEFORE B's commit (value: only
    `jd_full_truncated`), so a Python value computed from that SELECT and
    written back later as a literal (the pre-#217 shape) would silently
    overwrite B's already-committed change with a value that never saw it --
    a genuine lost update. The fixed code decides the SET-clause value via a
    SQL expression evaluated against the row's LIVE value at
    UPDATE-execution time, so it must reflect B's addition even though A
    never re-read the row in Python.

    Sabotage-verified (see this workstream's PR body / IMPLEMENTATION.md):
    temporarily reverting set_jd_full's unresolved_reasons SET-clause to the
    pre-#217 Python remove_reasons(existing_value, codes) + literal-value
    UPDATE shape makes this test fail with
    ``assert row["unresolved_reasons"] == ["location_missing"]`` observing
    ``[]`` instead -- B's addition is silently lost.
    """
    import threading
    import unittest.mock

    import psycopg
    from psycopg.rows import dict_row

    from jobcannon.db import _jd_full as jd_full_mod
    from jobcannon.db._jd_full import set_jd_full
    from jobcannon.db.pool import EngineCompatConnection

    dedup_key = "lostupdate-co|staff data engineer"
    conn_a = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    conn_b = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    entered = threading.Event()
    release = threading.Event()
    writer: threading.Thread | None = None
    try:
        cid = conn_a.execute(
            "INSERT INTO companies (name) VALUES ('lostupdate-co') RETURNING id"
        ).fetchone()["id"]
        conn_a.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company, "
            "unresolved_reasons) VALUES (%s, %s, 'Staff Data Engineer', "
            "'lostupdate-co', %s)",
            (dedup_key, cid, Jsonb(["jd_full_truncated"])),
        )
        conn_a.commit()

        real_classify = jd_full_mod.classify_jd_content

        def _paused_classify(text, title, company, config):
            entered.set()
            release.wait(timeout=10)
            return real_classify(text, title, company, config)

        writer_result: dict = {}

        def _write():
            writer_result["ok"] = set_jd_full(
                EngineCompatConnection(conn_a), dedup_key, GOOD_JD, source="test"
            )

        with unittest.mock.patch.object(jd_full_mod, "classify_jd_content", _paused_classify):
            writer = threading.Thread(target=_write)
            writer.start()
            assert entered.wait(timeout=10), "classify_jd_content was never entered"

            # Connection B commits its own, unrelated addition while A is
            # paused mid-function -- A's earlier SELECT cannot possibly have
            # seen this.
            conn_b.execute(
                "UPDATE postings SET unresolved_reasons = unresolved_reasons || "
                "'[\"location_missing\"]'::jsonb WHERE dedup_key = %s",
                (dedup_key,),
            )
            conn_b.commit()

            release.set()
            writer.join(timeout=10)
            assert not writer.is_alive(), "writer thread did not finish within timeout"

        assert writer_result.get("ok") is True

        row = conn_a.execute(
            "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
        ).fetchone()
        # Both writers' changes present: A's removal of the stale
        # jd_full_truncated code AND B's location_missing addition.
        assert row["unresolved_reasons"] == ["location_missing"]
    finally:
        release.set()
        if writer is not None:
            writer.join(timeout=10)
        try:
            try:
                conn_a.rollback()
                conn_a.execute("DELETE FROM postings WHERE dedup_key = %s", (dedup_key,))
                conn_a.execute("DELETE FROM companies WHERE name = 'lostupdate-co'")
                conn_a.commit()
            finally:
                conn_a.close()
        finally:
            conn_b.close()


def test_record_jd_content_reject_concurrent_appends_both_survive(postgres_test_dsn):
    """DETERMINISTIC lost-update proof for _record_jd_content_reject's
    append (#217). _record_jd_content_reject is now a single atomic UPDATE
    with no preceding SELECT, so there is no Python-visible gap left to
    monkeypatch a pause into -- the proof instead relies on real Postgres
    row-level locking: connection A's UPDATE is paused (via a patched
    `execute` on A's own connection) AFTER it has run but BEFORE its
    transaction commits, so A still holds the row lock. Connection B's
    concurrent call for a DIFFERENT reason genuinely BLOCKS on that lock
    (not merely races on timing) until A releases it, then B's SQL
    expression re-evaluates against A's now-committed row and correctly
    appends on top of it -- both reasons must survive.

    Sabotage-verified (see PR body / IMPLEMENTATION.md): temporarily
    reverting _record_jd_content_reject to the pre-#217 SELECT +
    append_reason(...) + literal-value UPDATE shape makes this test fail --
    the same monkeypatched-execute pause now lands after the (non-locking)
    SELECT instead of after the UPDATE, connection B's write commits freely
    and unblocked while A is paused, and A's later UPDATE overwrites B's
    committed change with a stale literal value. Failing assertion:
    ``assert set(row["unresolved_reasons"]) == {"jd_full_truncated",
    "jd_full_offsite"}`` observes only ``{"jd_full_truncated"}`` (A's own
    reason survives -- since A is the last writer -- but B's is lost).
    """
    import threading

    import psycopg
    from psycopg.rows import dict_row

    from jobcannon.db._jd_full import _record_jd_content_reject

    dedup_key = "concurrent-reject-co|staff data engineer"
    conn_a = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    conn_b = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    entered = threading.Event()
    release = threading.Event()
    b_committed = threading.Event()
    thread_a: threading.Thread | None = None
    thread_b: threading.Thread | None = None
    try:
        cid = conn_a.execute(
            "INSERT INTO companies (name) VALUES ('concurrent-reject-co') RETURNING id"
        ).fetchone()["id"]
        conn_a.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES (%s, %s, 'Staff Data Engineer', 'concurrent-reject-co')",
            (dedup_key, cid),
        )
        conn_a.commit()

        real_execute = conn_a.execute

        def _execute_then_pause(query, *args, **kwargs):
            cur = real_execute(query, *args, **kwargs)
            if "unresolved_reasons" in query:
                entered.set()
                release.wait(timeout=10)
            return cur

        conn_a.execute = _execute_then_pause

        results: dict = {}

        def _run_a():
            _record_jd_content_reject(conn_a, dedup_key, "jd_full_truncated")
            results["a_done"] = True

        def _run_b():
            _record_jd_content_reject(conn_b, dedup_key, "jd_full_offsite")
            results["b_done"] = True
            b_committed.set()

        thread_a = threading.Thread(target=_run_a)
        thread_a.start()
        assert entered.wait(timeout=10), "A's UPDATE was never entered"

        # B is started only once A holds the row lock (mid-transaction,
        # paused). B's UPDATE genuinely blocks on that lock until A
        # releases it below.
        thread_b = threading.Thread(target=_run_b)
        thread_b.start()

        # Deliberately NOT release.set() immediately: give B a bounded
        # window to land BEFORE A resumes. Against the #217 fix, A's paused
        # statement (the sole UPDATE) already holds the row lock, so B
        # genuinely blocks here and this wait times out -- that timeout is
        # the expected, harmless case, not a failure. Against the sabotaged
        # pre-#217 SELECT-then-UPDATE shape, A's pause lands after a
        # non-locking SELECT, so B is free to run unblocked; without this
        # wait, whether B's commit lands before or after A resumes is
        # scheduler-luck (verified empirically: the naive version of this
        # test below intermittently passed against sabotaged code purely
        # because B happened to finish after A), which would make the
        # lost-update proof non-deterministic. Waiting here for B's commit
        # (bounded to 1s, comfortably above a same-box Postgres round trip)
        # forces the interleaving that actually exercises the bug whenever
        # the code under test has no real lock protecting the gap.
        b_committed.wait(timeout=1.0)

        release.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
        assert not thread_a.is_alive(), "writer A did not finish within timeout"
        assert not thread_b.is_alive(), "writer B did not finish within timeout"
        assert results.get("a_done") is True
        assert results.get("b_done") is True

        row = conn_a.execute(
            "SELECT unresolved_reasons FROM postings WHERE dedup_key = %s", (dedup_key,)
        ).fetchone()
        assert set(row["unresolved_reasons"]) == {"jd_full_truncated", "jd_full_offsite"}
    finally:
        release.set()
        if thread_a is not None:
            thread_a.join(timeout=10)
        if thread_b is not None:
            thread_b.join(timeout=10)
        try:
            try:
                conn_a.rollback()
                conn_a.execute = real_execute if "real_execute" in dir() else conn_a.execute
            except Exception:
                pass
            try:
                conn_a.rollback()
                conn_a.execute("DELETE FROM postings WHERE dedup_key = %s", (dedup_key,))
                conn_a.execute("DELETE FROM companies WHERE name = 'concurrent-reject-co'")
                conn_a.commit()
            finally:
                conn_a.close()
        finally:
            conn_b.close()

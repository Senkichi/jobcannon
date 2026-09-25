"""PORTED (tests) from tests/test_jd_adjudicator.py @ 0cbf333a (private job-cannon).
Ledger L-0189. DB + host halves of the L-0189 three-way residence split:
jobcannon.db._jd_adjudication (stamp_adjudicated, select_adjudication_candidates)
and jobcannon.host.jd_adjudication_backfill (run_jd_adjudication_backfill).

# PORT-SEAM: the private suite patched the module-level
# `job_finder.web.jd_adjudicator.adjudicate_jd` name. This port's engine seam
# is call_model-injected, not adjudicate_jd itself -- adjudicate_jd stays a
# real (unpatched) function throughout, and these tests monkeypatch the name
# `jobcannon.host.jd_adjudication_backfill.adjudicate_jd` instead (the import
# binding the driver actually calls through), mirroring the private pattern
# at the new residence.

Dropped from the private suite (not ported), each for a stated reason:
  * test_backfill_does_not_crash_on_malformed_unresolved_reasons -- unreachable
    on this host: unresolved_reasons is `jsonb NOT NULL DEFAULT '[]'`, so the
    private "malformed JSON" branch has no analog (see
    jobcannon/db/_jd_adjudication.py's select_adjudication_candidates
    docstring).
  * test_backfill_concurrent_writer_succeeds,
    test_backfill_crash_mid_loop_persists_nothing,
    test_backfill_crash_between_writeback_items_leaves_prior_item_durable --
    all three pin SQLite lock semantics (PRAGMA busy_timeout,
    sqlite3.OperationalError "database is locked", threading.Event-driven
    write-lock ordering). Postgres MVCC has no analogous lock-contention
    phenomenon for these scenarios; the phenomenon under test does not exist
    on this host, so it is dropped rather than mistranslated.
  * test_run_jd_adjudication_loops_until_queue_empty,
    test_run_jd_adjudication_loop_breaks_on_no_progress,
    test_run_jd_adjudication_absent_runtime_limit_one_batch_only,
    test_run_jd_adjudication_soft_deadline_breaks_loop -- these test
    `run_jd_adjudication`, the scheduler drain-loop wrapper in the private
    repo's `job_finder.web.scheduler._runners`. That wrapper is out of scope
    for L-0189 (not part of the design addendum's three-way split) and is
    not ported.

Heal-leg update (issue #360): `heal_offsite` now lands as
`jobcannon.db._jd_full.clear_jd_full` + `_assessment_writer.invalidate_job_score`
composed by the driver under one ambient transaction (see that module's
docstring PORT-SEAM). The tests that were adapted to the peeled behavior in
L-0189 (`test_backfill_state_machine`'s AMBIGUOUS-NO assertions,
`test_backfill_reclassifies_row_with_unrelated_unresolved_reason`'s post-heal
assertions) are restored to assert the healed outcome -- the row is cleared
and quarantined, matching the private original's semantics at the new
residence split.
"""

from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from jobcannon.db.pool import EngineCompatConnection
from jobcannon.engine.jd_content_contract import JD_CONTENT_VERSION, JD_OFFSITE, JdVerdict
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres

# Bodies engineered for a deterministic verdict (ported verbatim from the
# private suite's fixtures) -- _CLEAN_JD names its employer ("Acme") so it
# stays CLEAN under the company-absent counter-signal; _AMBIGUOUS_JD is
# grounded + substantial but has no shape heading, so it lands AMBIGUOUS.
_CLEAN_JD = (
    "Acme is looking for a Senior Data Scientist. Responsibilities include building "
    "models and running experiments. Qualifications: Python, SQL, statistics. What "
    "you'll do: ship models to production and mentor analysts. " * 4
)
_AMBIGUOUS_JD = (
    "About Acme. Acme builds data platforms for the enterprise. Our data tooling "
    "is best in class and our platform scales globally. We value bold engineers. " * 5
)


def _svc_conn(db_conn):
    return EngineCompatConnection(db_conn)


@pytest.fixture()
def company(db_conn):
    return db_conn.execute(
        "INSERT INTO companies (name) VALUES ('Acme Corp') RETURNING id"
    ).fetchone()["id"]


def _insert(
    db_conn,
    company_id,
    dedup_key,
    *,
    title,
    jd,
    classification="apply",
    unresolved_reasons=None,
    first_seen=None,
    jd_content_verdict=None,
    scoring_model=None,
):
    """Insert a posting row shaped for these tests. `scoring_model` is only
    accepted alongside a non-NULL `classification` (the m0015
    `postings_scoring_model_requires_classification` CHECK) and exists for the
    heal tests, which assert the stale score is retracted."""
    cols = ["dedup_key", "company_id", "title", "company", "jd_full", "classification"]
    vals = [dedup_key, company_id, title, "Acme Corp", jd, classification]
    if unresolved_reasons is not None:
        cols.append("unresolved_reasons")
        vals.append(Jsonb(unresolved_reasons))
    if first_seen is not None:
        cols.append("first_seen")
        vals.append(first_seen)
    if jd_content_verdict is not None:
        cols.append("jd_content_verdict")
        vals.append(jd_content_verdict)
    if scoring_model is not None:
        cols.append("scoring_model")
        vals.append(scoring_model)
    placeholders = ", ".join(["%s"] * len(cols))
    db_conn.execute(
        f"INSERT INTO postings ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )


def _row(db_conn, dedup_key):
    return db_conn.execute(
        "SELECT jd_full, jd_adjudicated_version, unresolved_reasons, classification, "
        "scoring_model, jd_content_verdict FROM postings WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()


# ---------------------------------------------------------------------------
# stamp_adjudicated -- new, minimal (not a private port): the CAS-guard-miss
# coverage the dropped concurrent/crash tests provided is not otherwise
# exercised anywhere in this port, so this pins the guard directly rather
# than only indirectly through the backfill state machine below.
# ---------------------------------------------------------------------------


def test_stamp_adjudicated_matches_and_stamps(db_conn, company):
    from jobcannon.db._jd_adjudication import stamp_adjudicated

    _insert(db_conn, company, "acme|stamp-ok", title="Data Scientist", jd=_CLEAN_JD)
    assert stamp_adjudicated(_svc_conn(db_conn), "acme|stamp-ok", _CLEAN_JD) is True
    assert _row(db_conn, "acme|stamp-ok")["jd_adjudicated_version"] == JD_CONTENT_VERSION


def test_stamp_adjudicated_cas_guard_misses_on_stale_premise(db_conn, company):
    """A concurrent writer that rewrote jd_full after classification must make
    the stamp UPDATE's WHERE miss -- never vouch for content the classifier
    never saw."""
    from jobcannon.db._jd_adjudication import stamp_adjudicated

    _insert(db_conn, company, "acme|stamp-stale", title="Data Scientist", jd=_CLEAN_JD)
    db_conn.execute(
        "UPDATE postings SET jd_full = %s WHERE dedup_key = %s",
        ("a completely different body, rewritten concurrently", "acme|stamp-stale"),
    )
    assert stamp_adjudicated(_svc_conn(db_conn), "acme|stamp-stale", _CLEAN_JD) is False
    assert _row(db_conn, "acme|stamp-stale")["jd_adjudicated_version"] is None


# ---------------------------------------------------------------------------
# run_jd_adjudication_backfill -- the state machine
# ---------------------------------------------------------------------------


def test_backfill_state_machine(monkeypatch, db_conn, company):
    from jobcannon.host import jd_adjudication_backfill
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    _insert(db_conn, company, "acme|clean", title="Senior Data Scientist", jd=_CLEAN_JD)
    _insert(db_conn, company, "acme|yes", title="Data Platform Engineer YES", jd=_AMBIGUOUS_JD)
    _insert(db_conn, company, "acme|no", title="Data Platform Engineer NO", jd=_AMBIGUOUS_JD)
    _insert(db_conn, company, "acme|maybe", title="Data Platform Engineer MAYBE", jd=_AMBIGUOUS_JD)

    def fake_adjudicate(conn, title, company_name, jd_full, *, call_model, config):
        if "YES" in (title or ""):
            return True
        if "NO" in (title or ""):
            return False
        return None  # MAYBE -> undetermined

    monkeypatch.setattr(jd_adjudication_backfill, "adjudicate_jd", fake_adjudicate)

    summary = run_jd_adjudication_backfill(
        _svc_conn(db_conn), {}, call_model=lambda **k: None, limit=50
    )

    assert summary["scanned"] == 4
    assert summary["llm_calls"] == 3  # the 3 AMBIGUOUS rows (clean skipped the LLM)
    assert summary["kept"] == 2  # clean + yes
    assert summary["rejected"] == 1  # no
    assert summary["undetermined"] == 1  # maybe

    # CLEAN: stamped, body kept, still "scored" (classification unchanged).
    clean = _row(db_conn, "acme|clean")
    assert clean["jd_adjudicated_version"] == JD_CONTENT_VERSION
    assert clean["jd_full"] is not None
    assert clean["classification"] == "apply"

    # AMBIGUOUS-YES: stamped, kept.
    yes = _row(db_conn, "acme|yes")
    assert yes["jd_adjudicated_version"] == JD_CONTENT_VERSION
    assert yes["jd_full"] is not None

    # AMBIGUOUS-NO: healed (issue #360) -- jd_full cleared, the stale score
    # retracted (classification nulled), and the row quarantined with the
    # generic offsite reason (an LLM "no" carries no deterministic reason).
    no = _row(db_conn, "acme|no")
    assert no["jd_full"] is None
    assert no["jd_adjudicated_version"] is None
    assert no["jd_content_verdict"] is None
    assert no["classification"] is None
    assert no["unresolved_reasons"] == [JD_OFFSITE]

    # AMBIGUOUS-undetermined: left unstamped for retry, body intact.
    maybe = _row(db_conn, "acme|maybe")
    assert maybe["jd_adjudicated_version"] is None
    assert maybe["jd_full"] is not None


def test_backfill_skips_already_adjudicated(monkeypatch, db_conn, company):
    from jobcannon.host import jd_adjudication_backfill
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    _insert(db_conn, company, "acme|done", title="Data Platform Engineer YES", jd=_AMBIGUOUS_JD)

    calls = {"n": 0}

    def fake_adjudicate(conn, title, company_name, jd_full, *, call_model, config):
        calls["n"] += 1
        return True

    monkeypatch.setattr(jd_adjudication_backfill, "adjudicate_jd", fake_adjudicate)

    svc = _svc_conn(db_conn)
    run_jd_adjudication_backfill(svc, {}, call_model=lambda **k: None, limit=50)  # stamps
    assert calls["n"] == 1
    run_jd_adjudication_backfill(
        svc, {}, call_model=lambda **k: None, limit=50
    )  # already stamped -> not re-selected
    assert calls["n"] == 1


def test_backfill_reclassifies_row_with_unrelated_unresolved_reason(monkeypatch, db_conn, company):
    """A row already carrying an UNRELATED quarantine reason (e.g.
    "location_missing") must still be eligible for jd-content re-classification
    -- the eligibility filter checks for a JD_CONTENT_REASON_CODES member
    specifically, not "unresolved_reasons is non-empty"."""
    from jobcannon.host import jd_adjudication_backfill
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    _insert(
        db_conn,
        company,
        "acme|location-quarantined",
        title="Data Platform Engineer NO",
        jd=_AMBIGUOUS_JD,
        unresolved_reasons=["location_missing"],
    )

    monkeypatch.setattr(
        jd_adjudication_backfill,
        "adjudicate_jd",
        lambda conn, title, company_name, jd_full, *, call_model, config: False,
    )

    summary = run_jd_adjudication_backfill(
        _svc_conn(db_conn), {}, call_model=lambda **k: None, limit=50
    )

    # Was scanned + adjudicated at all (the pre-fix-equivalent filter would
    # have excluded it, leaving scanned=0 and the row never re-examined).
    assert summary["scanned"] == 1
    assert summary["llm_calls"] == 1
    assert summary["rejected"] == 1

    # Heal (issue #360) leaves the pre-existing UNRELATED reason untouched and
    # appends the jd-content quarantine code after it -- the append-dedupe
    # idiom only touches the reason it adds.
    row = db_conn.execute(
        "SELECT jd_full, unresolved_reasons FROM postings WHERE dedup_key = %s",
        ("acme|location-quarantined",),
    ).fetchone()
    assert row["unresolved_reasons"] == ["location_missing", JD_OFFSITE]
    assert row["jd_full"] is None


def test_backfill_still_skips_row_already_jd_content_quarantined(monkeypatch, db_conn, company):
    """A row already carrying a JD_CONTENT_REASON_CODES reason (e.g.
    jd_full_offsite from a prior heal) stays excluded -- the fix narrows the
    filter, it does not remove it."""
    from jobcannon.host import jd_adjudication_backfill
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    _insert(
        db_conn,
        company,
        "acme|already-offsite",
        title="Data Platform Engineer",
        jd=_AMBIGUOUS_JD,
        unresolved_reasons=[JD_OFFSITE],
    )

    calls = {"n": 0}

    def fake_adjudicate(conn, title, company_name, jd_full, *, call_model, config):
        calls["n"] += 1
        return True

    monkeypatch.setattr(jd_adjudication_backfill, "adjudicate_jd", fake_adjudicate)

    summary = run_jd_adjudication_backfill(
        _svc_conn(db_conn), {}, call_model=lambda **k: None, limit=50
    )

    assert summary["scanned"] == 0
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# select_adjudication_candidates -- issue #1939 batch partition
# ---------------------------------------------------------------------------


def test_backfill_partition_does_not_starve_blocked_unscored(monkeypatch, db_conn, company):
    """The blocked-unscored cohort (`classification IS NULL` + a non-CLEAN
    persisted `jd_content_verdict` -- the rows the D5 scoring_precheck gate
    defers as `awaiting_jd_adjudication`) must get a reserved slice of every
    batch even when scored rows dominate the queue."""
    from jobcannon.host import jd_adjudication_backfill
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    for i in range(6):
        _insert(
            db_conn,
            company,
            f"acme|scored|{i}",
            title=f"Data Platform Engineer S{i}",
            jd=_AMBIGUOUS_JD,
            first_seen="2026-01-10T00:00:00Z",
        )
    for i in range(2):
        _insert(
            db_conn,
            company,
            f"acme|blocked|{i}",
            title=f"Data Platform Engineer U{i}",
            jd=_AMBIGUOUS_JD,
            classification=None,
            first_seen="2026-01-01T00:00:00Z",
            jd_content_verdict=JdVerdict.AMBIGUOUS.value,
        )

    monkeypatch.setattr(
        jd_adjudication_backfill,
        "adjudicate_jd",
        lambda conn, title, company_name, jd_full, *, call_model, config: True,
    )

    summary = run_jd_adjudication_backfill(
        _svc_conn(db_conn), {}, call_model=lambda **k: None, limit=4
    )

    for i in range(2):
        row = _row(db_conn, f"acme|blocked|{i}")
        assert row["jd_adjudicated_version"] is not None, (
            f"blocked-unscored row acme|blocked|{i} was starved (not adjudicated)"
        )

    scored_stamped = db_conn.execute(
        "SELECT COUNT(*) AS n FROM postings "
        "WHERE dedup_key LIKE %s AND jd_adjudicated_version IS NOT NULL",
        ("acme|scored|%",),
    ).fetchone()["n"]
    assert scored_stamped == 2
    assert summary["scanned"] == 4
    assert summary["kept"] == 4


def test_backfill_partition_small_unscored_cohort_returns_capacity_to_scored(
    monkeypatch, db_conn, company
):
    """A small blocked-unscored cohort must not waste batch capacity: the
    unused portion of the reserve flows back to the scored-retraction slice."""
    from jobcannon.host import jd_adjudication_backfill
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    for i in range(5):
        _insert(
            db_conn,
            company,
            f"acme|scored|{i}",
            title=f"Data Platform Engineer S{i}",
            jd=_AMBIGUOUS_JD,
            first_seen="2026-01-10T00:00:00Z",
        )
    _insert(
        db_conn,
        company,
        "acme|blocked|0",
        title="Data Platform Engineer U0",
        jd=_AMBIGUOUS_JD,
        classification=None,
        first_seen="2026-01-01T00:00:00Z",
        jd_content_verdict=JdVerdict.AMBIGUOUS.value,
    )

    monkeypatch.setattr(
        jd_adjudication_backfill,
        "adjudicate_jd",
        lambda conn, title, company_name, jd_full, *, call_model, config: True,
    )

    # limit=4, reserve=2 but only 1 unscored row exists -> scored_limit=3.
    summary = run_jd_adjudication_backfill(
        _svc_conn(db_conn), {}, call_model=lambda **k: None, limit=4
    )
    assert summary["scanned"] == 4  # 1 unscored + 3 scored
    assert summary["kept"] == 4


# ---------------------------------------------------------------------------
# heal leg (issue #360) -- REJECT decisions are applied via
# _jd_full.clear_jd_full + _assessment_writer.invalidate_job_score
# ---------------------------------------------------------------------------

# Deterministic-REJECT body (same shape as test_jd_full.py's WIKI_JD):
# jd_content_reject -> ("jd_full_offsite", "head_block_or_wiki"), so the
# driver heals it without spending an LLM call.
_WIKI_JD = "From Wikipedia, the free encyclopedia. City in California. " * 8


def test_backfill_heals_deterministic_reject_without_llm(monkeypatch, db_conn, company):
    """A stored body the deterministic contract REJECTs is healed with its
    contract reason (jd_full_offsite) and never reaches the LLM. The row was
    scored (classification + scoring_model set -- the scored-retraction sweep
    cohort), so the stale score must be retracted too."""
    from jobcannon.host import jd_adjudication_backfill
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    _insert(
        db_conn,
        company,
        "acme|wiki",
        title="Data Platform Engineer",
        jd=_WIKI_JD,
        scoring_model="test-model",
    )

    calls = {"n": 0}

    def counting_adjudicate(conn, title, company_name, jd_full, *, call_model, config):
        calls["n"] += 1
        return True

    monkeypatch.setattr(jd_adjudication_backfill, "adjudicate_jd", counting_adjudicate)
    summary = run_jd_adjudication_backfill(
        _svc_conn(db_conn), {}, call_model=lambda **k: None, limit=50
    )

    assert summary["scanned"] == 1
    assert summary["llm_calls"] == 0  # deterministic REJECT: no LLM call
    assert calls["n"] == 0
    assert summary["rejected"] == 1
    assert summary["skipped_stale"] == 0

    row = _row(db_conn, "acme|wiki")
    assert row["jd_full"] is None
    assert row["unresolved_reasons"] == [JD_OFFSITE]
    assert row["classification"] is None  # stale score retracted
    assert row["scoring_model"] is None
    assert row["jd_content_verdict"] is None
    assert row["jd_adjudicated_version"] is None


def test_backfill_heal_skips_stale_premise(monkeypatch, db_conn, company):
    """#1060 Blocker 1 on the heal leg: a concurrent writer that rewrites
    jd_full between classification and write-back must make the heal's CAS
    guard miss -- the new (unseen) body is kept, the score is NOT retracted,
    and the miss lands in skipped_stale."""
    from jobcannon.host import jd_adjudication_backfill
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    _insert(
        db_conn,
        company,
        "acme|stale-heal",
        title="Data Platform Engineer",
        jd=_AMBIGUOUS_JD,
        scoring_model="test-model",
    )
    rewritten = "a completely different body, rewritten concurrently " * 5

    def rewriting_adjudicate(conn, title, company_name, jd_full, *, call_model, config):
        # Simulates the racing writer landing between our SELECT/classify and
        # the post-loop write-back (same-connection injection, mirroring
        # test_jd_full.py's interleaved-write pattern).
        db_conn.execute(
            "UPDATE postings SET jd_full = %s WHERE dedup_key = %s",
            (rewritten, "acme|stale-heal"),
        )
        return False  # NO on the OLD body -> heal decision queued on a stale premise

    monkeypatch.setattr(jd_adjudication_backfill, "adjudicate_jd", rewriting_adjudicate)

    summary = run_jd_adjudication_backfill(
        _svc_conn(db_conn), {}, call_model=lambda **k: None, limit=50
    )

    assert summary["rejected"] == 1  # decision was still a reject
    assert summary["skipped_stale"] == 1  # but the write-back missed
    row = _row(db_conn, "acme|stale-heal")
    assert row["jd_full"] == rewritten  # new content NOT deleted
    assert row["unresolved_reasons"] == []  # not quarantined
    assert row["classification"] == "apply"  # score NOT retracted
    assert row["scoring_model"] == "test-model"


def test_backfill_healed_row_is_not_reselected(monkeypatch, db_conn, company):
    """The cost consequence #360 exists to close: once a REJECT is applied,
    the row must leave the eligibility cohort permanently -- the peeled
    behavior re-selected (and re-paid the LLM call for) the same row every
    tick. Second run must scan nothing and call nothing."""
    from jobcannon.host import jd_adjudication_backfill
    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    _insert(db_conn, company, "acme|no-once", title="Data Platform Engineer", jd=_AMBIGUOUS_JD)

    calls = {"n": 0}

    def fake_adjudicate(conn, title, company_name, jd_full, *, call_model, config):
        calls["n"] += 1
        return False

    monkeypatch.setattr(jd_adjudication_backfill, "adjudicate_jd", fake_adjudicate)

    svc = _svc_conn(db_conn)
    first = run_jd_adjudication_backfill(svc, {}, call_model=lambda **k: None, limit=50)
    assert first["rejected"] == 1
    assert calls["n"] == 1

    second = run_jd_adjudication_backfill(svc, {}, call_model=lambda **k: None, limit=50)
    assert second["scanned"] == 0
    assert second["llm_calls"] == 0
    assert calls["n"] == 1  # no second adjudication paid


def test_backfill_heal_commits_durably_on_bare_connection(postgres_test_dsn):
    """Production durability path for the heal leg: every other test here runs
    under db_conn's ambient transaction (nested-savepoint + rollback), but the
    scheduled task runs on a bare pooled connection where the driver's
    `with_write_txn` block degrades to a savepoint over the implicit
    transaction the SELECTs opened and its trailing commit_unless_nested does
    the real commit. A second connection must observe the heal as committed."""
    import psycopg
    from psycopg.rows import dict_row

    from jobcannon.host.jd_adjudication_backfill import run_jd_adjudication_backfill

    dedup_key = "bare-co|wikipedia"
    conn_a = psycopg.connect(postgres_test_dsn, row_factory=dict_row)
    conn_b = psycopg.connect(postgres_test_dsn, row_factory=dict_row, autocommit=True)
    try:
        cid = conn_a.execute(
            "INSERT INTO companies (name) VALUES ('bare-co') RETURNING id"
        ).fetchone()["id"]
        conn_a.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, "
            "classification) VALUES (%s, %s, 'Engineer', 'bare-co', %s, 'apply')",
            (dedup_key, cid, _WIKI_JD),
        )
        conn_a.commit()

        summary = run_jd_adjudication_backfill(conn_a, {}, call_model=lambda **k: None, limit=50)
        assert summary["rejected"] == 1

        row = conn_b.execute(
            "SELECT jd_full, unresolved_reasons, classification FROM postings WHERE dedup_key = %s",
            (dedup_key,),
        ).fetchone()
        assert row["jd_full"] is None
        assert row["unresolved_reasons"] == [JD_OFFSITE]
        assert row["classification"] is None
    finally:
        try:
            conn_a.rollback()
            conn_a.execute("DELETE FROM postings WHERE dedup_key = %s", (dedup_key,))
            conn_a.execute("DELETE FROM companies WHERE name = 'bare-co'")
            conn_a.commit()
        finally:
            conn_a.close()
            conn_b.close()

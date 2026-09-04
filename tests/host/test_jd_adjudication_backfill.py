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

Adapted for the heal-leg peel (see jobcannon/host/jd_adjudication_backfill.py's
module docstring PORT-SEAM): `heal_offsite` is not shipped in this unit. Tests
that exercised the private heal path (`test_backfill_state_machine`'s
AMBIGUOUS-NO assertions, `test_backfill_reclassifies_row_with_unrelated_
unresolved_reason`'s post-heal assertions) are adapted to assert the new
counted-but-unapplied behavior: the row is left completely untouched and
`rejected` is incremented, rather than asserting a cleared/quarantined row.
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
):
    """Insert a posting row shaped for these tests. Unlike the private `_insert`
    helper, `scoring_model` is never set here -- no test in this port needs a
    scored row's `scoring_model` value (heal, the only path that nulled it, is
    peeled), so the m0015 `postings_scoring_model_requires_classification`
    CHECK (scoring_model IS NULL OR classification IS NOT NULL) is trivially
    satisfied."""
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
    placeholders = ", ".join(["%s"] * len(cols))
    db_conn.execute(
        f"INSERT INTO postings ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )


def _row(db_conn, dedup_key):
    return db_conn.execute(
        "SELECT jd_full, jd_adjudicated_version, unresolved_reasons, classification "
        "FROM postings WHERE dedup_key = %s",
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

    # AMBIGUOUS-NO: heal is peeled in this unit (see module docstring PORT-SEAM)
    # -- counted in `rejected` above, but the row is left COMPLETELY untouched
    # (unlike the private original, which healed it here). It naturally
    # re-selects and re-classifies next tick once the heal fast-follow lands.
    no = _row(db_conn, "acme|no")
    assert no["jd_full"] == _AMBIGUOUS_JD
    assert no["jd_adjudicated_version"] is None
    assert no["classification"] == "apply"

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

    # Heal is peeled in this unit: the row is left untouched (not healed), so
    # the pre-existing unrelated reason survives unchanged and no jd-content
    # reason is added (that append is heal_offsite's job, deferred).
    row = db_conn.execute(
        "SELECT jd_full, unresolved_reasons FROM postings WHERE dedup_key = %s",
        ("acme|location-quarantined",),
    ).fetchone()
    assert row["unresolved_reasons"] == ["location_missing"]
    assert JD_OFFSITE not in row["unresolved_reasons"]
    assert row["jd_full"] == _AMBIGUOUS_JD


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

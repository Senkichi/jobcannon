"""Host-dialect tests for jobcannon.host.scoring_runner (ledger L-0263).

Scope: `_worker_count`/`run_scoring` behavior against real Postgres
(tests/host/conftest.py's db_conn / throwaway-pool convention, matching
tests/host/test_ats_prober_dialect.py's own pool-fixture pattern) plus the
#361-refuter-flag regression test: a job dict built the way this module
builds it (`SELECT * FROM postings`) must carry `jd_adjudicated_version`
through to `scoring_precheck`, so a stamped posting is never spuriously
gated by the D5 jd-adjudication check.

`check_job_liveness` is never exercised live here (it makes real HTTP
calls) -- tests that reach the liveness gate set `expiry_checked_at` to a
fresh timestamp so the D-11 TTL gate skips the check entirely, matching
the private module's own re-check-suppression behavior for a recently
verified row.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.jd_content_contract import JD_CONTENT_VERSION, JdVerdict
from jobcannon.engine.job_scorer import scoring_precheck
from jobcannon.host import scoring_runner
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def _insert_company(conn, *, name: str) -> int:
    conn.execute("INSERT INTO companies (name) VALUES (%s)", (name,))
    return conn.execute("SELECT id FROM companies WHERE name = %s", (name,)).fetchone()["id"]


def _insert_posting(conn, *, dedup_key: str, company_id: int, **cols) -> None:
    base = {
        "dedup_key": dedup_key,
        "company_id": company_id,
        "title": "Staff Engineer",
        "company": "score-co",
        "jd_full": "A" * 2000,
        "location": "Remote",
    }
    base.update(cols)
    fields = ", ".join(base)
    placeholders = ", ".join(["%s"] * len(base))
    conn.execute(
        f"INSERT INTO postings ({fields}) VALUES ({placeholders})",
        tuple(base.values()),
    )


class _FakeResult:
    def __init__(self, status="ok"):
        self.status = status
        self.data = None
        self.provider = "fake"
        self.model = "fake-model"


def test_worker_count_defaults_to_one(monkeypatch):
    monkeypatch.delenv("JC_SCORE_WORKERS", raising=False)
    assert scoring_runner._worker_count() == 1


def test_worker_count_reads_env_var(monkeypatch):
    monkeypatch.setenv("JC_SCORE_WORKERS", "4")
    assert scoring_runner._worker_count() == 4


def test_worker_count_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("JC_SCORE_WORKERS", "not-a-number")
    assert scoring_runner._worker_count() == 1
    monkeypatch.setenv("JC_SCORE_WORKERS", "0")
    assert scoring_runner._worker_count() == 1
    monkeypatch.setenv("JC_SCORE_WORKERS", "-3")
    assert scoring_runner._worker_count() == 1


def test_run_scoring_empty_list_short_circuits(monkeypatch):
    # No connection_factory call should happen at all for an empty batch --
    # patch it to raise so this test fails loudly if that invariant regresses.
    def _boom(*_a, **_kw):
        raise AssertionError("connection_factory() must not be called for an empty batch")

    monkeypatch.setattr(scoring_runner, "connection_factory", _boom)
    summary = scoring_runner.run_scoring([], {})
    assert summary == {
        "scored": 0,
        "classified_apply": 0,
        "classified_consider": 0,
        "classified_skip": 0,
        "classified_reject": 0,
        "skipped_dead": 0,
        "skipped_no_jd": 0,
        "deferred": 0,
        "errors": 0,
    }


def test_stamped_posting_job_dict_passes_scoring_precheck(db_conn):
    """#361 REFUTER FLAG: the job dict scoring_runner builds (`SELECT * FROM
    postings WHERE dedup_key = %s`, the exact query _process_one_job/
    _prefetch_liveness run) must carry `jd_adjudicated_version` so a posting
    the adjudicator has vouched for is never re-gated by scoring_precheck's
    D5 check. Uses a non-CLEAN verdict (AMBIGUOUS) specifically because the
    D5 gate is a no-op on jd_content_verdict IS NULL / CLEAN -- only a
    REJECT/AMBIGUOUS verdict exercises the jd_adjudicated_version comparison
    this test is pinning."""
    cid = _insert_company(db_conn, name="refuter-co")
    _insert_posting(
        db_conn,
        dedup_key="refuter-co|staff-engineer",
        company_id=cid,
        jd_content_verdict=JdVerdict.AMBIGUOUS.value,
        jd_adjudicated_version=JD_CONTENT_VERSION,
    )

    row = db_conn.execute(
        "SELECT * FROM postings WHERE dedup_key = %s",
        ("refuter-co|staff-engineer",),
    ).fetchone()
    job = dict(row)

    assert job["jd_adjudicated_version"] == JD_CONTENT_VERSION
    assert scoring_precheck(job) is None


def test_unstamped_ambiguous_posting_is_gated(db_conn):
    """Negative control for the test above: an AMBIGUOUS verdict with NO
    adjudication (jd_adjudicated_version NULL) must still gate -- proves the
    positive result isn't a vacuous pass from a broken/no-op check."""
    cid = _insert_company(db_conn, name="refuter-co-2")
    _insert_posting(
        db_conn,
        dedup_key="refuter-co-2|staff-engineer",
        company_id=cid,
        jd_content_verdict=JdVerdict.AMBIGUOUS.value,
    )
    row = db_conn.execute(
        "SELECT * FROM postings WHERE dedup_key = %s",
        ("refuter-co-2|staff-engineer",),
    ).fetchone()
    job = dict(row)
    assert job["jd_adjudicated_version"] is None
    assert scoring_precheck(job) == "awaiting_jd_adjudication"


@pytest.fixture
def scoring_pool():
    """Own throwaway DB + open pool, mirroring test_ats_prober_dialect.py's
    fixture -- run_scoring drives jobcannon.host.scoring_runner.connection_
    factory (the module-level pool), so a real open pool is required rather
    than the rollback-isolated db_conn fixture (which never commits)."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations

    dsn, db_name = create_throwaway_db("jobcannon_scoring_runner")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        yield pool_mod
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def test_run_scoring_scores_a_stamped_job_and_reads_classification_back(scoring_pool, monkeypatch):
    with scoring_pool.connection_factory() as conn:
        raw = conn.raw
        cid = _insert_company(raw, name="pool-co")
        _insert_posting(
            raw,
            dedup_key="pool-co|staff-engineer",
            company_id=cid,
            jd_content_verdict=JdVerdict.CLEAN.value,
        )
        # Fresh expiry_checked_at so the D-11 TTL gate skips the (real,
        # network-making) liveness check entirely for this test.
        raw.execute(
            "UPDATE postings SET expiry_checked_at = now() WHERE dedup_key = %s",
            ("pool-co|staff-engineer",),
        )
        raw.commit()

    seen_jobs = []

    def _fake_score_and_persist_job(job, conn, config, *, run_id=None, timeout=None):
        seen_jobs.append(job)
        raw = conn.raw if hasattr(conn, "raw") else conn
        raw.execute(
            "UPDATE postings SET classification = 'apply' WHERE dedup_key = %s",
            (job["dedup_key"],),
        )
        raw.commit()
        return _FakeResult(status="ok")

    monkeypatch.setattr(scoring_runner, "score_and_persist_job", _fake_score_and_persist_job)

    summary = scoring_runner.run_scoring(["pool-co|staff-engineer"], {})

    assert summary["scored"] == 1
    assert summary["classified_apply"] == 1
    assert len(seen_jobs) == 1
    # REFUTER FLAG, end-to-end through run_scoring's own SELECT *.
    assert "jd_adjudicated_version" in seen_jobs[0]


def test_run_scoring_skips_excluded_job_without_touching_pipeline_status(scoring_pool, monkeypatch):
    """Proves the dropped update_pipeline_status auto-dismiss leg (design
    note L-0263 seam #4, gated per the note's own authorized alternative):
    an excluded job is skipped, never scored, and never reaches
    score_and_persist_job -- no pipeline_status write is attempted at all
    (jobcannon.db._user_actions remains the sole writer of that table)."""
    with scoring_pool.connection_factory() as conn:
        raw = conn.raw
        cid = _insert_company(raw, name="exclude-co")
        _insert_posting(
            raw,
            dedup_key="exclude-co|excluded-role",
            company_id=cid,
            title="Excluded Title Marker",
        )
        raw.commit()

    def _boom(*_a, **_kw):
        raise AssertionError("score_and_persist_job must not be called for an excluded job")

    monkeypatch.setattr(scoring_runner, "score_and_persist_job", _boom)

    config = {"profile": {"exclusions": {"title_keywords": ["Excluded Title Marker"]}}}
    summary = scoring_runner.run_scoring(["exclude-co|excluded-role"], config)

    assert summary["scored"] == 0
    assert summary["skipped_no_jd"] == 1

    with scoring_pool.connection_factory() as conn:
        row = conn.raw.execute("SELECT COUNT(*) AS n FROM pipeline_status").fetchone()
        assert row["n"] == 0


def test_score_task_delegates_to_run_scoring(monkeypatch):
    from jobcannon.host import tasks

    calls = []

    def _fake_run_scoring(dedup_keys, config, **kwargs):
        calls.append((dedup_keys, config))
        return {"scored": len(dedup_keys)}

    monkeypatch.setattr(tasks, "run_scoring", _fake_run_scoring)
    monkeypatch.setattr("jobcannon.engine.runtime_config.get_runtime_config", lambda: {"x": 1})

    result = tasks.score(["a", "b"])

    assert result == {"scored": 2}
    assert calls == [(["a", "b"], {"x": 1})]
    assert tasks.score.name in tasks.app.tasks

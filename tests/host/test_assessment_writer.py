"""Host-dialect tests for jobcannon.db._assessment_writer (ledger L-0064).

Scope note: exercises persist_job_assessment / invalidate_job_score directly
against real Postgres (tests/host/conftest.py's db_conn fixture), matching
tests/host/test_companies.py / test_jd_full.py's own convention. Does NOT
cover wiring a caller (score_and_persist_job has no in-tree caller yet, per
jobcannon.engine.job_scorer's own module docstring) -- that wiring is
out of scope here, mirroring L-0077/L-0078's unwired-writer precedent.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from jobcannon.db._assessment_writer import (
    _postings_optional_columns,
    invalidate_job_score,
    persist_job_assessment,
)
from jobcannon.engine.classification import JobAssessment
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres

_STRONG_SUB_SCORES = {
    "title_fit": 5,
    "location_fit": 4,
    "comp_fit": 4,
    "domain_match": 5,
    "seniority_match": 4,
    "skills_match": 4,
}
_FLAT_NEUTRAL_SUB_SCORES = dict.fromkeys(_STRONG_SUB_SCORES, 3)
_REJECT_SUB_SCORES = {**_STRONG_SUB_SCORES, "comp_fit": 1}


def _svc_conn(db_conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(db_conn)


@pytest.fixture()
def posting(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('assess-co')")
    cid = db_conn.execute("SELECT id FROM companies WHERE name='assess-co'").fetchone()["id"]
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, jd_full) "
        "VALUES ('assess-co|staff engineer', %s, 'Staff Engineer', 'assess-co', "
        "'A' || repeat('b', 2000))",
        (cid,),
    )
    return "assess-co|staff engineer"


def _row(db_conn, dedup_key):
    return db_conn.execute(
        "SELECT classification, sub_scores_json, fit_analysis, scoring_provider, scoring_model "
        "FROM postings WHERE dedup_key = %s",
        (dedup_key,),
    ).fetchone()


def test_postings_optional_columns_reflects_live_schema(db_conn):
    # Current live schema (through m0015) carries neither legitimacy_note
    # nor enrichment_tier -- see the module's PORT-SEAM docstring. This test
    # is a deliberate tripwire: it should start failing the moment a future
    # migration adds either column, which is the intended signal to revisit
    # the corresponding derive_classification branch.
    assert _postings_optional_columns(db_conn) == set()


def test_persist_writes_scoring_tuple_and_derives_apply(db_conn, posting):
    conn = _svc_conn(db_conn)
    assessment = JobAssessment(
        sub_scores=_STRONG_SUB_SCORES,
        classification="",
        rationale={"strengths": ["deep backend experience"]},
        provider="ollama",
    )
    result = persist_job_assessment(
        conn, posting, assessment, provider="ollama", model="qwen2.5:14b"
    )
    assert result == "apply"

    row = _row(db_conn, posting)
    assert row["classification"] == "apply"
    assert row["sub_scores_json"] == _STRONG_SUB_SCORES
    assert row["fit_analysis"]["strengths"] == ["deep backend experience"]
    assert row["scoring_provider"] == "ollama"
    assert row["scoring_model"] == "qwen2.5:14b"


def test_persist_returns_none_for_missing_dedup_key(db_conn):
    conn = _svc_conn(db_conn)
    assessment = JobAssessment(sub_scores=_STRONG_SUB_SCORES, classification="", rationale={})
    assert persist_job_assessment(conn, "does-not-exist", assessment) is None


def test_persist_derives_reject_on_any_axis_one(db_conn, posting):
    conn = _svc_conn(db_conn)
    assessment = JobAssessment(sub_scores=_REJECT_SUB_SCORES, classification="", rationale={})
    result = persist_job_assessment(
        conn, posting, assessment, provider="gemini", model="gemini-2.5"
    )
    assert result == "reject"
    assert _row(db_conn, posting)["classification"] == "reject"


def test_persist_derives_low_signal_on_flat_neutral(db_conn, posting):
    conn = _svc_conn(db_conn)
    assessment = JobAssessment(sub_scores=_FLAT_NEUTRAL_SUB_SCORES, classification="", rationale={})
    result = persist_job_assessment(conn, posting, assessment)
    assert result == "low_signal"


def test_persist_scoring_provider_preserved_when_model_none_on_recall(db_conn, posting):
    conn = _svc_conn(db_conn)
    first = JobAssessment(sub_scores=_STRONG_SUB_SCORES, classification="", rationale={})
    persist_job_assessment(conn, posting, first, provider="ollama", model="qwen2.5:14b")

    second = JobAssessment(sub_scores=_FLAT_NEUTRAL_SUB_SCORES, classification="", rationale={})
    persist_job_assessment(conn, posting, second, provider=None, model=None)

    row = _row(db_conn, posting)
    assert row["classification"] == "low_signal"  # re-derived on the second call
    assert row["scoring_provider"] == "ollama"  # COALESCE preserved
    assert row["scoring_model"] == "qwen2.5:14b"  # COALESCE preserved


def test_persist_with_location_policy_verdict_overrides_location_fit(db_conn, posting):
    conn = _svc_conn(db_conn)
    # location_fit=1 would normally reject; a policy verdict overriding it
    # to 4 for classification purposes should avoid the reject branch.
    sub_scores = {**_STRONG_SUB_SCORES, "location_fit": 1}
    assessment = JobAssessment(sub_scores=sub_scores, classification="", rationale={})
    verdict_json = json.dumps({"effective_location_fit": 4})

    result = persist_job_assessment(
        conn, posting, assessment, location_policy_verdict_json=verdict_json
    )
    assert result != "reject"

    row = _row(db_conn, posting)
    # Raw LLM sub_scores (with location_fit=1) are what gets persisted --
    # only classification derivation used the policy override.
    assert row["sub_scores_json"]["location_fit"] == 1
    assert row["fit_analysis"]["location_policy"]["effective_location_fit"] == 4


def test_invalidate_job_score_nulls_tuple_leaves_provider(db_conn, posting):
    conn = _svc_conn(db_conn)
    assessment = JobAssessment(sub_scores=_STRONG_SUB_SCORES, classification="", rationale={})
    persist_job_assessment(conn, posting, assessment, provider="ollama", model="qwen2.5:14b")

    assert invalidate_job_score(conn, posting) is True

    row = _row(db_conn, posting)
    assert row["classification"] is None
    assert row["sub_scores_json"] is None
    assert row["fit_analysis"] is None
    assert row["scoring_model"] is None
    assert row["scoring_provider"] == "ollama"  # deliberately preserved


def test_invalidate_returns_false_for_missing_dedup_key(db_conn):
    conn = _svc_conn(db_conn)
    assert invalidate_job_score(conn, "does-not-exist") is False


def test_i05_check_blocks_scoring_model_without_classification(db_conn, posting):
    with pytest.raises(psycopg.errors.CheckViolation):
        with db_conn.transaction():
            db_conn.execute(
                "UPDATE postings SET scoring_model = %s, classification = NULL WHERE dedup_key = %s",
                ("qwen2.5:14b", posting),
            )

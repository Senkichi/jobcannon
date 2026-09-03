"""Tests for jobcannon.engine.scoring.scorer (new — not carried from the
private repo; see the port PR body for what was dropped and why).

Exists primarily as acceptance evidence that the ``thefuzz`` dependency
this port adds actually resolves in the public venv (an import ruff/uv
sync would not catch), plus basic coverage of JobScorer's own behavior
since no ledgered carried test exercised it directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobcannon.engine.models import Job
from jobcannon.engine.scoring.scorer import DEFAULT_MIN_SCORE_THRESHOLD, JobScorer


def _job(**overrides) -> Job:
    defaults = dict(
        title="Senior Backend Engineer",
        company="Acme Corp",
        location="Remote",
        source="test",
        source_url="https://example.com/job/1",
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_default_threshold_used_when_config_omits_it():
    scorer = JobScorer({})
    assert scorer.threshold == DEFAULT_MIN_SCORE_THRESHOLD


def test_config_threshold_overrides_default():
    scorer = JobScorer({"scoring": {"min_score_threshold": 55}})
    assert scorer.threshold == 55


def test_score_jobs_filters_below_threshold_and_sorts_desc():
    config = {
        "profile": {"target_titles": ["Senior Backend Engineer"]},
        "scoring": {
            "weights": {"title_match": 1.0},
            "min_score_threshold": 90,
        },
    }
    scorer = JobScorer(config)
    strong = _job(title="Senior Backend Engineer")
    weak = _job(title="Junior Sales Associate", company="Other Co")

    scored = scorer.score_jobs([weak, strong])

    assert [j.title for j in scored] == ["Senior Backend Engineer"]
    assert scored[0].score >= 90


def test_title_exclusion_hard_rejects():
    config = {
        "profile": {"target_titles": ["Engineer"], "exclusions": {"title_keywords": ["intern"]}}
    }
    scorer = JobScorer(config)
    assert scorer._score_title("Software Engineering Intern") == 0


def test_seniority_penalty_keyword_zeroes_score():
    scorer = JobScorer({})
    assert scorer._score_seniority("Junior Software Engineer") == 0
    assert scorer._score_seniority("Staff Software Engineer") == 100


def test_company_exclusion_hard_rejects():
    config = {"profile": {"exclusions": {"companies": ["bad co"]}}}
    scorer = JobScorer(config)
    assert scorer._score_company("Bad Co") == 0
    assert scorer._score_company("Good Co") == 50


def test_recency_scores_recent_posting_highest():
    scorer = JobScorer({})
    now = datetime.now(UTC)
    assert scorer._score_recency(now) == 100
    assert scorer._score_recency(now - timedelta(days=30)) == 20
    assert scorer._score_recency(None) == 50

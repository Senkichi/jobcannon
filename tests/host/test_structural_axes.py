"""Tests for the rules_v1 structural axes (1B Wave 2, PR 7).

Unit tests below need no DB — the four axis scorers are pure functions over
plain values. The one Postgres-backed test (batch scoring against real
`postings` rows) is individually marked with `requires_postgres` rather than
module-scoped, so the unit tests keep running without a local Postgres.
"""

from __future__ import annotations

import inspect

import pytest

from tests.host.conftest import requires_postgres

# ---------------------------------------------------------------------------
# comp_transparency
# ---------------------------------------------------------------------------


def test_comp_transparency_structured_salary_present():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    result = score_comp_transparency(120000, 150000, None)
    assert result == {"value": True, "method": "structured"}


def test_comp_transparency_clean_range_sentence():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    jd = (
        "We build great things. The salary range for this role is $120,000 to "
        "$150,000 per year. Apply now."
    )
    result = score_comp_transparency(None, None, jd)
    assert result["value"] is True
    assert result["method"] == "regex_grammar"


def test_comp_transparency_ambiguous_trap_sentence():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    # A resolvable range that shares a sentence with a non-base-pay dollar
    # figure (a sign-on bonus, not base salary) must not be scored True.
    jd = (
        "Join our growing team. This role comes with a $50,000 - $60,000 "
        "sign-on bonus paid over two years."
    )
    result = score_comp_transparency(None, None, jd)
    assert result["value"] == "ambiguous"
    assert result["method"] == "regex_grammar"
    assert "candidate_sentence" in result


def test_comp_transparency_no_range_found():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    jd = "We are looking for a great engineer to join our growing team. Apply today."
    result = score_comp_transparency(None, None, jd)
    assert result == {"value": False, "method": "regex_grammar"}


def test_comp_transparency_none_jd_full():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    result = score_comp_transparency(None, None, None)
    assert result == {"value": False, "method": "regex_grammar"}


def test_comp_transparency_bare_numeric_range_is_not_salary():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    # No currency anywhere — headcount/scale prose, not pay (finding 1).
    jd = "The team supports 200-500 users across the platform daily."
    assert score_comp_transparency(None, None, jd) == {"value": False, "method": "regex_grammar"}


def test_comp_transparency_bare_k_range_headcount_is_not_salary():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    jd = "Our platform serves 100K-500K users every single day."
    assert score_comp_transparency(None, None, jd) == {"value": False, "method": "regex_grammar"}


def test_comp_transparency_base_salary_with_benefits_is_transparent():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    # Explicit base-salary label -> the trailing equity/401k mention must NOT
    # make the labeled base range ambiguous (finding 2).
    jd = "The base salary range for this role is $120,000 to $150,000, plus equity and 401k match."
    result = score_comp_transparency(None, None, jd)
    assert result["value"] is True
    assert result["method"] == "regex_grammar"


def test_comp_transparency_single_figure_salary_is_transparent():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    # A lone currency-anchored figure is a real disclosure (finding 3).
    jd = "This role pays a base salary of $120,000 per year, plus full benefits."
    result = score_comp_transparency(None, None, jd)
    assert result["value"] is True
    assert result["method"] == "regex_grammar"


def test_comp_transparency_single_figure_requires_currency():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    # A lone number with no currency is not pay (guards the single-figure path).
    jd = "This role supports 120,000 daily active users."
    assert score_comp_transparency(None, None, jd) == {"value": False, "method": "regex_grammar"}


def test_comp_transparency_revenue_figure_is_not_salary():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    for jd in (
        "Our product helps save customers $100,000 annually on cloud costs.",
        "We generated $2,000,000 in revenue last year.",
        "The team was awarded a $300,000 annual research grant.",
        "This role manages a budget of $2,000,000.",
        "We process $2,000,000 in transactions annually.",
    ):
        assert score_comp_transparency(None, None, jd) == {
            "value": False,
            "method": "regex_grammar",
        }, jd


def test_comp_transparency_unlabeled_equity_or_bonus_figure_is_not_true():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    jd1 = (
        "Salary is not the focus here -- we instead offer $200,000 in equity "
        "(RSUs) vesting over four years."
    )
    jd2 = (
        "The salary is comparable to similar roles and this role also includes "
        "a $40,000 signing bonus."
    )
    # A lone bonus/equity figure with no base-pay label must NOT read as a
    # transparent base-pay disclosure.
    assert score_comp_transparency(None, None, jd1)["value"] is not True
    assert score_comp_transparency(None, None, jd2)["value"] is not True


def test_comp_transparency_spelled_out_currency_range_is_transparent():
    from jobcannon.host.structural_axes.comp_transparency import score_comp_transparency

    jd = "Compensation range: 90,000 to 110,000 dollars per year."
    result = score_comp_transparency(None, None, jd)
    assert result["value"] is True
    assert result["method"] == "regex_grammar"


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------


def test_freshness_stale_overrides_recent_posted_date():
    from datetime import date

    from jobcannon.host.structural_axes.freshness import score_freshness

    result = score_freshness(date.today(), "exact", None, True, None)
    assert result["value"] <= 0.2
    assert result["method"] == "rules_v1"


def test_freshness_expired_status_overrides_recent_posted_date():
    from datetime import date

    from jobcannon.host.structural_axes.freshness import score_freshness

    result = score_freshness(date.today(), "exact", None, False, "expired")
    assert result["value"] <= 0.2


def test_freshness_recent_posted_date_scores_high():
    from datetime import date

    from jobcannon.host.structural_axes.freshness import score_freshness

    result = score_freshness(date.today(), "exact", None, False, None)
    assert result["value"] == 1.0


def test_freshness_no_usable_date_falls_back_to_flat_default():
    from jobcannon.host.structural_axes.freshness import score_freshness

    result = score_freshness(None, None, None, False, None)
    assert result["value"] == 0.3


# ---------------------------------------------------------------------------
# seniority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Senior Software Engineer", True),
        ("Software Engineer", False),
        ("Team Leader", False),  # word boundary, not a 'Lead' substring
        ("Sr. Software Engineer", True),
        ("Lead Generation Specialist", False),  # 'lead generation' is not a level
        ("Lead Engineer", True),
        ("Lead", True),
        ("Lead Generative AI Engineer", True),
        ("Lead Generalist", True),
        ("Lead Geneticist", True),
        ("Lead General Counsel", True),
    ],
)
def test_seniority_clarity(title, expected):
    from jobcannon.host.structural_axes.seniority import score_seniority_clarity

    result = score_seniority_clarity(title)
    assert result["value"] is expected
    assert result["method"] == "rules_v1"


# ---------------------------------------------------------------------------
# jd_quality
# ---------------------------------------------------------------------------


def test_jd_quality_zero_siblings_no_crash():
    from jobcannon.host.structural_axes.jd_quality import score_jd_quality

    jd = (
        "We are looking for a great Software Engineer to join our team. "
        "Responsibilities include coding, testing, and shipping features."
    )
    result = score_jd_quality(jd, [])
    assert 0.0 <= result["value"] <= 1.0
    assert result["method"] == "rules_v1"


def test_jd_quality_empty_jd_is_zero():
    from jobcannon.host.structural_axes.jd_quality import score_jd_quality

    result = score_jd_quality(None, [])
    assert result == {"value": 0.0, "method": "rules_v1"}


# ---------------------------------------------------------------------------
# No-LLM boundary guard
# ---------------------------------------------------------------------------


def test_no_llm_boundary_guard():
    """Structural axes are zero-LLM by design — grep the source for any
    reference to the model-provider cascade and fail loudly if one creeps in."""
    from jobcannon.host import structural_axes
    from jobcannon.host.structural_axes import (
        comp_transparency,
        freshness,
        jd_quality,
        seniority,
    )

    forbidden = ("call_model", "model_provider", "providers")
    modules = (structural_axes, comp_transparency, freshness, jd_quality, seniority)
    for module in modules:
        src = inspect.getsource(module)
        for term in forbidden:
            assert term not in src, f"{module.__name__} references forbidden term {term!r}"


# ---------------------------------------------------------------------------
# Integration — real Postgres postings rows
# ---------------------------------------------------------------------------


def _svc_conn(db_conn):
    from jobcannon.db.pool import EngineCompatConnection

    return EngineCompatConnection(db_conn)


@pytest.fixture()
def company_id(db_conn):
    db_conn.execute("INSERT INTO companies (name) VALUES ('Structural Axes Co')")
    return db_conn.execute("SELECT id FROM companies WHERE name='Structural Axes Co'").fetchone()[
        "id"
    ]


_REAL_JD = (
    "Senior Data Scientist at Acme. We are looking for a Senior Data Scientist to "
    "join our analytics team. Responsibilities include building machine learning "
    "models, running experiments, and partnering with product. Qualifications: 5+ "
    "years of experience with Python and SQL, strong statistics background. What "
    "you'll do: design data pipelines, ship models to production, mentor analysts. "
) * 3


@requires_postgres
def test_score_pending_structural_axes_integration(db_conn, company_id):
    from jobcannon.host.structural_axes import score_pending_structural_axes

    # One posting with structured salary, one with a regex-detectable range,
    # one with no salary signal at all.
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, "
        "salary_min, salary_max) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            "struct-1",
            company_id,
            "Senior Data Scientist",
            "Structural Axes Co",
            _REAL_JD,
            120000,
            150000,
        ),
    )
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, jd_full) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            "struct-2",
            company_id,
            "Data Scientist",
            "Structural Axes Co",
            _REAL_JD + " The salary range for this role is $120,000 to $150,000 per year.",
        ),
    )
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, jd_full) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("struct-3", company_id, "Data Scientist", "Structural Axes Co", _REAL_JD),
    )

    scored = score_pending_structural_axes(_svc_conn(db_conn), {})
    assert scored == 3

    rows = {
        row["dedup_key"]: row
        for row in db_conn.execute(
            "SELECT dedup_key, structural_scoring_method, structural_scored_at, "
            "structural_axes, salary_min, salary_max FROM postings"
        ).fetchall()
    }
    assert set(rows) == {"struct-1", "struct-2", "struct-3"}
    for row in rows.values():
        assert row["structural_scoring_method"] == "rules_v1"
        assert row["structural_scored_at"] is not None
        assert set(row["structural_axes"]) == {
            "freshness",
            "seniority_clarity",
            "comp_transparency",
            "jd_quality",
        }

    # Single-writer invariant: structural scoring must never touch salary columns.
    assert rows["struct-1"]["salary_min"] == 120000
    assert rows["struct-1"]["salary_max"] == 150000
    assert rows["struct-2"]["salary_min"] is None
    assert rows["struct-2"]["salary_max"] is None
    assert rows["struct-3"]["salary_min"] is None
    assert rows["struct-3"]["salary_max"] is None

    assert rows["struct-1"]["structural_axes"]["comp_transparency"] == {
        "value": True,
        "method": "structured",
    }


@requires_postgres
def test_score_pending_scores_jd_less_posting(db_conn, company_id):
    from jobcannon.host.structural_axes import score_pending_structural_axes

    # A posting whose jd_full never arrived must still get its JD-independent
    # axes (freshness, seniority) scored — it must not be silently dropped
    # (finding 4: the old jd_full IS NOT NULL eligibility gate excluded it).
    db_conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) VALUES (%s, %s, %s, %s)",
        ("jdless-1", company_id, "Senior Data Scientist", "Structural Axes Co"),
    )
    scored = score_pending_structural_axes(_svc_conn(db_conn), {})
    assert scored == 1
    row = db_conn.execute(
        "SELECT structural_scoring_method, structural_axes FROM postings "
        "WHERE dedup_key = 'jdless-1'"
    ).fetchone()
    assert row["structural_scoring_method"] == "rules_v1"
    assert set(row["structural_axes"]) == {
        "freshness",
        "seniority_clarity",
        "comp_transparency",
        "jd_quality",
    }
    # seniority is derivable from the title even with no JD.
    assert row["structural_axes"]["seniority_clarity"]["value"] is True
    assert row["structural_axes"]["comp_transparency"] == {
        "value": False,
        "method": "regex_grammar",
    }
    assert row["structural_axes"]["jd_quality"]["value"] == 0.0

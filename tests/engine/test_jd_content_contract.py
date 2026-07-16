"""Tests for the fail-closed jd-content contract (I-18).

Covers:
  * classify_jd_content / jd_content_reject — the 3-way verdict + high-precision
    deterministic REJECT signals.
  * Over-fire guards — the legitimate JDs a naive denylist would destroy (a JD
    that merely mentions cloudflare/cookies/javascript deep in prose, a Built-In
    "404 Total Employees" stat, a benign "no longer" phrase). These are the
    regression guard against the contract over-firing — the same discipline the
    title contract earned the hard way.
  * ParsedJob.from_job ingest gate — offsite jd_full is quarantined + cleared.

Ported from the private repo's tests/test_jd_content_contract.py. The
set_jd_full storage-gate tests and the _run_jd_content_resweep_if_stale
retroactive re-sweep tests are NOT ported — both need a fully migrated DB
(job_finder.db._jd_full / job_finder.web.migrations._post_hooks are outside
this task's manifest).
"""

from __future__ import annotations

import pytest

from jobcannon.engine.jd_content_contract import (
    JD_EXPIRED,
    JD_OFFSITE,
    JdVerdict,
    classify_jd_content,
    jd_content_reject,
)

# A real, well-formed JD body reused across tests (shape + title grounding + len).
_REAL_JD = (
    "Senior Data Scientist at Acme. We are looking for a Senior Data Scientist to "
    "join our analytics team. Responsibilities include building machine learning "
    "models, running experiments, and partnering with product. Qualifications: 5+ "
    "years of experience with Python and SQL, strong statistics background. What "
    "you'll do: design data pipelines, ship models to production, mentor analysts. "
) * 3

# ---------------------------------------------------------------------------
# REJECT — deterministic high-precision (reason, signal)
# ---------------------------------------------------------------------------

_REJECTS = [
    # (jd_full, title, expected_reason)
    (
        "Alameda, California - Wikipedia Jump to content From Wikipedia, the free "
        "encyclopedia City in California " * 5,
        "Information Systems Manager",
        JD_OFFSITE,
    ),
    (
        "JLA FORUMS - REQUEST DENIED! You appear to be in violation of our Terms "
        "Of Service. Your request to view this site has been denied. " * 4,
        "Sr. Biological Data Scientist",
        JD_OFFSITE,
    ),
    (
        "399 Clinical Research Coordinator jobs in Boston Skip to main content 25 "
        "miles Exact location Done Any time " * 4,
        "Clinical Research Coordinator",
        JD_OFFSITE,
    ),
    (
        "1,000+ Chief Clinical Officer jobs in United States Skip to main content "
        "Any time Past month Past week Done Company " * 4,
        "Clinical AI Specialist",
        JD_OFFSITE,
    ),
    (
        "404 not found. The page you requested could not be located on this server. " * 5,
        "Senior Data Analyst",
        JD_OFFSITE,
    ),
    (
        "Senior Data Scientist at Adobe. We're sorry, the job you are trying to "
        "apply for has been filled. Maybe you would like another role. " * 4,
        "Senior Data Scientist",
        JD_EXPIRED,
    ),
    (
        "This position is no longer available. Please browse our other openings "
        "for current opportunities at our company. " * 4,
        "Senior Data Scientist",
        JD_EXPIRED,
    ),
    # title_zero_overlap: a substantial body that shares none of the title stems.
    (
        "Join the smartmedia technologies team. Our values bring us together. "
        "Passion: we pursue our best. Integrity in everything we do. " * 5,
        "Quantum Photonics Researcher",
        JD_OFFSITE,
    ),
    # cms_placeholder: an unfilled site-builder scaffold LEADING the page (a
    # careers-landing template the employer never customized with real content).
    (
        "Find a Job That Suits Your Passion. News from Acme Corp. "
        "Your engaging subtitle goes here. Provide a brief description or "
        "engaging subtitle that captures the essence of your content. " * 3,
        "Senior Compensation Analyst",
        JD_OFFSITE,
    ),
    # no_search_results: a "zero results for your search" careers-search page.
    (
        "Senior Data Analyst job at Acme Corp | Acme Careers "
        "There are no jobs for your search criteria. Maybe you would like "
        "to consider the categories below: Sales, Marketing, IT. " * 3,
        "Senior Data Analyst",
        JD_OFFSITE,
    ),
]


@pytest.mark.parametrize("jd, title, reason", _REJECTS)
def test_reject_signals(jd, title, reason):
    res = classify_jd_content(jd, title, "Acme Corp")
    assert res.verdict is JdVerdict.REJECT
    assert res.reason == reason


# ---------------------------------------------------------------------------
# CLEAN — shape + title grounding + substantial
# ---------------------------------------------------------------------------


def test_clean_real_jd():
    res = classify_jd_content(_REAL_JD, "Senior Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.CLEAN
    assert res.reason is None


# ---------------------------------------------------------------------------
# AMBIGUOUS — the LLM-adjudication middle
# ---------------------------------------------------------------------------


def test_ambiguous_real_jd_without_headings():
    # Grounded + substantial but NO standard JD-shape heading -> needs the LLM.
    body = (
        "Bachelor's degree in Statistics or a related quantitative field. 8 years "
        "using analytics to solve product problems. You will partner with data "
        "science teams to ship measurement frameworks. " * 4
    )
    res = classify_jd_content(body, "Senior Product Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.AMBIGUOUS


def test_ambiguous_short_jd():
    body = "We are looking for a Data Scientist. Responsibilities include modeling."
    res = classify_jd_content(body, "Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.AMBIGUOUS  # has shape+grounding but too short


def test_ambiguous_grounded_but_no_shape():
    # A company-marketing body that DOES mention the title's tokens (so it is not
    # a zero-overlap REJECT) but carries no JD-shape heading -> the LLM decides.
    body = (
        "About Acme. Acme builds data platforms for the enterprise. Our data "
        "tooling is best in class and our platform scales globally. We value a "
        "data-driven culture and bold engineers. " * 5
    )
    res = classify_jd_content(body, "Data Platform Engineer", "Acme")
    assert res.verdict is JdVerdict.AMBIGUOUS


def test_company_grounded_zero_title_overlap_rejects():
    # Precedence: even when the company name is present, a substantial body that
    # shares ZERO of the TITLE's stems is the wrong page -> REJECT (title_zero_overlap).
    body = (
        "About Catalent. Catalent is a trusted global partner. Our requirements "
        "for partnership are rigorous. Catalent delivers for patients worldwide. " * 5
    )
    res = classify_jd_content(body, "Quantum Photonics Researcher", "Catalent")
    assert res.verdict is JdVerdict.REJECT
    assert res.reason == JD_OFFSITE


# ---------------------------------------------------------------------------
# Over-fire guards — these MUST NOT be REJECTed (the false-positive regression)
# ---------------------------------------------------------------------------

_MUST_NOT_REJECT = [
    # Real JD that mentions cloudflare / javascript / cookies DEEP in the body.
    (
        _REAL_JD + " Our stack uses Cloudflare and requires JavaScript; we set cookies.",
        "Senior Data Scientist",
    ),
    # Built-In company stat "404 Total Employees" must not trip the 404 signal.
    (
        "Acme Corp. We are looking for a Senior Data Scientist. Responsibilities "
        "include modeling. The company has 404 Total Employees and is growing. " * 3,
        "Senior Data Scientist",
    ),
    # Benign "no longer" phrasing must not trip the expired signal.
    (
        "We are looking for a Data Scientist. Candidates no longer need a PhD. "
        "Responsibilities include building models and analysis. " * 3,
        "Data Scientist",
    ),
    # Widget-noise-in-real-JD: a complete, legitimate JD that happens to carry
    # an unfilled CMS placeholder block from an unrelated "related content"
    # widget FAR into the page (past the head window) must not be REJECTed —
    # confirmed on a live corpus row where this exact scaffold text trails a
    # complete, already-scored real JD. Signal must stay head-scoped.
    (
        _REAL_JD + " Watch the video. Loading... Widget title goes here. "
        "Your engaging subtitle goes here. Provide a brief description or "
        "engaging subtitle that captures the essence of your content.",
        "Senior Data Scientist",
    ),
    # "No jobs to display" WITHOUT "your search" — a related-jobs widget on an
    # otherwise real JD's own page saying that unrelated widget is empty. Must
    # not be conflated with a "no results for your search" page even when it
    # leads the body (the phrase-narrowness guard, independent of position).
    (
        "Featured jobs. Jobs List is hidden because there are no jobs to display. " + _REAL_JD,
        "Senior Data Scientist",
    ),
]


@pytest.mark.parametrize("jd, title", _MUST_NOT_REJECT)
def test_no_overfire(jd, title):
    res = classify_jd_content(jd, title, "Acme Corp")
    assert res.verdict is not JdVerdict.REJECT


# ---------------------------------------------------------------------------
# jd_content_reject — content-only (no title) vs title-dependent
# ---------------------------------------------------------------------------


def test_reject_content_only_without_title():
    wiki = "From Wikipedia, the free encyclopedia. City in California. " * 8
    rej = jd_content_reject(wiki)  # no title
    assert rej is not None and rej[0] == JD_OFFSITE


def test_zero_overlap_requires_title():
    # An off-topic body with NO title given cannot fire the title cross-check.
    body = "Our company values: passion, integrity, teamwork, and excellence. " * 8
    assert jd_content_reject(body) is None  # no title -> no zero-overlap signal


def test_empty_jd_is_not_rejected():
    assert jd_content_reject(None) is None
    assert jd_content_reject("") is None


# ---------------------------------------------------------------------------
# ParsedJob.from_job ingest gate
# ---------------------------------------------------------------------------


def _from_job(title, jd_full=None):
    from jobcannon.engine.models import Job
    from jobcannon.engine.parsed_job import ParsedJob

    job = Job(
        title=title, company="Acme Corp", location="", source="careers_page", source_url="http://x"
    )
    return ParsedJob.from_job(job, source_meta={"jd_full": jd_full})


def test_from_job_quarantines_offsite_jd():
    wiki = "From Wikipedia, the free encyclopedia. City in California. " * 8
    p = _from_job("Senior Data Scientist", jd_full=wiki)
    assert JD_OFFSITE in p.unresolved_reasons
    assert p.jd_full is None


def test_from_job_keeps_clean_jd():
    p = _from_job("Senior Data Scientist", jd_full=_REAL_JD)
    assert JD_OFFSITE not in p.unresolved_reasons
    assert JD_EXPIRED not in p.unresolved_reasons
    assert p.jd_full is not None

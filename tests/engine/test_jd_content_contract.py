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

import json

import pytest

from jobcannon.engine.jd_content_contract import (
    JD_CONTENT_REASON_CODES,
    JD_CONTENT_VERSION,
    JD_EXPIRED,
    JD_OFFSITE,
    JD_TRUNCATED,
    JdVerdict,
    _company_stems,
    _is_jd_truncated,
    _is_json_config_blob,
    classify_jd_content,
    get_jd_full_thresholds,
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
    # Above the min-length floor but below the substantial CLEAN bar: still AMBIGUOUS.
    body = ("We are looking for a Data Scientist. Responsibilities include modeling. " * 4).strip()
    res = classify_jd_content(body, "Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.AMBIGUOUS  # has shape+grounding but not substantial


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
# Issue #1813 — wrong-employer cross-listing contamination.
#
# A genuine, well-formed JD for a DIFFERENT employer is JD-shaped, substantial,
# and (because generic DS/analytics titles collide across employers) title-
# grounded, so it scored CLEAN and was scored by production as if it were this
# listing's JD. The company-absent counter-signal downgrades such bodies from
# CLEAN to AMBIGUOUS (signal ``company_absent``) for LLM adjudication — never a
# hard REJECT. Fixtures are redacted paraphrases preserving the title-overlap +
# company-absence shape of the observed audit-dispute rows.
# ---------------------------------------------------------------------------

# Highgate Hotels listing whose jd_full is a Northrop Grumman requisition
# (Falls Church VA, Secret clearance, Finance AI & Analytics). Title stems
# (data / scientist / finance / analytics) all appear in the Northrop body, so
# title grounding succeeds; the Highgate company stems do not.
_HIGHGATE_NORTHROP_BODY = (
    "Data Scientist - Finance AI & Analytics at Northrop Grumman, Falls Church VA. "
    "Responsibilities: build finance AI & analytics models, run experiments, "
    "partner with the finance analytics team on forecasting tooling. "
    "Qualifications: active Secret clearance, 5+ years Python and SQL, strong "
    "statistics background. What you'll do: design data pipelines, ship models "
    "to production, mentor analysts on analytics tooling. Minimum "
    "qualifications: degree in a quantitative field. Northrop Grumman is an "
    "equal opportunity employer. "
) * 2

# Randstad listing whose jd_full mixes in Indra Group "AI Security/GenAI
# Specialist" boilerplate. Title stems (data / scientist) appear; "randstad" does not.
_RANDSTAD_INDRA_BODY = (
    "Data Scientist AI. Indra Group is hiring an AI Security / GenAI Specialist. "
    "Responsibilities: develop generative AI security tooling, evaluate LLM "
    "guardrails, run adversarial model reviews. Qualifications: experience with "
    "GenAI, Python, model risk frameworks. What you'll do: design AI security "
    "pipelines, ship detection models, mentor analysts on secure AI. Minimum "
    "qualifications: degree in CS or a related field. Indra Group careers. "
) * 2

# Jobtailor listing whose jd_full describes a Senior Data Scientist (genAI) role
# at Lingaro, Warsaw PL, PLN salary. Title stems (data / scientist / generative
# / nlp) appear; "jobtailor" does not.
_JOBTAILOR_LINGARO_BODY = (
    "Senior Data Scientist (genAI) at Lingaro, Warsaw PL. Salary: 18000 PLN per "
    "month. Responsibilities: build generative AI and NLP pipelines, fine-tune "
    "LLMs, ship NLP models to production. Qualifications: 4+ years Python, NLP, "
    "transformers. What you'll do: design NLP pipelines, mentor data scientists, "
    "partner with product on generative AI. Minimum qualifications: MS in CS. "
    "Lingaro is hiring across its Warsaw data science practice. "
) * 2

# Google listing whose jd_full is TELUS Digital site-navigation/menu text. Title
# stems (data / scientist / discover) appear; "google" does not.
_GOOGLE_TELUS_BODY = (
    "Data Scientist, Discover. TELUS Digital navigation menu. Discover our data "
    "science opportunities and analyst roles. Responsibilities: analyze data, "
    "build scientist-grade models, partner with teams. Qualifications: Python, "
    "SQL, statistics. What you'll do: discover insights, ship models, mentor. "
    "Minimum qualifications: degree in a quantitative field. TELUS Digital "
    "careers portal. "
) * 2


_CONTAMINATED_ROWS = [
    (
        "highgate",
        _HIGHGATE_NORTHROP_BODY,
        "Data Scientist - Finance AI & Analytics",
        "Highgate Hotels Corporate Office TX",
    ),
    ("randstad", _RANDSTAD_INDRA_BODY, "Data Scientist AI", "Randstad"),
    (
        "jobtailor",
        _JOBTAILOR_LINGARO_BODY,
        "Principal Data Scientist: Generative AI & NLP Lead",
        "Jobtailor",
    ),
    ("google", _GOOGLE_TELUS_BODY, "Data Scientist, Discover", "Google"),
]


@pytest.mark.parametrize("label, body, title, company", _CONTAMINATED_ROWS)
def test_wrong_employer_contamination_is_not_clean(label, body, title, company):
    """Issue #1813: a wrong-employer body that is JD-shaped, substantial, and
    title-grounded must NOT pass CLEAN — it is downgraded to AMBIGUOUS with the
    ``company_absent`` signal for LLM adjudication.

    Each fixture preserves the title-overlap + company-absence shape of an
    observed audit-dispute row (Highgate/Northrop Grumman, Randstad/Indra
    Group, Jobtailor/Lingaro, Google/TELUS Digital).
    """
    res = classify_jd_content(body, title, company)
    assert res.verdict is not JdVerdict.CLEAN, label
    assert res.verdict is JdVerdict.AMBIGUOUS, label
    assert res.signal == "company_absent", label
    # Non-destructive: AMBIGUOUS, not REJECT — the row is adjudicated, not
    # quarantined or dropped at the storage gate.
    assert res.reason is None, label


def test_company_absent_is_not_reject_at_storage_gate():
    """Issue #1813: the company-absent downgrade is AMBIGUOUS-only — the cheap
    storage/ingest gate (``jd_content_reject``) must NOT gain a new REJECT path.
    A wrong-employer body that ``classify_jd_content`` routes to AMBIGUOUS is
    still accepted by ``jd_content_reject`` (returns None).
    """
    assert (
        jd_content_reject(_HIGHGATE_NORTHROP_BODY, "Data Scientist - Finance AI & Analytics")
        is None
    )


def test_clean_jd_naming_its_company_stays_clean():
    """Issue #1813 positive control: a CLEAN-eligible body that DOES mention a
    distinctive company stem stays CLEAN (the counter-signal only fires on
    absence). ``_REAL_JD`` opens with "Senior Data Scientist at Acme."
    """
    res = classify_jd_content(_REAL_JD, "Senior Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.CLEAN
    assert res.signal == "shape+grounded"


def test_legit_jd_without_company_name_routed_ambiguous_non_destructive():
    """Issue #1813 counter-test: a legitimate, well-formed posting whose body
    never spells out the company name is downgraded to AMBIGUOUS (the accepted
    tradeoff of the cheap deterministic half) — but NON-destructively: it is
    adjudicated by the LLM, not REJECTed, quarantined, or dropped.

    The body uses "our analytics team" / "the company" and never names "Acme",
    so the company-absent check fires even though this is a genuine posting.
    """
    body = (
        "We are looking for a Senior Data Scientist to join our analytics team. "
        "Responsibilities include building machine learning models, running "
        "experiments, and partnering with product. Qualifications: 5+ years of "
        "experience with Python and SQL, strong statistics background. What "
        "you'll do: design data pipelines, ship models to production, mentor "
        "analysts. Minimum qualifications: degree in a quantitative field. The "
        "company offers competitive benefits and a remote-first culture. "
    ) * 2
    res = classify_jd_content(body, "Senior Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.AMBIGUOUS
    assert res.signal == "company_absent"
    # Non-destructive: no quarantine reason code, so the storage gate keeps the
    # body and the adjudicator (not the resweep) owns the tie-break.
    assert res.reason is None
    assert jd_content_reject(body, "Senior Data Scientist") is None


def test_generic_company_name_skips_company_absent_check():
    """Issue #1813: a company name with no distinctive stem (e.g. "The Corporate
    Group") cannot support an absence assertion, so the check is skipped and the
    row stays eligible for CLEAN on title-grounding + shape + length alone.
    """
    body = (
        "Senior Data Scientist. We are looking for a Senior Data Scientist to "
        "join our analytics team. Responsibilities include building machine "
        "learning models, running experiments, and partnering with product. "
        "Qualifications: 5+ years of Python and SQL, strong statistics. What "
        "you'll do: design data pipelines, ship models to production, mentor "
        "analysts. Minimum qualifications: degree in a quantitative field. "
    ) * 2
    res = classify_jd_content(body, "Senior Data Scientist", "The Corporate Group")
    assert res.verdict is JdVerdict.CLEAN


# ---------------------------------------------------------------------------
# Issue #1892 — company_absent guard bypassed for companies whose distinctive
# token is under 3 characters (AT&T, C3). The #1813 guard reuses the title
# tokenizer's 3-char floor (``_SIGNIFICANT_TOKEN_RE = [a-z0-9]{3,}``), so a
# brand like AT&T yields NO distinctive stem and the guard takes its skip
# branch — CLEAN no matter whose requisition is in the body. The fix gives
# the company-absence check its own stem derivation (alphanumeric runs >= 2 +
# a punctuation-stripped acronym) and word-boundary matching for short stems.
# ---------------------------------------------------------------------------


def test_company_stems_att_nonempty():
    """``_company_stems("AT&T")`` must yield a non-empty stem list.

    The 3-char title tokenizer floor drops ``at`` (2) and ``t`` (1), so the
    pre-#1892 ``_company_stems`` returned ``[]`` and the guard was skipped
    forever. The punctuation-stripped acronym ``att`` restores a
    distinctive stem.
    """
    stems = _company_stems("AT&T")
    assert stems, f"expected non-empty stems for AT&T, got {stems}"
    assert "att" in stems


def test_company_stems_c3_includes_c3_token():
    """``_company_stems("C3 IoT")`` must include a stem derived from ``c3``.

    The 3-char floor dropped ``c3`` (2 chars), leaving only the weak generic
    ``iot``; the acronym ``c3iot`` is additionally emitted.
    """
    stems = _company_stems("C3 IoT")
    assert "c3" in stems
    assert "iot" in stems
    assert "c3iot" in stems


def test_att_listing_with_rrd_body_is_ambiguous_company_absent():
    """Issue #1892 observed row: ``at&t|lead data analyst: compensation
    insights & automation`` whose body is RRD's marketing-mix/attribution
    posting. The body is JD-shaped, substantial, and title-grounded (data /
    analyst / compensation / insights / automation stems all appear), so it
    passed every CLEAN gate while being about a different employer. With the
    company-specific stem derivation, ``AT&T`` now yields the ``att`` acronym
    stem, which the RRD body does not mention -> AMBIGUOUS (company_absent).
    """
    body = (
        "Lead Data Analyst at RRD. We are looking for a Lead Data Analyst to "
        "join our marketing-mix and attribution analytics team. "
        "Responsibilities include building compensation insights models, "
        "running automation experiments, and partnering with product. "
        "Qualifications: 5+ years of Python and SQL, strong statistics "
        "background. What you'll do: design data pipelines, ship models to "
        "production, mentor analysts on compensation insights and automation. "
        "Minimum qualifications: degree in a quantitative field. RRD is an "
        "equal opportunity employer. RRD offers competitive benefits. "
    ) * 2
    res = classify_jd_content(body, "Lead Data Analyst: Compensation Insights & Automation", "AT&T")
    assert res.verdict is JdVerdict.AMBIGUOUS
    assert res.signal == "company_absent"
    assert res.reason is None  # non-destructive: AMBIGUOUS, not REJECT


def test_c3_iot_listing_with_dwelly_body_is_ambiguous_company_absent():
    """Issue #1892 observed row: ``c3 iot|data scientist/senior data
    scientist`` whose body is entirely about Dwelly, a UK proptech Senior
    DS-Growth role. The body is JD-shaped, substantial, and title-grounded
    (data / scientist stems appear), so it passed CLEAN while naming a
    different employer.

    The #1813 guard did not catch it because ``_company_stems("C3 IoT")``
    dropped ``c3`` (2 chars) and left only the weak generic ``iot``; the
    Dwelly body mentions ``Patriot Square`` (a London landmark), so the
    pre-#1892 *unanchored* ``iot in body_lower`` substring test matched
    ``patriot`` and asserted company presence — the promiscuous-substring
    bypass the issue documents. The fix's word-boundary matching for short
    stems (``\\biot\\b`` does not match ``patriot``) plus the new ``c3`` stem
    (which the Dwelly body never mentions) restores the absence assertion ->
    AMBIGUOUS (company_absent).
    """
    body = (
        "Senior Data Scientist, Growth at Dwelly. We are looking for a Senior "
        "Data Scientist to join our growth analytics team by Patriot Square in "
        "London. Responsibilities include building data scientist models, "
        "running experiments, and partnering with product. Qualifications: 5+ "
        "years of Python and SQL, strong statistics background. What you'll "
        "do: design data pipelines, ship models to production, mentor "
        "analysts. Minimum qualifications: degree in a quantitative field. "
        "Dwelly is a UK proptech company. Dwelly offers equity. "
    ) * 2
    res = classify_jd_content(body, "Data Scientist/Senior Data Scientist", "C3 IoT")
    assert res.verdict is JdVerdict.AMBIGUOUS
    assert res.signal == "company_absent"
    assert res.reason is None


def test_genuine_att_posting_stays_clean():
    """Issue #1892 counter-test: a genuine AT&T posting whose body is AT&T's
    own requisition still classifies CLEAN. The body writes the brand with
    its original punctuation (``AT&T``), which the acronym matcher
    (separator-tolerant, ``\\ba[^a-z0-9]?t[^a-z0-9]?t\\b``) matches against
    the stem ``att`` -> company present -> no ``company_absent`` downgrade.
    """
    body = (
        "Lead Data Analyst at AT&T. We are looking for a Lead Data Analyst to "
        "join our compensation insights & automation team. Responsibilities "
        "include building compensation insights models, running automation "
        "experiments, and partnering with product. Qualifications: 5+ years "
        "of Python and SQL, strong statistics background. What you'll do: "
        "design data pipelines, ship models to production, mentor analysts on "
        "compensation insights and automation. Minimum qualifications: degree "
        "in a quantitative field. AT&T is an equal opportunity employer. "
        "AT&T offers competitive benefits. "
    ) * 2
    res = classify_jd_content(body, "Lead Data Analyst: Compensation Insights & Automation", "AT&T")
    assert res.verdict is JdVerdict.CLEAN
    assert res.signal == "shape+grounded"


def test_genuine_c3_iot_posting_stays_clean():
    """Issue #1892 counter-test: a genuine C3 IoT posting still classifies
    CLEAN. The body mentions ``C3 IoT`` (-> ``c3`` word-boundary match), so
    company presence is confirmed and no ``company_absent`` downgrade fires.
    """
    body = (
        "Data Scientist at C3 IoT. We are looking for a Data Scientist to "
        "join our analytics team. Responsibilities include building machine "
        "learning models, running experiments, and partnering with product. "
        "Qualifications: 5+ years of Python and SQL, strong statistics "
        "background. What you'll do: design data pipelines, ship models to "
        "production, mentor analysts. Minimum qualifications: degree in a "
        "quantitative field. C3 IoT is an equal opportunity employer. "
    ) * 2
    res = classify_jd_content(body, "Data Scientist/Senior Data Scientist", "C3 IoT")
    assert res.verdict is JdVerdict.CLEAN
    assert res.signal == "shape+grounded"


def test_iot_inc_with_patriot_body_is_company_absent():
    """Issue #1892 counter-test: company ``IOT Inc`` with a body that says
    ``patriot`` but never ``IOT`` is treated as company-ABSENT
    (word-boundary matching), not present. The pre-#1892 unanchored
    ``iot in body_lower`` substring test matched ``patriot`` (``iot`` inside
    ``patriot``) and let the wrong-employer body through as CLEAN.
    """
    body = (
        "Senior Data Scientist. We are looking for a Senior Data Scientist to "
        "join our patriot analytics team. Responsibilities include building "
        "machine learning models, running experiments, and partnering with "
        "product. Qualifications: 5+ years of Python and SQL, strong "
        "statistics. What you'll do: design data pipelines, ship models to "
        "production, mentor analysts. Minimum qualifications: degree in a "
        "quantitative field. "
    ) * 2
    res = classify_jd_content(body, "Senior Data Scientist", "IOT Inc")
    assert res.verdict is JdVerdict.AMBIGUOUS
    assert res.signal == "company_absent"
    assert res.reason is None


def test_company_absent_still_not_reject_at_storage_gate_after_1892():
    """Issue #1892: the REJECT set gains no new member and no write-time
    rejection is introduced at the storage gate. Both observed wrong-employer
    bodies are AMBIGUOUS-only — ``jd_content_reject`` (the storage/ingest
    gate) returns None for them.
    """
    rrd_body = (
        "Lead Data Analyst at RRD. We are looking for a Lead Data Analyst to "
        "join our marketing-mix and attribution analytics team. "
        "Responsibilities include building compensation insights models. "
        "Qualifications: 5+ years of Python and SQL. What you'll do: design "
        "data pipelines, ship models to production. Minimum qualifications: "
        "degree in a quantitative field. "
    ) * 2
    assert (
        jd_content_reject(rrd_body, "Lead Data Analyst: Compensation Insights & Automation") is None
    )


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
# Version watermark + reason-code registry (the re-sweep contract surface)
# ---------------------------------------------------------------------------


def test_jd_content_version_is_9():
    # LITERAL pin: the F7 port of company_absent (#1813), short-token company
    # stems (#1892), and AMBIGUOUS-widening signals (#1814) bumps the
    # watermark 5 -> 8 (see the module docstring's numbering-divergence note
    # for why this port does not reuse private's own 5/6/6 integers). L-0004's
    # empty_requirements_header AMBIGUOUS-widening signal (#1952) bumps it
    # 8 -> 9. A drift here silently re-arms (or fails to re-arm) the corpus
    # re-sweep.
    assert JD_CONTENT_VERSION == 9


def test_reason_code_registry_set_equality():
    # Pinned against string literals, not the module's own constants, so a
    # renamed/retyped code cannot self-consistently pass.
    assert JD_CONTENT_REASON_CODES == frozenset(
        {"jd_full_offsite", "jd_full_expired", "jd_full_truncated"}
    )
    assert JD_TRUNCATED == "jd_full_truncated"


# ---------------------------------------------------------------------------
# Truncation gate: too_short + trailing_ellipsis
# ---------------------------------------------------------------------------


def test_truncated_snippet_rejected():
    """A long snippet ending in '...' is rejected as truncated."""
    snippet = "A" * 227 + "..."  # 230 chars
    rej = jd_content_reject(snippet)
    assert rej is not None
    assert rej[0] == JD_TRUNCATED
    assert rej[1] == "trailing_ellipsis"


def test_too_short_body_rejected():
    """A body below the min-length floor is rejected."""
    rej = jd_content_reject("Short body.")
    assert rej is not None
    assert rej[0] == JD_TRUNCATED
    assert rej[1] == "too_short"


def test_is_jd_truncated_signals():
    assert _is_jd_truncated("tiny") == ("jd_full_truncated", "too_short")
    assert _is_jd_truncated("B" * 250 + "…") == ("jd_full_truncated", "trailing_ellipsis")
    # A healthy body (above the floor, no trailing ellipsis) is falsy.
    assert not _is_jd_truncated("C" * 250)
    assert not _is_jd_truncated(None)
    assert not _is_jd_truncated("")


def test_truncated_check_runs_first():
    # Order discriminator: a SHORT body that also leads with a head-block
    # marker must attribute to the truncation gate, which the private
    # contract runs before every content signal.
    rej = jd_content_reject("Request denied.")
    assert rej == ("jd_full_truncated", "too_short")


def test_get_jd_full_thresholds_defaults_and_overrides():
    assert get_jd_full_thresholds(None) == (200, True)
    assert get_jd_full_thresholds({}) == (200, True)
    cfg = {"enrichment": {"jd_full": {"min_chars": 50, "reject_trailing_ellipsis": False}}}
    assert get_jd_full_thresholds(cfg) == (50, False)
    # A nonsensical floor falls back to the default rather than disabling the gate.
    assert get_jd_full_thresholds({"enrichment": {"jd_full": {"min_chars": 0}}}) == (200, True)


def test_config_threads_through_reject_and_classify():
    # The config parameter must actually govern the gate (wired, not decorative):
    # lowering min_chars admits a body the default floor rejects, in BOTH
    # public entry points.
    body = "We are looking for a Data Scientist. Responsibilities include modeling."
    assert jd_content_reject(body)[0] == JD_TRUNCATED  # default floor: rejected
    permissive = {"enrichment": {"jd_full": {"min_chars": 10}}}
    assert jd_content_reject(body, None, permissive) is None
    assert classify_jd_content(body, "Data Scientist", "Acme", permissive).verdict is not (
        JdVerdict.REJECT
    )
    # And disabling the ellipsis signal admits a trailing-ellipsis body.
    snippet = "A" * 227 + "..."
    assert jd_content_reject(snippet)[1] == "trailing_ellipsis"
    no_ellipsis = {"enrichment": {"jd_full": {"reject_trailing_ellipsis": False}}}
    assert jd_content_reject(snippet, None, no_ellipsis) is None


# ---------------------------------------------------------------------------
# JSON config blob
# ---------------------------------------------------------------------------

# Eightfold/Netflix micro-site config blob: a large JSON object with theme,
# fonts, supported locales, and an empty ``job_description``. The blob is above
# the low_signal length threshold but carries no prose, so it must be rejected
# at the content-contract extraction gate.
_NETFLIX_EIGHTFOLD_BLOB = json.dumps(
    {
        "theme": {
            "primary_color": "#E50914",
            "secondary_color": "#221F1F",
            "logo_url": "https://assets.nflxext.com/us/en/nf/logo.png",
            "appearance": "dark",
            "custom_css_enabled": True,
        },
        "fonts": ["Netflix Sans", "Helvetica Neue", "Arial", "sans-serif"],
        "supported_locales": [
            "en-US",
            "es-US",
            "pt-BR",
            "fr-FR",
            "de-DE",
            "it-IT",
            "ja-JP",
            "ko-KR",
            "zh-CN",
            "hi-IN",
            "ar-AE",
            "tr-TR",
            "pl-PL",
            "nl-NL",
            "sv-SE",
            "da-DK",
            "fi-FI",
            "no-NO",
            "cs-CZ",
            "hu-HU",
            "ro-RO",
            "sk-SK",
            "hr-HR",
            "sl-SI",
            "bg-BG",
            "ru-RU",
            "uk-UA",
            "he-IL",
            "th-TH",
            "vi-VN",
            "id-ID",
            "ms-MY",
        ],
        "company_name": "Netflix",
        "job_title": "Data Scientist 4",
        "job_description": "",
        "apply_url": "https://explore.jobs.netflix.net/careers/job/40875/apply",
        "metadata": {
            "id": 40875,
            "department": "Data & Insights",
            "location": "USA - Remote",
            "employment_type": "full-time",
        },
        "css_rules": [
            ".nf-hero { background: #E50914; color: #fff; }",
            ".nf-button { border-radius: 4px; font-weight: 600; }",
            ".nf-job-card { padding: 24px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
            ".nf-search-bar { margin-bottom: 16px; }",
            ".nf-footer { font-size: 12px; color: #666; }",
        ],
    },
    separators=(",", ":"),
)


def test_netflix_eightfold_config_blob_rejected():
    """A long Eightfold/Netflix config blob must be REJECT, not CLEAN.

    The payload clears the length gate but contains no job description prose,
    so it cannot be scored.
    """
    res = classify_jd_content(_NETFLIX_EIGHTFOLD_BLOB, "Data Scientist 4", "Netflix")
    assert res.verdict is JdVerdict.REJECT
    assert res.reason == JD_OFFSITE
    assert res.signal == "json_config_blob"


def test_json_blob_with_real_description_not_rejected():
    """A JSON object with a non-empty prose description is not a config blob."""
    body = json.dumps(
        {
            "job_title": "Senior Data Scientist",
            "job_description": (
                "Senior Data Scientist at Acme. We are looking for a Senior Data "
                "Scientist to join our analytics team. Responsibilities include "
                "building machine learning models, running experiments, and "
                "partnering with product. Qualifications: 5+ years of Python and SQL. "
                "What you'll do: design data pipelines, ship models to production, "
                "mentor analysts."
            ),
        }
    )
    assert not _is_json_config_blob(body)
    assert jd_content_reject(body) is None


# ---------------------------------------------------------------------------
# JSON-blob escape-hatch vocabulary widening (issue #37, re-ported from the
# private source). The blob detector's description-key escape hatch only recognized
# snake_case job_description/description; these pin the widened vocabulary
# (camelCase, a dict-valued description, the generic `content` key, and a
# bare array wrapping a JSON-LD-style posting object).
# ---------------------------------------------------------------------------


def test_json_blob_with_camelcase_description_not_rejected():
    """camelCase jobDescription is recognized, not just snake_case."""
    body = json.dumps({"jobTitle": "Senior Data Scientist", "jobDescription": _REAL_JD})
    res = classify_jd_content(body, "Senior Data Scientist", "Acme")
    assert res.verdict is not JdVerdict.REJECT


def test_json_blob_with_dict_valued_description_not_rejected():
    """A description field whose value is itself a nested object (some ATS
    payloads wrap raw/html variants) is still recognized as real prose."""
    body = json.dumps(
        {
            "job_title": "Senior Data Scientist",
            "description": {"raw": _REAL_JD, "html": "<p>...</p>"},
        }
    )
    res = classify_jd_content(body, "Senior Data Scientist", "Acme")
    assert res.verdict is not JdVerdict.REJECT


def test_json_blob_with_content_key_not_rejected():
    """The generic `content` key (some ATS micro-sites use it instead of
    description/job_description) is recognized too."""
    body = json.dumps({"job_title": "Senior Data Scientist", "content": _REAL_JD})
    res = classify_jd_content(body, "Senior Data Scientist", "Acme")
    assert res.verdict is not JdVerdict.REJECT


def test_json_blob_bare_array_with_nested_description_not_rejected():
    """A leading bare array wrapping a JSON-LD-style posting object, whose
    element carries real prose under a description key, is not a config
    blob even though the top-level JSON value is a list."""
    body = json.dumps(
        [{"@type": "JobPosting", "title": "Senior Data Scientist", "description": _REAL_JD}]
    )
    res = classify_jd_content(body, "Senior Data Scientist", "Acme")
    assert res.verdict is not JdVerdict.REJECT


def test_json_blob_bare_array_without_prose_still_rejected():
    """Counter-test: a bare array with no nested description/content prose
    (a genuine config array) stays REJECT -- the widened escape hatch must
    not swallow real junk."""
    body = json.dumps([{"id": i, "code": f"locale-{i:03d}"} for i in range(30)])
    res = classify_jd_content(body, "Senior Data Scientist", "Acme")
    assert res.verdict is JdVerdict.REJECT
    assert res.signal == "json_config_blob"


def test_json_blob_short_format_tag_under_description_still_rejected():
    """Counter-test: a description-like key whose value is short filler (a
    format tag, not prose) must not trip the widened escape hatch. Without a
    substantiality floor on the matched leaf, this config blob would flip
    from REJECT to a scorable verdict on the strength of the word "html"."""
    padding = {"font": "Helvetica", "locale": "en-US", "palette": ["#111", "#222", "#333"] * 60}
    body = json.dumps(
        {
            "theme": padding,
            "job_title": "Data Scientist 4",
            "description": {"format": "html", "text": ""},
        }
    )
    res = classify_jd_content(body, "Data Scientist 4", "Netflix")
    assert res.verdict is JdVerdict.REJECT
    assert res.signal == "json_config_blob"


def test_json_blob_short_content_label_still_rejected():
    """Counter-test: the generic `content` alias must require substantial
    text too -- a short section label (`"header": "Careers"`) must not read
    as real prose just because it sits under the `content` key."""
    padding = {"font": "Helvetica", "locale": "en-US", "palette": ["#111", "#222", "#333"] * 60}
    body = json.dumps(
        {
            "theme": padding,
            "job_title": "Data Scientist 4",
            "content": {"header": "Careers", "footer": "(c) Acme"},
        }
    )
    res = classify_jd_content(body, "Data Scientist 4", "Acme")
    assert res.verdict is JdVerdict.REJECT
    assert res.signal == "json_config_blob"


def test_json_blob_short_direct_string_description_now_rejected():
    """Pins the *tightening* half of the substantiality-floor change: before
    the floor, ANY non-empty string directly under `description` -- even a
    one-word value like "N/A" -- escaped blob classification entirely
    (`if isinstance(val, str) and val.strip(): return False`). With the
    40-char `_PROSE_LEAF_MIN_CHARS` floor, that same short direct-string
    value (not nested under another key, unlike the format-tag/label
    counter-tests above) no longer clears the escape hatch, so this body
    -- long enough to pass the `too_short` gate on padding alone -- flips
    from a pre-existing fall-through to a deterministic REJECT
    `json_config_blob` at the write gate."""
    padding = {"font": "Helvetica", "locale": "en-US", "palette": ["#111", "#222", "#333"] * 20}
    body = json.dumps(
        {
            "theme": padding,
            "job_title": "Data Scientist 4",
            "description": "See attached PDF",  # 17 chars: non-empty, under the 40-char floor
        }
    )
    assert len(body) >= 200  # clears the too_short gate independent of the blob check
    res = classify_jd_content(body, "Data Scientist 4", "Acme")
    assert res.verdict is JdVerdict.REJECT
    assert res.signal == "json_config_blob"


def test_prose_containing_braces_is_not_a_blob():
    # A real JD that merely CONTAINS an inline JSON/code block must not be
    # mistaken for a serialized config payload.
    body = _REAL_JD + ' Example payload: {"model": "gpt", "temperature": 0.2}.'
    assert not _is_json_config_blob(body)
    assert jd_content_reject(body, "Senior Data Scientist") is None


def test_expired_marker_wins_over_blob_shape():
    # Order discriminator: the json-blob check runs AFTER the expired signal,
    # so a JSON payload whose text carries a dead-posting sentence attributes
    # to JD_EXPIRED (matching the private signal order).
    payload = json.dumps({"message": "This position has been filled. " * 12, "job_description": ""})
    rej = jd_content_reject(payload)
    assert rej is not None
    assert rej[0] == JD_EXPIRED


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


def test_from_job_ingest_gate_reads_runtime_config():
    # L3 wiring pin: the ingest gate passes the host-injected runtime config
    # into jd_content_reject. With a provider demanding an absurd floor, a
    # body that is CLEAN under defaults must be quarantined as truncated —
    # deleting the config argument at the parsed_job call site kills this.
    from jobcannon.engine.runtime_config import set_config_provider

    set_config_provider(lambda: {"enrichment": {"jd_full": {"min_chars": 10_000}}})
    try:
        p = _from_job("Senior Data Scientist", jd_full=_REAL_JD)
        assert JD_TRUNCATED in p.unresolved_reasons
        assert p.jd_full is None
    finally:
        set_config_provider(None)
    # And with the provider cleared, the same body is stored again.
    p = _from_job("Senior Data Scientist", jd_full=_REAL_JD)
    assert p.jd_full is not None


# ---------------------------------------------------------------------------
# has_recognizable_jd_shape — additive public wrapper (1B Wave 2 PR 7)
# ---------------------------------------------------------------------------


def test_has_recognizable_jd_shape_matches_classifier_vocabulary():
    from jobcannon.engine.jd_content_contract import has_recognizable_jd_shape

    assert has_recognizable_jd_shape("Responsibilities: build things. Qualifications: 5y.")
    assert not has_recognizable_jd_shape("hello world, nothing job-shaped here")


def test_has_recognizable_jd_shape_none_and_empty_return_false():
    from jobcannon.engine.jd_content_contract import has_recognizable_jd_shape

    assert has_recognizable_jd_shape(None) is False
    assert has_recognizable_jd_shape("") is False


# ---------------------------------------------------------------------------
# Mutation-review pins (B3 review round): boundary + dominance-guard killers.
# Adopted from the test-quality refuter's sabotage-verified proposals.
# ---------------------------------------------------------------------------


def test_truncation_floor_boundary():
    """Bidirectional boundary pin at the default floor: 199 rejects, 200 does
    not. Kills both the off-by-one (`<` -> `<=`) and floor-shift mutants that
    the accessor-literal test alone leaves green."""
    assert _is_jd_truncated("x" * 199) == ("jd_full_truncated", "too_short")
    assert _is_jd_truncated("x" * 200) is None


def test_json_blob_must_dominate_body():
    """A leading JSON-LD header followed by a real JD is NOT a config blob —
    the dominance criterion is load-bearing (deleting it rejects common
    extractor output that leads with a schema.org JobPosting header)."""
    header = json.dumps({"@type": "JobPosting", "employmentType": "FULL_TIME"})
    body = header + "\n\n" + _REAL_JD
    assert _is_json_config_blob(body) is False
    assert jd_content_reject(body) is None


def test_leading_array_blob_rejected():
    """A dominating leading ARRAY payload is a blob — pins the documented
    `[` half of _JSON_START_CHARS."""
    payload = json.dumps([{"locale": "en-US", "theme": "dark"}] * 40)
    assert _is_json_config_blob(payload) is True


def test_malformed_config_degrades_to_defaults():
    """String / None section AND leaf values must not crash the gate at the
    chokepoint (issue #37): a null jd_full section degrades to defaults, a
    numeric string coerces, a null `enrichment` section degrades (previously
    raised AttributeError), a non-numeric min_chars degrades (previously
    raised ValueError), a None min_chars degrades (previously raised
    TypeError), and a non-dict jd_full section degrades."""
    assert get_jd_full_thresholds({"enrichment": {"jd_full": None}}) == (200, True)
    assert get_jd_full_thresholds({"enrichment": {"jd_full": {"min_chars": "50"}}}) == (50, True)
    assert get_jd_full_thresholds({"enrichment": None}) == (200, True)
    assert get_jd_full_thresholds({"enrichment": {"jd_full": {"min_chars": "abc"}}}) == (200, True)
    assert get_jd_full_thresholds({"enrichment": {"jd_full": {"min_chars": None}}}) == (200, True)
    assert get_jd_full_thresholds({"enrichment": {"jd_full": "yes"}}) == (200, True)


@pytest.mark.parametrize(
    "cfg",
    [
        {"enrichment": "not-a-dict"},
        {"enrichment": []},
        "not-a-dict",
        [],
    ],
    ids=[
        "enrichment_scalar",
        "enrichment_list",
        "config_scalar",
        "config_list",
    ],
)
def test_get_jd_full_thresholds_degrades_on_malformed_top_level_shape(cfg):
    """Extends test_malformed_config_degrades_to_defaults (issue #37) to the
    isinstance-guard levels the private source's re-port added on top of
    the leaf/null-section fix: a *truthy* non-dict `enrichment` value (not
    just None -- a bare `or {}` only substitutes on falsy values, so a stray
    scalar would otherwise reach `.get()` on a non-dict and raise
    AttributeError), a list-valued `enrichment`, and a non-dict `config`
    itself (scalar or list) all degrade to the documented defaults instead of
    raising."""
    assert get_jd_full_thresholds(cfg) == (200, True)


@pytest.mark.parametrize("bad_min_chars", [[], {}])
def test_get_jd_full_thresholds_degrades_on_container_min_chars(bad_min_chars):
    """A list/dict-valued min_chars leaf (int() raises TypeError on these, the
    same as the string/None cases test_malformed_config_degrades_to_defaults
    already pins) also degrades to the default."""
    cfg = {"enrichment": {"jd_full": {"min_chars": bad_min_chars}}}
    min_chars, _ = get_jd_full_thresholds(cfg)
    assert min_chars == 200


def test_malformed_config_degrades_at_reject_chokepoint():
    """The same malformed config shapes must not crash jd_content_reject
    itself — the actual per-row gate hit by set_jd_full and
    ParsedJob.from_job (issue #37's damage claim was at those chokepoints,
    not just the resolver in isolation)."""
    body = "x" * 250  # clears the default 200-char floor; no other reject signal
    assert jd_content_reject(body, None, {"enrichment": None}) is None
    assert jd_content_reject(body, None, {"enrichment": {"jd_full": {"min_chars": "abc"}}}) is None


def test_classify_jd_content_survives_null_enrichment_section():
    """The full 3-way verdict (not just the write-gate reject check) must
    also survive a null `enrichment` section -- a YAML section whose
    children are all commented out parses to None, so {"enrichment": None}
    is a realistic config shape."""
    cfg = {"enrichment": None}
    res = classify_jd_content(_REAL_JD, "Senior Data Scientist", "Acme Corp", cfg)
    assert res.verdict is JdVerdict.CLEAN


# ---------------------------------------------------------------------------
# Issue #1814 — AMBIGUOUS-widening signals (non-leading JSON config blob,
# listing-index "results" phrasing / whole-body, career-explainer/SEO page).
#
# These widen the contract's AMBIGUOUS lane so non-posting captures that the
# tight REJECT set lets through (and that shape+grounded+substantial would
# otherwise CLEAN) are routed to the LLM adjudicator instead of the scorer.
# They add NO REJECT member and NO write-time rejection: every one of these
# bodies is AMBIGUOUS, never REJECT.
# ---------------------------------------------------------------------------

# A Netflix/Eightfold config blob preceded by a short markdown heading + nav
# wrapper (issue #1814: the production capture was "entirely scraped
# page-theme/config JSON ... with job_description field empty"). The leading
# blob is REJECT (test_netflix_eightfold_config_blob_rejected); the NON-leading
# blob is the AMBIGUOUS-widening case.
_NETFLIX_NON_LEADING_BLOB = (
    "# Senior Data Scientist 5\n\nExplore Jobs\n\n" + _NETFLIX_EIGHTFOLD_BLOB
)

# A Randstad-style career-explainer/SEO page. It carries JD-shape headings
# ("Responsibilities", "Qualifications") so it would CLEAN on shape+grounded
# +substantial alone, AND the explainer topic + aggregate-salary markers that
# the widening signal keys on. Reused for the three Randstad rows (the title
# supplies the explainer marker for two of them; the body supplies it for the
# bare-title ``randstad|data scientist`` row).
_RANDSTAD_EXPLAINER_BODY = (
    "What is a data scientist? Data scientists analyze data to help companies "
    "make decisions. Responsibilities include building models, cleaning data, "
    "and communicating findings. Qualifications: Python, SQL, statistics, and "
    "a quantitative degree. The national average salary for a data scientist "
    "is $108,020 per year according to the BLS; the median salary in the "
    "United States varies by region. Career path: data analyst to data "
    "scientist to senior data scientist. This profile page summarizes the "
    "role for job seekers. "
) * 3

# A Capital One / Eightfold-style search-results listing captured as a posting
# (issue #1814: "50,048 results" plus dozens of unrelated postings). The
# ``<count> results`` header is the structural tell; the body also mentions
# the title's stems so it is NOT a title_zero_overlap REJECT.
_CAPITAL_ONE_LISTING_BODY = (
    "50,048 results for data scientist. Senior Data Scientist at Capital One. "
    "Research Scientist at Lab Corp. Machine Learning Engineer at Tech Co. "
    "Data Analyst at Fin Corp. Professor of Computer Science at State "
    "University. Business Analyst at Globex. Software Engineer at Initech. "
    "Quantitative Analyst at Hedge Co. "
) * 4

# A Visa Hunt-style job-board listing page (issue #1814: "dozens of unrelated
# postings (professors, teachers, engineers, analysts across countries)").
_VISA_HUNT_LISTING_BODY = (
    "1,239 results. Staff Data Scientist at Visa. Professor of Physics at MIT. "
    "High School Teacher at West High. Mechanical Engineer at Build Co. "
    "Financial Analyst at Money Corp. Data Scientist at National Bank. "
    "Civil Engineer at City Works. Operations Analyst at Logi Corp. "
) * 4


# The six named rows from the 2026-08-22 / 2026-08-21 audit disputes, each
# pinned as AMBIGUOUS (non-CLEAN, non-REJECT) — routed to the LLM adjudicator,
# not the scorer and not silently dropped.
_ISSUE_1814_ROWS = [
    (
        "netflix|senior data scientist 5",
        _NETFLIX_NON_LEADING_BLOB,
        "senior data scientist 5",
        "json_config_blob_unanchored",
    ),
    (
        "randstad usa|data scientist profile page",
        _RANDSTAD_EXPLAINER_BODY,
        "data scientist profile page",
        "career_explainer_seo",
    ),
    (
        "randstad usa|salary of a data scientist",
        _RANDSTAD_EXPLAINER_BODY,
        "salary of a data scientist",
        "career_explainer_seo",
    ),
    (
        "randstad|data scientist",
        _RANDSTAD_EXPLAINER_BODY,
        "data scientist",
        "career_explainer_seo",
    ),
    (
        "capital one|senior data scientist — ml for customer outcomes",
        _CAPITAL_ONE_LISTING_BODY,
        "senior data scientist — ml for customer outcomes",
        "listing_index",
    ),
    (
        "visa hunt|staff data scientist",
        _VISA_HUNT_LISTING_BODY,
        "staff data scientist",
        "listing_index",
    ),
]


@pytest.mark.parametrize("dedup_key, jd, title, signal", _ISSUE_1814_ROWS)
def test_issue_1814_named_rows_routed_ambiguous(dedup_key, jd, title, signal):
    """Issue #1814: each named audit-dispute row is AMBIGUOUS, not CLEAN.

    The widening signals override the shape+grounded+substantial CLEAN test so
    the body is adjudicated by the LLM tie-breaker instead of scored as a real
    requisition. None of these are REJECT — the tight REJECT set is unchanged.
    """
    res = classify_jd_content(jd, title, "Employer")
    assert res.verdict is JdVerdict.AMBIGUOUS, dedup_key
    assert res.verdict is not JdVerdict.CLEAN, dedup_key
    assert res.signal == signal, dedup_key


def test_issue_1814_ambiguous_lane_reachable_end_to_end():
    """Issue #1814 acceptance: the AMBIGUOUS lane is reachable end-to-end.

    A representative non-posting body that the deterministic REJECT set lets
    through is classified AMBIGUOUS (adjudicated, not silently dropped and not
    scored as CLEAN). This is the contract-level reachability check — the LLM
    adjudicator wiring itself is outside this port's manifest (nightly_monitor
    / adjudicator are web-tier, DIES).
    """
    res = classify_jd_content(
        _CAPITAL_ONE_LISTING_BODY,
        "senior data scientist — ml for customer outcomes",
        "Capital One",
    )
    assert res.verdict is JdVerdict.AMBIGUOUS
    assert res.signal == "listing_index"


# ---------------------------------------------------------------------------
# Issue #1814 — counter-tests (the widening signals must NOT over-fire on
# legitimate JDs).
# ---------------------------------------------------------------------------


def test_issue_1814_real_jd_with_inline_json_block_not_flagged():
    """A real JD that contains an inline JSON block (prose dominates) is not
    flagged by the un-anchored json_config_blob signal — the JSON value is a
    small minority of the body, so the dominance check fails."""
    body = _REAL_JD + ' {"widget": "related_content", "jobs": []}'
    res = classify_jd_content(body, "Senior Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.CLEAN


def test_issue_1814_real_jd_with_headcount_figure_not_flagged():
    """A real JD that merely mentions a headcount figure (no "results" /
    "jobs in" header) is not flagged by the listing-index signal."""
    body = _REAL_JD + " We have 50,048 employees across 20 offices worldwide."
    res = classify_jd_content(body, "Senior Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.CLEAN


def test_issue_1814_explainer_marker_without_salary_not_flagged():
    """A body with an explainer topic marker but NO aggregate-salary marker
    must not trip the career-explainer signal (single-marker guard)."""
    body = (
        "Senior Data Scientist at Acme. Responsibilities include building "
        "models. Qualifications: Python, SQL. What is a data scientist's "
        "typical day? Our team answers that across many projects. What you'll "
        "do: ship models to production and mentor analysts. "
    ) * 3
    res = classify_jd_content(body, "Senior Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.CLEAN


def test_issue_1814_salary_marker_without_explainer_not_flagged():
    """A body with an aggregate-salary marker but NO explainer topic marker
    must not trip the career-explainer signal (single-marker guard)."""
    body = _REAL_JD + " Note: the national average for this role is above market."
    res = classify_jd_content(body, "Senior Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.CLEAN


def test_issue_1814_benign_career_path_plus_national_average_is_ambiguous():
    """Compound-but-benign co-occurrence counter-test (review finding).

    Two ordinary, unrelated competitive-comp phrases that BOTH match the
    widening regexes — 'clear career path for growth' (``\\bcareer\\s+path\\b``)
    and 'pay above the national average' (``\\bnational\\s+average\\b``) — in an
    otherwise normal, JD-shaped, title-grounded, company-named body. The
    existing single-marker counter-tests only prove that ONE marker alone does
    not trip; this exercises the co-occurrence the signal keys on.

    Resulting verdict: AMBIGUOUS (signal ``career_explainer_seo``). This is
    ACCEPTABLE and non-destructive — it is the same tradeoff the
    ``company_absent`` counter-signal (#1813) already makes
    (``test_legit_jd_without_company_name_routed_ambiguous_non_destructive``):
    a cheap deterministic half routes an uncertain body to the LLM
    adjudicator, which confirms a genuine posting, rather than scoring it
    blind or REJECTing it. It is NOT REJECT (no quarantine reason code), and
    ``jd_content_reject`` returns None so the storage gate keeps the body.
    The cost is one background adjudicator LLM call, not a lost or mis-scored
    row.
    """
    body = _REAL_JD + (
        " We offer a clear career path for growth and pay above the national average for this role."
    )
    res = classify_jd_content(body, "Senior Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.AMBIGUOUS
    assert res.signal == "career_explainer_seo"
    # Non-destructive: no quarantine reason code; the storage gate keeps it.
    assert res.reason is None
    assert jd_content_reject(body, "Senior Data Scientist") is None


def test_issue_1814_benign_footer_listing_chrome_beyond_head_is_ambiguous():
    """Whole-body listing-count counter-test (review finding).

    ``_LISTING_COUNT_RE`` was originally calibrated against the corpus as
    head-only (the ``jd_content_reject`` path checks the first ``_HEAD_WINDOW``
    chars). The widening path (``_ambiguous_widening_signal``) evaluates it
    against the WHOLE body. This counter-test exercises an otherwise-legitimate
    JD that carries footer/nav chrome an ATS/careers page commonly appends
    beyond the head window — 'Browse 1,000+ jobs in your area' — which matches
    ``\\b\\d[\\d,]{0,4}\\+?\\s+[\\w\\s,&/+.\\-]{0,40}?\\bjobs\\s+in\\b``.

    The footer phrase is placed at the END of a ~990-char body, well beyond
    ``_HEAD_WINDOW`` (400), so the head-only REJECT path does not fire and
    only the whole-body widening path can catch it.

    Resulting verdict: AMBIGUOUS (signal ``listing_index``). This is ACCEPTABLE
    and non-destructive — same tradeoff as
    ``test_issue_1814_benign_career_path_plus_national_average_is_ambiguous``
    and the ``company_absent`` signal: the LLM adjudicator confirms a genuine
    posting. It is NOT REJECT (no quarantine reason code), and
    ``jd_content_reject`` returns None so the storage gate keeps the body.
    """
    from jobcannon.engine.jd_content_contract import _HEAD_WINDOW

    footer = " Browse 1,000+ jobs in your area at Acme Careers."
    body = _REAL_JD + footer
    # Guard: the footer chrome is genuinely beyond the head-only REJECT window
    # so this exercises the whole-body widening path, not the head REJECT.
    assert body.find("1,000+ jobs in") >= _HEAD_WINDOW
    res = classify_jd_content(body, "Senior Data Scientist", "Acme Corp")
    assert res.verdict is JdVerdict.AMBIGUOUS
    assert res.signal == "listing_index"
    # Non-destructive: no quarantine reason code; the storage gate keeps it.
    assert res.reason is None
    assert jd_content_reject(body, "Senior Data Scientist") is None


def test_issue_1814_no_new_write_time_rejection():
    """Issue #1814 acceptance: the widening signals introduce no new
    write-time rejection. ``jd_content_reject`` (the set_jd_full gate) returns
    None for every one of the named non-posting bodies — they are AMBIGUOUS at
    the classify layer, not REJECT at the write gate."""
    for _dedup_key, jd, title, _signal in _ISSUE_1814_ROWS:
        assert jd_content_reject(jd, title) is None, _dedup_key

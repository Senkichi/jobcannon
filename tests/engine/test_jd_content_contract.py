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


def test_jd_content_version_is_4():
    # LITERAL pin: the resync lands the private contract's truncation +
    # json-blob rules, whose watermark is 4. A drift here silently re-arms
    # (or fails to re-arm) the whole-corpus re-sweep.
    assert JD_CONTENT_VERSION == 4


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
    """String / None LEAF values must not crash the gate at the chokepoint:
    a null jd_full section degrades to defaults, a numeric string coerces.
    (A null `enrichment` section still raises — upstream-identical resolver
    shape, tracked as a follow-up issue, parity-bound.)"""
    assert get_jd_full_thresholds({"enrichment": {"jd_full": None}}) == (200, True)
    assert get_jd_full_thresholds({"enrichment": {"jd_full": {"min_chars": "50"}}}) == (50, True)

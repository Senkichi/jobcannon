"""
Tests for universal title-filter enforcement via ParsedJob.from_job (Phase 48.01).

Acceptance criteria from issue #52:
- ParsedJob.from_job calls is_metadata_blob + clean_title for every caller.
- Fixtures from each of the three documented callsites yield UnresolvedParsedJob
  with 'title_metadata_blob' reason.
- Shim path: a Job with a metadata-blob title passed to upsert_job produces
  UpsertResult.unresolved_reasons containing 'title_metadata_blob'.

Callsite shapes exercised:
  1. AI-nav tier  — UNDP-style labeled-form blob (phrase markers).
  2. careers_scraper.py:322/:602 — Blue State paren-close shape (I-08 regex).
  3. _static_tier.py — req-ID pipe pattern (_REQ_ID_PIPE_RE in is_metadata_blob).
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import patch

from jobcannon.engine.models import Job
from jobcannon.engine.parsed_job import ParsedJob, UnresolvedParsedJob

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(title: str, source: str = "careers_crawl") -> Job:
    return Job(
        title=title,
        company="Test Corp",
        location="New York, NY",
        source=source,
        source_url="https://example.com/jobs/1",
        source_id="",
    )


@contextlib.contextmanager
def _clean_patches():
    """Disable I-10 (company denylist) so other validators can be exercised."""
    with (
        patch("jobcannon.engine.parsed_job.load_config", return_value={}),
        patch("jobcannon.engine.parsed_job.get_company_denylist", return_value=frozenset()),
    ):
        yield


# ---------------------------------------------------------------------------
# Three-source blob fixtures
# ---------------------------------------------------------------------------


class TestTitleFilterUniversalBlobs:
    """Phase 48.01: is_metadata_blob + clean_title run in from_job for all callers.

    Each test stands in for one of the three callsite paths that previously
    bypassed the static-tier title filter.
    """

    def test_ai_nav_tier_style_metadata_blob(self):
        """AI-nav tier extraction style: UNDP-style labeled-form blob.

        The AI navigator renders JavaScript-heavy pages; some aggregate sites
        concatenate title + metadata labels as inline text without separators.
        The 'job title', 'post level', and 'apply by' phrase markers in
        _is_metadata_blob catch this shape.
        """
        # Simulates UNDP-style careers page where field labels run into the title.
        blob_title = "Job TitleSenior Software EngineerPost levelNPSA-9Apply byApr-29-26"
        job = _make_job(blob_title, source="careers_crawl")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob), (
            f"Expected UnresolvedParsedJob for {blob_title!r}, got {type(result).__name__}"
        )
        assert "title_metadata_blob" in result.unresolved_reasons
        assert result.raw_title == blob_title

    def test_careers_scraper_style_metadata_blob(self):
        """careers_scraper.py:322/:602 extraction style: Blue State paren-close shape.

        The careers_page low-tier path produces ')XX' patterns when inline
        title + location text is concatenated without separator whitespace.
        I-08 (_TITLE_LOCATION_BLEED_RE) catches this shape.  Runs on raw_title
        before clean_title strips the state-code suffix via _NOSEP_TRAIL_LOC_RE.
        """
        # Blue State ')XX' shape: title closes with ')' then bare state code.
        blob_title = "Senior Software Engineer)NY"
        job = _make_job(blob_title, source="careers_page")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob), (
            f"Expected UnresolvedParsedJob for {blob_title!r}, got {type(result).__name__}"
        )
        assert "title_metadata_blob" in result.unresolved_reasons
        assert result.raw_title == blob_title

    def test_static_tier_style_metadata_blob(self):
        """_static_tier.py extraction style: req-ID pipe pattern.

        Aggregator-style careers pages concatenate title + req-ID + location
        without separators; the _REQ_ID_PIPE_RE in is_metadata_blob catches
        the 'digits|TitleCase' shape (e.g. 'SQL2354308|Chennai, Tamil Nadu').
        Runs on raw_title before clean_title strips _REQID_PREFIX_RE patterns.
        """
        # Workday/aggregator req-ID pipe shape: digits run + pipe + city name.
        blob_title = "Senior Data Scientist - GenAI SQL2354308|Chennai, Tamil Nadu"
        job = _make_job(blob_title, source="workday")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob), (
            f"Expected UnresolvedParsedJob for {blob_title!r}, got {type(result).__name__}"
        )
        assert "title_metadata_blob" in result.unresolved_reasons

    # -----------------------------------------------------------------------
    # Positive case: clean title must not be flagged
    # -----------------------------------------------------------------------

    def test_clean_title_returns_parsed_job(self):
        """A well-formed title produces a clean ParsedJob."""
        job = _make_job("Senior Software Engineer")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert not isinstance(result, UnresolvedParsedJob)
        assert "title_metadata_blob" not in result.unresolved_reasons
        assert result.title == "Senior Software Engineer"

    def test_clean_title_with_parenthetical_passes(self):
        """Titles with legitimate parentheticals are not falsely flagged."""
        job = _make_job("Software Engineer (Backend)")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, ParsedJob)
        assert "title_metadata_blob" not in result.unresolved_reasons

    # -----------------------------------------------------------------------
    # clean_title normalisation
    # -----------------------------------------------------------------------

    def test_clean_title_strips_nosep_state_code(self):
        """clean_title removes no-separator trailing state codes from the stored title."""
        # "EngineerNY" shape — _NOSEP_TRAIL_LOC_RE strips "NY"
        job = _make_job("Senior Software EngineerNY")
        with _clean_patches():
            result = ParsedJob.from_job(job)
        # The blob flag should fire (I-08 sees raw_title ← "EngineerNY" matches)
        # but we also verify the stored title is cleaned.
        # "EngineerNY" → I-08 pattern 1 checks raw: ")NY" — but there's no ")",
        # so I-08 won't fire.  is_metadata_blob also won't fire (no markers).
        # clean_title strips "NY" via _NOSEP_TRAIL_LOC_RE.
        assert isinstance(result, ParsedJob)
        assert result.title == "Senior Software Engineer"

    def test_raw_title_preserved_on_unresolved(self):
        """UnresolvedParsedJob.raw_title carries the original pre-clean title."""
        blob_title = "Job TitlePrincipal EngineerPost levelP5"
        job = _make_job(blob_title)
        with _clean_patches():
            result = ParsedJob.from_job(job)
        assert isinstance(result, UnresolvedParsedJob)
        assert result.raw_title == blob_title


# ---------------------------------------------------------------------------
# Caller boundary: Job → ParsedJob.from_job → upsert_job → unresolved_reasons
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# WI-10 (#1834): order-insensitive title matching (TITLE_MATCH_VERSION 2)
# ---------------------------------------------------------------------------
import json
from pathlib import Path

import pytest

from jobcannon.engine.ats_platforms import (
    TITLE_MATCH_VERSION,
    _title_matches,
)

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "title_match_golden.json"


def _load_golden() -> dict:
    with open(_GOLDEN_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def test_title_match_version_is_two():
    """The order-insensitive fallback is the active tier-3 semantics."""
    assert TITLE_MATCH_VERSION == 2


def test_golden_corpus():
    """Every golden row's `_title_matches` verdict equals its mechanical label.

    Labels were produced by the WI-10 rubric (order-free token-set subset of
    any target title, after `_normalize_title`, exclusions empty) applied to
    live `companies.last_scan_postings_json`. The fixture is self-contained:
    it carries its own `target_titles`/`exclusions`, so the test does not read
    live config and is stable in CI. Asserting the real public entry point
    (`_title_matches`, the same one `_registry.py`/`__init__.py` call) pins
    that the shipped code implements the rubric across all three tiers.
    """
    golden = _load_golden()
    targets = golden["target_titles"]
    exclusions = golden["exclusions"]
    assert targets, "fixture must carry target_titles"

    mismatches = []
    for row in golden["rows"]:
        want = row["expected"] == "MATCH"
        got = _title_matches(row["title"], targets, exclusions)
        if got != want:
            mismatches.append((row["title"], row["expected"], got))

    assert not mismatches, (
        f"{len(mismatches)}/{len(golden['rows'])} golden rows disagree with "
        f"_title_matches. First 10: {mismatches[:10]}"
    )


# (title, target_titles, expected) — the four REPORT D-4 examples asserted
# against the live profile list, plus explicit order-scramble tripwires.
# Under the mechanical token-set rubric only the Airbnb reorder is rescued;
# the other three are *seniority-coverage* misses (staff vs senior/lead, or no
# profile subset at all), NOT order misses — out of scope for WI-10.
_ORDER_INSENSITIVE_CASES = [
    # Airbnb: reversed + qualifier-inserted. OLD ordered matcher rejects
    # (needs "analytics" before "lead"); NEW subset accepts. Sabotage tripwire.
    ("Lead, Advanced Analytics", ["Analytics Lead"], True),
    # Okta: "Staff Product Analyst" — no profile is a subset ("Lead Product
    # Analyst" needs "lead"). Seniority-coverage miss, stays NO_MATCH.
    ("Staff Product Analyst", None, False),
    # Upstart: "Staff Data Analyst" — "Senior/Lead Data Analyst" need
    # senior/lead. Seniority-coverage miss, stays NO_MATCH.
    ("Staff Data Analyst", None, False),
    # Affirm: "Analytics Engineer II" — no profile is {analytics, engineer}.
    # Stays NO_MATCH (would need a profile-coverage change, not a matcher one).
    ("Analytics Engineer II", None, False),
    # Second independent order tripwire: reversed "Senior Analyst".
    ("Analyst Senior, TPO Operations", ["Senior Analyst"], True),
    # Negative control: subset requires ALL tokens — "scientist" absent.
    ("Senior Data Platform", ["Data Scientist"], False),
]


@pytest.mark.parametrize("title, targets, expected", _ORDER_INSENSITIVE_CASES)
def test_order_insensitive_examples(title, targets, expected):
    """The four REPORT D-4 examples + order-scramble tripwires.

    `targets=None` uses the live profile list from the golden fixture; an
    explicit list isolates the order-insensitivity for a single phrase.
    """
    if targets is None:
        targets = _load_golden()["target_titles"]
    assert _title_matches(title, targets, []) is expected


# ---------------------------------------------------------------------------
# #1861: all-caps abbreviation expansion must be case-sensitive
# ---------------------------------------------------------------------------
# A lowercased "em" is the Portuguese preposition "in"; case-insensitive
# \bEM\b expansion injected a spurious "manager" token, making
# "Especialista em Data Analytics" token-set-match "Analytics Manager".
# The same class of false positive hit "da" (Da Nang city / Italian "from")
# and "ds" (Polish "do spraw" = regarding). All-caps abbreviations now
# expand only when the original token is uppercase.
_ALL_CAPS_CASE_SENSITIVE_CASES = [
    # The issue's primary case: Portuguese "em" must NOT expand.
    ("Especialista em Data Analytics", ["Analytics Manager"], False),
    # Da Nang (Vietnamese city) — "Da" must NOT expand to "Data Analyst".
    ("Senior Engineer - Da Nang", ["Senior Data Analyst"], False),
    # Polish "ds." (do spraw = regarding) — "DS" must NOT expand.
    ("Doradca ds. Produktów", ["Data Scientist"], False),
    # Positive control: uppercase EM still expands to "engineering manager".
    ("EM, Platform Team", ["Engineering Manager"], True),
    # Positive control: uppercase DA still expands to "data analyst".
    ("DA, Marketing", ["Data Analyst"], True),
    # Positive control: uppercase DS still expands to "data scientist".
    ("DS, Growth", ["Data Scientist"], True),
    # Mixed-case "Em" (sentence-start) must NOT expand — only all-caps
    # is unambiguous recruiter shorthand.
    ("Em Data Analytics We Trust", ["Analytics Manager"], False),
]


@pytest.mark.parametrize("title, targets, expected", _ALL_CAPS_CASE_SENSITIVE_CASES)
def test_all_caps_abbreviations_case_sensitive(title, targets, expected):
    """All-caps abbreviations expand only when the original token is uppercase (#1861).

    A lowercased ``em``/``da``/``ds`` is a non-English word, not recruiter
    shorthand — expanding it injects spurious tokens ("manager", "data
    analyst") that produce false-positive token-set matches.
    """
    assert _title_matches(title, targets, []) is expected

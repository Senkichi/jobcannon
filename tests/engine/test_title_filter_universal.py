"""
Tests for universal title-filter enforcement via ParsedJob.from_job (Phase 48.01).

Acceptance criteria from private issue #52:
- ParsedJob.from_job calls is_metadata_blob + clean_title for every caller.
- Fixtures from each of the three documented callsites yield UnresolvedParsedJob
  with 'title_metadata_blob' reason.

Callsite shapes exercised:
  1. AI-nav tier  — UNDP-style labeled-form blob (phrase markers).
  2. careers_scraper.py:322/:602 — Blue State paren-close shape (I-08 regex).
  3. _static_tier.py — req-ID pipe pattern (_REQ_ID_PIPE_RE in is_metadata_blob).

Ported from the private repo's tests/test_title_filter_universal.py. The
TestCallerBoundaryMetadataBlob class (Job -> ParsedJob.from_job -> upsert_job
-> UpsertResult.unresolved_reasons) is NOT ported — upsert_job / db_migrate
are Task 3 (PR-3, ScanServices) scope, not this task's manifest. The denylist
seam is adapted: the private repo patched load_config/get_company_denylist;
the engine port drives parsed_job.set_denylist_provider instead (Step 7a).
"""

from __future__ import annotations

import contextlib

from jobcannon.engine import parsed_job as parsed_job_mod
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
    parsed_job_mod.set_denylist_provider(lambda: frozenset())
    try:
        yield
    finally:
        parsed_job_mod.set_denylist_provider(None)


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

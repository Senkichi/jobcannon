"""Tests for the result-count / category-landing tile filter.

Two layers of enforcement, one predicate:

  1. ``is_listing_tile`` predicate — unit pack: tiles match, real postings
     (including numeric-prefixed legitimate titles) do not.
  2. ``ParsedJob.from_job`` — raises ``ListingTileError`` (hard drop, I-14).

Root cause: the static crawler harvested careers-page *category landing*
links — anchor text "84 Data Scientist Jobs" — which ordered-words-matched the
target "Data Scientist" and slipped the keyword gate, then scored as a real
posting. The fix rejects the tile shape at the source boundary.

Ported from the private repo's tests/test_listing_tile_filter.py. Layer 3
(the static-tier early-drop, ``_extract_jobs_from_soup`` in
``jobcannon.engine.careers_crawler._static_tier``) landed with L-0464/
L-0469 (crawler-3, riding the L-0443 umbrella) but is exercised through its
own module's tests, not this file — this file stays scoped to the pure
predicate, unchanged. The denylist seam is adapted: the private repo
patched load_config/get_company_denylist; the engine port drives
parsed_job.set_denylist_provider instead (Step 7a).
"""

from __future__ import annotations

import contextlib

import pytest

from jobcannon.engine import parsed_job as parsed_job_mod
from jobcannon.engine.careers_crawler._title_filters import _is_listing_tile, is_listing_tile
from jobcannon.engine.models import Job
from jobcannon.engine.parsed_job import ListingTileError, ParsedJob

# ---------------------------------------------------------------------------
# Layer 1: predicate unit pack
# ---------------------------------------------------------------------------

# Real count tiles / category-landing titles — MUST match.
_TILE_TITLES = [
    "84 Data Scientist Jobs",  # the exact Capital One offender that surfaced this class
    "71 Business Analyst Jobs",
    "1,200+ openings",
    "12 results",
    "5 positions",
    "3 roles",
    "27 opportunities",
    "1 job",  # singular noun variant
    "100+ Jobs",
    "  9 Software Engineer Positions  ",  # surrounding whitespace tolerated
    "250 OPENINGS",  # case-insensitive
]

# Legitimate postings (some numeric-prefixed) — MUST NOT match.
_NON_TILE_TITLES = [
    "Data Scientist",
    "Senior Software Engineer",
    "100 Women in Finance — Analyst",  # numeric prefix, no listing-noun end
    "3D Artist",  # leading digit glued to a word, no space, no listing noun
    "5G Network Engineer",
    "Jobs Data Analyst",  # listing noun mid-string, not end-anchored
    "Director of Open Roles Strategy",  # "roles" mid-string
    "Engineer — 401k and 12 other benefits",  # number not leading
    "",  # empty
    "Lead Positions Manager",  # no leading count
]


@pytest.mark.parametrize("title", _TILE_TITLES)
def test_listing_tile_predicate_matches_tiles(title):
    assert _is_listing_tile(title) is True, f"expected tile match for {title!r}"


@pytest.mark.parametrize("title", _NON_TILE_TITLES)
def test_listing_tile_predicate_rejects_real_titles(title):
    assert _is_listing_tile(title) is False, f"expected non-match for {title!r}"


def test_public_alias_is_same_callable():
    assert is_listing_tile is _is_listing_tile


# ---------------------------------------------------------------------------
# Layer 2: ParsedJob.from_job hard-drop (I-14)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _clean_patches():
    """Disable I-10 (company denylist) so the tile validator is what fires."""
    parsed_job_mod.set_denylist_provider(lambda: frozenset())
    try:
        yield
    finally:
        parsed_job_mod.set_denylist_provider(None)


def _make_job(title: str, source: str = "careers_crawl") -> Job:
    return Job(
        title=title,
        company="Capital One",
        location="",
        source=source,
        source_url="https://www.capitalonecareers.com/category/data-science-jobs/234/24980/1",
        source_id="",
    )


def test_from_job_raises_on_listing_tile():
    job = _make_job("84 Data Scientist Jobs", source="careers_page")
    with _clean_patches(), pytest.raises(ListingTileError):
        ParsedJob.from_job(job)


def test_from_job_does_not_raise_on_real_title():
    job = _make_job("Data Scientist", source="careers_page")
    with _clean_patches():
        result = ParsedJob.from_job(job)
    assert isinstance(result, ParsedJob)
    assert result.title == "Data Scientist"


def test_from_job_does_not_raise_on_numeric_prefixed_real_title():
    job = _make_job("100 Women in Finance — Analyst")
    with _clean_patches():
        result = ParsedJob.from_job(job)
    assert isinstance(result, ParsedJob)

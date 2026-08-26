"""Tests for the careers-crawler URL-path navigation filter (`_is_nav_path`).

Ported from the private repo's tests/test_careers_crawler.py
(`test_filters_nav_segments_nested_under_portal_prefix`), which exercises
this logic indirectly through `_extract_jobs_from_soup` on the static-tier
crawler. That consumer (`job_finder.web.careers_crawler._static_tier` /
`_extract_jobs_from_soup`) is outside this task's manifest — the engine's
`careers_crawler` package is only the `_title_contract` / `_title_filters`
subpackage, not the full crawler. The port therefore targets the pure
predicate directly rather than the HTML-extraction path around it; the
assertions are the same (segment-nested nav paths are rejected, a real job
path is not).

`_is_nav_path` currently has no caller inside the public engine package —
this file exists to keep the predicate's behavior pinned (manifest sync)
ahead of a future crawler-tier port that will wire it up.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.careers_crawler._title_filters import _is_nav_path

# Segment-nested nav paths — the segment appears somewhere other than
# position 0, so only the segment-level check (not the prefix check) catches
# them. Mirrors the Randstad `/job-seeker/career-advice/...` shape.
_SEGMENT_NESTED_NAV_PATHS = [
    "/job-seeker/career-advice/resume-tips",
    "/job-seeker/insights/market-trends",
    "/careers/blog/engineer-spotlight",
    "/careers/support/",
]

# Real job-detail paths — must NOT be treated as nav, including ones that
# share a substring with a nav segment without matching a full segment.
_NON_NAV_JOB_PATHS = [
    "/jobs/real-engineer",
    "/jobs/lead-advisor-123",  # "advisor" contains "advice"-adjacent text but no full segment match
    "/careers/software-engineer",
]


@pytest.mark.parametrize("path", _SEGMENT_NESTED_NAV_PATHS)
def test_is_nav_path_matches_segment_nested_nav(path):
    assert _is_nav_path(path) is True, f"expected nav match for {path!r}"


@pytest.mark.parametrize("path", _NON_NAV_JOB_PATHS)
def test_is_nav_path_rejects_real_job_paths(path):
    assert _is_nav_path(path) is False, f"expected non-nav for {path!r}"


def test_is_nav_path_prefix_still_matches_at_position_zero():
    """Pre-existing prefix behavior (position 0) is unaffected by the
    segment-level addition."""
    assert _is_nav_path("/blog/some-article") is True
    assert _is_nav_path("/about") is True


def test_is_nav_path_search_subpath_carve_out_unaffected():
    """`/search` is a subpath-jobs prefix (ByteDance `/search/<id>` tiles),
    not a nav segment — the segment set must not contain "search", or this
    carve-out silently regresses."""
    assert _is_nav_path("/search") is True
    assert _is_nav_path("/search/12345") is False

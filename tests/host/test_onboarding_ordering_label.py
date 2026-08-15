"""Pure-function edge-case coverage for jobcannon/web/onboarding.py's
_ordering_label helper. No Postgres needed: the helper takes plain row-like
dicts and does no I/O, mirroring tests/host/test_pages.py's no-DB style.

Exists because the rendering path these tests exercise (a rank_score
present on some but not all rows, or a mixed/falsy ranker_version) is
UNREACHABLE on /preview today -- list_feed_postings' anonymous branch
hardcodes rank_score/ranker_version to NULL for every row
(jobcannon/db/_feed.py) -- so no route-level, DB-backed test could ever
drive this function through those branches. The helper is written for
reuse by a later authenticated consumer of _feed_list.html (its own
docstring says so), so its edge-case behavior needs coverage independent
of any route that happens to call it today.
"""

from __future__ import annotations

from jobcannon.web.onboarding import _ordering_label


def test_empty_row_list_is_not_personalized():
    assert _ordering_label([]) == {"personalized": False, "ranker_version": None}


def test_all_rows_unranked_is_not_personalized():
    rows = [{"rank_score": None, "ranker_version": None}]
    assert _ordering_label(rows) == {"personalized": False, "ranker_version": None}


def test_all_rows_ranked_with_a_consistent_version_is_personalized():
    rows = [
        {"rank_score": 0.9, "ranker_version": "ranker-v7"},
        {"rank_score": 0.4, "ranker_version": "ranker-v7"},
    ]
    assert _ordering_label(rows) == {"personalized": True, "ranker_version": "ranker-v7"}


def test_partially_ranked_rows_with_a_real_version_still_show_it():
    """Mirrors tests/host/test_feed_page.py's
    test_feed_reads_rank_score_and_ranker_version_from_feed_state (2 rows,
    only 1 carries a feed_state row) -- the pages.py twin already renders
    "Ranked by ranker-test-v7." for exactly this shape, so this copy must
    agree: a stricter "personalized only when every row is ranked" rule
    would make the two _ordering_label copies diverge in behavior the
    moment either one is reused, which is the opposite of what this
    function's own docstring promises ("the same logic stays correct if a
    later authenticated consumer ... ever passes ranked rows through
    it")."""
    rows = [
        {"rank_score": 0.9, "ranker_version": "ranker-test-v7"},
        {"rank_score": None, "ranker_version": None},
    ]
    assert _ordering_label(rows) == {"personalized": True, "ranker_version": "ranker-test-v7"}


def test_mixed_ranker_versions_do_not_render_ranked_by_none():
    """Before the fix: a mixed version set made `version` resolve to
    `None`, which the route still labeled personalized=True -- rendering
    the literal text "Ranked by None." in preview.html (Jinja stringifies
    None, it does not blank it)."""
    rows = [
        {"rank_score": 0.9, "ranker_version": "ranker-v7"},
        {"rank_score": 0.4, "ranker_version": "ranker-v8"},
    ]
    assert _ordering_label(rows) == {"personalized": False, "ranker_version": None}


def test_blank_ranker_version_does_not_render_ranked_by_dot():
    """The literal repro of the issue's headline bug: ranker_version is an
    unconstrained nullable `text` column, so a single, internally-consistent
    but EMPTY string is a representable value. Before the fix this slipped
    through as personalized=True / ranker_version='', which is exactly what
    made preview.html's `Ranked by {{ ordering.ranker_version }}.` render
    the bare "Ranked by ."."""
    rows = [{"rank_score": 0.9, "ranker_version": ""}]
    assert _ordering_label(rows) == {"personalized": False, "ranker_version": None}

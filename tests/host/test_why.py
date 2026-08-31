"""chip_kinds (jobcannon/web/why.py) — pure, DB-free literal restatements of
stored posting values. No Postgres needed: every input here is a plain dict
standing in for a `list_feed_postings` row / a session `pending_picker`
dict, so this module runs with no `requires_postgres` marker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jobcannon.host.structural_axes.freshness import score_freshness
from jobcannon.web.why import chip_kinds


def _row(**overrides):
    base = {
        "title": "Senior Backend Engineer",
        "salary_min": None,
        "salary_max": None,
        "structural_axes": None,
        # Trustworthy by default so existing "posted ..." expectations below
        # need no change; tests that care about the last_seen-anchored label
        # set override this explicitly.
        "posted_date_precision": "exact",
    }
    base.update(overrides)
    return base


def test_chips_are_literal_restatements_of_stored_values():
    row = _row(
        structural_axes={
            "freshness": {"value": 1.0, "method": "rules_v1"},
            "seniority_clarity": {"value": True, "method": "rules_v1"},
            "jd_quality": {"value": 0.9, "method": "rules_v1"},
            "comp_transparency": {"value": True, "method": "structured"},
        },
        salary_min=120000,
        salary_max=160000,
    )
    selections = {"titles": ["Senior Backend Engineer"], "skills": ["python"]}

    kinds = chip_kinds(row, selections)

    # Every chip traces to one specific stored field — none is a fabricated
    # judgment about fit. The freshness band comes from the stored
    # freshness value, not from re-deriving age against wall-clock time.
    assert kinds["freshness"] == "posted within the last week"
    assert kinds["seniority"] == "level stated in title"
    assert kinds["jd_quality"] == "JD looks complete"
    # There is no salary kind at all — no key for it exists.
    assert set(kinds) == {"overlap", "freshness", "seniority", "jd_quality"}
    assert kinds["overlap"].startswith("title matches your selections:")
    # No chip may claim a quality/fit judgment beyond the literal
    # restatement of what is stored.
    assert not any(
        v and ("great fit" in v.lower() or "match score" in v.lower()) for v in kinds.values()
    )


def test_no_chips_from_null_structural_axes_row_but_row_still_renders():
    row = _row(structural_axes=None, salary_min=90000)
    selections = {"titles": ["Senior Backend Engineer"]}

    kinds = chip_kinds(row, selections)

    # No axis-derived chip appears (there is nothing stored to restate) ...
    assert kinds["freshness"] is None
    assert kinds["seniority"] is None
    assert kinds["jd_quality"] is None
    # ... but overlap still resolves: axis absence does not take down the
    # title-overlap chip, which reads the row and selections directly
    # rather than through structural_axes. There is no salary kind — no
    # key for it exists.
    assert set(kinds) == {"overlap", "freshness", "seniority", "jd_quality"}
    assert kinds["overlap"].startswith("title matches your selections:")


def test_no_selections_yields_no_overlap_chip():
    row = _row()
    empty = {"overlap": None, "freshness": None, "seniority": None, "jd_quality": None}
    assert chip_kinds(row, None) == empty
    assert chip_kinds(row, {}) == empty


def test_freshness_bands_use_ge_ladder_not_equality():
    for value, expected in [
        (1.0, "posted within the last week"),
        (0.7, "posted within the last month"),
        (0.4, "posted within the last quarter"),
        (0.3, "no confirmed post date"),
        (0.2, "posting is likely over 90 days old"),
        (0.1, "listing shows signs of being stale"),
    ]:
        row = _row(
            structural_axes={"freshness": {"value": value, "method": "rules_v1"}},
            posted_date_precision="exact",
        )
        assert chip_kinds(row, {})["freshness"] == expected


def test_freshness_chip_says_last_seen_not_posted_when_precision_is_untrustworthy():
    # score_freshness falls back to last_seen whenever posted_date_precision
    # is not 'exact'/'approximate' (freshness.py's own docstring). A chip
    # claiming "posted ..." in that state would assert an origination date
    # the row does not actually carry — only what was last *observed*.
    for precision in (None, "proxy", "unknown"):
        for value, expected in [
            (1.0, "last seen within the last week"),
            (0.7, "last seen within the last month"),
            (0.4, "last seen within the last quarter"),
            (0.3, "no confirmed post date"),
            (0.2, "last seen over 90 days ago"),
            (0.1, "listing shows signs of being stale"),
        ]:
            row = _row(
                structural_axes={"freshness": {"value": value, "method": "rules_v1"}},
                posted_date_precision=precision,
            )
            assert chip_kinds(row, {})["freshness"] == expected


def test_freshness_chip_matches_real_scorer_output_for_trustworthy_precision():
    # Real coupling: drive score_freshness itself (not a value transcribed
    # from why.py) at one age per bucket boundary (freshness.py's
    # _age_bucket: <=7, <=30, <=90, else) with an 'exact' precision, then
    # assert chip_kinds renders the matching "posted ..." label for whatever
    # that call actually returned — pinning all three date-derived buckets,
    # not just one age inside the widest one.
    for days, expected in [
        (3, "posted within the last week"),
        (20, "posted within the last month"),
        (60, "posted within the last quarter"),
        (200, "posting is likely over 90 days old"),
    ]:
        posted = datetime.now(timezone.utc) - timedelta(days=days)
        axis = score_freshness(posted, "exact", None, False, None)
        row = _row(structural_axes={"freshness": axis}, posted_date_precision="exact")
        assert chip_kinds(row, {})["freshness"] == expected


def test_freshness_chip_matches_real_scorer_output_for_last_seen_fallback():
    # Same real-scorer coupling for the fallback anchor: no usable
    # posted_date, only a last_seen at one age per bucket boundary.
    for days, expected in [
        (3, "last seen within the last week"),
        (20, "last seen within the last month"),
        (60, "last seen within the last quarter"),
        (200, "last seen over 90 days ago"),
    ]:
        seen = datetime.now(timezone.utc) - timedelta(days=days)
        axis = score_freshness(None, None, seen, False, None)
        row = _row(structural_axes={"freshness": axis}, posted_date_precision=None)
        assert chip_kinds(row, {})["freshness"] == expected


def test_freshness_chip_matches_real_scorer_output_when_stale_flagged():
    # is_stale overrides the date arithmetic entirely (freshness.py:57-58);
    # the chip must reflect that override regardless of precision.
    posted = datetime.now(timezone.utc) - timedelta(days=1)
    axis = score_freshness(posted, "exact", None, True, None)
    row = _row(structural_axes={"freshness": axis}, posted_date_precision="exact")

    assert axis["value"] == 0.1
    assert chip_kinds(row, {})["freshness"] == "listing shows signs of being stale"


def test_malformed_axis_shape_does_not_raise():
    row = _row(structural_axes={"freshness": "not-a-mapping", "jd_quality": {"value": "nan"}})
    assert chip_kinds(row, {}) == {
        "overlap": None,
        "freshness": None,
        "seniority": None,
        "jd_quality": None,
    }


def test_chip_kinds_keys_stable_and_none_when_nothing_to_say():
    row = {"structural_axes": None, "posted_date_precision": None, "title": "Engineer"}
    assert chip_kinds(row, {}) == {
        "overlap": None,
        "freshness": None,
        "seniority": None,
        "jd_quality": None,
    }


def test_chip_kinds_overlap_resolves_without_axes():
    row = {"structural_axes": None, "posted_date_precision": None, "title": "Staff Engineer"}
    kinds = chip_kinds(row, {"titles": ["Staff Engineer"]})
    assert kinds["overlap"] == "title matches your selections: engineer, staff"
    assert kinds["freshness"] is None


def test_salary_never_produces_a_chip_kind():
    row = {
        "structural_axes": None,
        "posted_date_precision": None,
        "title": "Engineer",
        "salary_min": 150000,
        "salary_max": 200000,
    }
    kinds = chip_kinds(row, {})
    assert kinds == {"overlap": None, "freshness": None, "seniority": None, "jd_quality": None}
    assert "salary" not in " ".join(kinds)

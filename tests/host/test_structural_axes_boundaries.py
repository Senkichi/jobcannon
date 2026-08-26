"""Characterization tests pinning the structural-axes scoring boundaries.

See docs/design/structural-axes-verification.md for the full accounting: no
specification pins these numbers, and Phase 1C is the first consumer of them
in this repository. Pinning each boundary here means any future change to a
number is a deliberate, visible test edit, not a silent drift. This module
asserts current behavior only — it changes no scoring behavior.
"""

from __future__ import annotations

from datetime import date

from jobcannon.host.structural_axes.freshness import _age_bucket, score_freshness
from jobcannon.host.structural_axes.jd_quality import _boilerplate_ratio, score_jd_quality

# ---------------------------------------------------------------------------
# freshness: age-bucket edges (7 / 30 / 90 days, both sides)
# ---------------------------------------------------------------------------


def test_age_bucket_seven_day_edge():
    assert _age_bucket(7.0) == 1.0
    assert _age_bucket(7.0001) == 0.7


def test_age_bucket_thirty_day_edge():
    assert _age_bucket(30.0) == 0.7
    assert _age_bucket(30.0001) == 0.4


def test_age_bucket_ninety_day_edge():
    assert _age_bucket(90.0) == 0.4
    assert _age_bucket(90.0001) == 0.2


def test_freshness_no_usable_date_flat_default():
    result = score_freshness(None, None, None, False, None)
    assert result == {"value": 0.3, "method": "rules_v1"}


def test_freshness_is_stale_override_exact_value():
    # A fresh-looking posted_date is overridden entirely by is_stale=True.
    result = score_freshness(date.today(), "exact", None, True, None)
    assert result == {"value": 0.1, "method": "rules_v1"}


# ---------------------------------------------------------------------------
# jd_quality: 200 / 1200 word-count band edges
# ---------------------------------------------------------------------------


def _jd_text(word_count: int, *, with_shape: bool) -> str:
    """A body with an exact word count, optionally carrying a JD-shape hit."""
    words = ["Responsibilities"] if with_shape else ["Overview"]
    words += ["word"] * (word_count - len(words))
    return " ".join(words)


def test_jd_quality_band_lower_edge():
    in_band = score_jd_quality(_jd_text(200, with_shape=True), [])
    below_band = score_jd_quality(_jd_text(199, with_shape=True), [])
    assert in_band["value"] == 1.0
    assert below_band["value"] == 0.8


def test_jd_quality_band_upper_edge():
    in_band = score_jd_quality(_jd_text(1200, with_shape=True), [])
    above_band = score_jd_quality(_jd_text(1201, with_shape=True), [])
    assert in_band["value"] == 1.0
    assert above_band["value"] == 0.8


# ---------------------------------------------------------------------------
# jd_quality: 0.4 / 0.4 / 0.2 weighting
# ---------------------------------------------------------------------------


def test_jd_quality_weighting_no_shape_signal():
    # band=1.0, section=0.0, boiler=0.0 -> 0.4*1 + 0.4*0 + 0.2*1 = 0.6
    result = score_jd_quality(_jd_text(200, with_shape=False), [])
    assert result["value"] == 0.6


def test_jd_quality_weighting_full_boilerplate_overlap():
    # band=1.0, section=1.0, boiler=1.0 (identical sibling) -> 0.4+0.4+0 = 0.8
    text = _jd_text(200, with_shape=True)
    result = score_jd_quality(text, [text])
    assert result["value"] == 0.8


# ---------------------------------------------------------------------------
# jd_quality: no siblings -> zero boilerplate penalty
# ---------------------------------------------------------------------------


def test_boilerplate_ratio_no_siblings_is_zero():
    assert _boilerplate_ratio("Responsibilities include shipping features.", []) == 0.0

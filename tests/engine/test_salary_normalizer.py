"""Unit tests for jobcannon.engine.salary_normalizer (Data Integrity Overhaul P1.1).

Covers parse_salary_text (range/units/currency/period cues/garbage) and every rung
of the normalize_observation salvage ladder (D-3), using the real-world fixtures from
the plan's §2 diagnosis table.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.salary_normalizer import (
    MAX_PLAUSIBLE_ANNUAL,
    MIN_PLAUSIBLE_ANNUAL,
    PROVENANCE_RANK,
    NormalizedSalary,
    SalaryObservation,
    annualize,
    detect_currency,
    detect_period,
    normalize_observation,
    parse_salary_text,
    period_for_column,
)

# ---------------------------------------------------------------------------
# Module constants / public surface
# ---------------------------------------------------------------------------


def test_plausibility_bounds_are_single_source_of_truth():
    assert MIN_PLAUSIBLE_ANNUAL == 30_000
    assert MAX_PLAUSIBLE_ANNUAL == 5_000_000


def test_provenance_rank_ordering():
    assert PROVENANCE_RANK["ats_structured"] == 4
    assert PROVENANCE_RANK["jd_regex"] == 3
    assert PROVENANCE_RANK["llm_extract"] == 2
    assert PROVENANCE_RANK["email_snippet"] == PROVENANCE_RANK["feed_string"] == 1


@pytest.mark.parametrize(
    ("value", "period", "expected"),
    [
        (50.0, "hourly", 104_000.0),
        (200.0, "daily", 52_000.0),
        (2_000.0, "weekly", 104_000.0),
        (10_000.0, "monthly", 120_000.0),
        (120_000.0, "annual", 120_000.0),
        (120_000.0, "unknown", 120_000.0),
    ],
)
def test_annualize(value, period, expected):
    assert annualize(value, period) == expected


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        ("annual", "annual"),
        ("hourly", "hourly"),
        ("monthly", "monthly"),
        ("unknown", "unknown"),
        ("weekly", "unknown"),  # m081 fold
        ("daily", "unknown"),  # m081 fold
    ],
)
def test_period_for_column_folds_weekly_daily(period, expected):
    assert period_for_column(period) == expected


# ---------------------------------------------------------------------------
# parse_salary_text — currency + period cue detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$120K - $150K", "USD"),
        ("£60,000 - £80,000", "GBP"),
        ("€60,000 - €80,000", "EUR"),
        ("CA$90,000 - CA$110,000", "CAD"),
        ("C$90,000 - C$110,000", "CAD"),
        ("₹2,000,000 - ₹3,000,000", "INR"),
    ],
)
def test_detect_currency(text, expected):
    assert detect_currency(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$42 - $51 an hour", "hourly"),
        ("$42 - $51 /hr", "hourly"),
        ("$42 - $51 per hour", "hourly"),
        ("$120K - $150K per year", "annual"),
        ("$120K - $150K annually", "annual"),
        ("$8K - $10K per month", "monthly"),
        ("$2K - $3K per week", "weekly"),
        ("$400 - $600 per day", "daily"),
        ("$120K - $150K", "unknown"),
    ],
)
def test_detect_period(text, expected):
    assert detect_period(text) == expected


# ---------------------------------------------------------------------------
# parse_salary_text — parsing
# ---------------------------------------------------------------------------


def test_parse_basic_k_suffix():
    obs = parse_salary_text("$120K - $150K", provenance="jd_regex")
    assert obs is not None
    assert obs.min_value == 120_000
    assert obs.max_value == 150_000
    assert obs.period == "unknown"
    assert obs.currency == "USD"
    assert obs.provenance == "jd_regex"
    assert obs.raw_text == "$120K - $150K"


def test_parse_full_dollars_with_commas():
    obs = parse_salary_text("$150,000 - $200,000", provenance="email_snippet")
    assert obs == SalaryObservation(
        min_value=150_000,
        max_value=200_000,
        period="unknown",
        currency="USD",
        provenance="email_snippet",
        raw_text="$150,000 - $200,000",
    )


def test_parse_k_elision_both_under_1000_no_units():
    # "$120 - $150" with no K/M -> thousands at parse time only (D-3 note).
    obs = parse_salary_text("$120 - $150", provenance="feed_string")
    assert obs is not None
    assert obs.min_value == 120_000
    assert obs.max_value == 150_000


def test_parse_hourly_keeps_raw_values_and_period():
    obs = parse_salary_text("$42 - $51 an hour", provenance="jd_regex")
    assert obs is not None
    assert obs.min_value == 42
    assert obs.max_value == 51
    assert obs.period == "hourly"


def test_parse_currency_recorded():
    obs = parse_salary_text("£60,000 - £80,000", provenance="jd_regex")
    assert obs is not None
    assert obs.currency == "GBP"
    assert obs.min_value == 60_000
    assert obs.max_value == 80_000


def test_parse_m_suffix():
    obs = parse_salary_text("$1.5M - $2M", provenance="jd_regex")
    assert obs is not None
    assert obs.min_value == 1_500_000
    assert obs.max_value == 2_000_000


@pytest.mark.parametrize("text", [None, "", "competitive salary", "no numbers here"])
def test_parse_returns_none_on_no_range(text):
    assert parse_salary_text(text, provenance="feed_string") is None


def test_parse_single_value_out_of_scope():
    # Single value (no range) -> None; ambiguous min/max attribution.
    assert parse_salary_text("starting at $120K", provenance="jd_regex") is None


# ---------------------------------------------------------------------------
# normalize_observation — Rung 1 (period known)
# ---------------------------------------------------------------------------


def test_rung1_hourly_salvaged():
    # §2 S3: "$42 - $51 an hour" -> 42*2080, 51*2080.
    obs = SalaryObservation(42, 51, period="hourly", provenance="jd_regex")
    result = normalize_observation(obs)
    assert result.salary_min == 87_360
    assert result.salary_max == 106_080
    assert result.resolution == "salvaged_hourly"
    assert result.period == "hourly"


def test_rung1_monthly_salvaged():
    obs = SalaryObservation(8_000, 10_000, period="monthly", provenance="jd_regex")
    result = normalize_observation(obs)
    assert result.salary_min == 96_000
    assert result.salary_max == 120_000
    assert result.resolution == "salvaged_monthly"
    assert result.period == "monthly"


def test_rung1_weekly_annualized_column_period_unknown():
    # Weekly cue -> annualized; observation period 'weekly' but COLUMN folds to unknown.
    obs = SalaryObservation(2_000, 3_000, period="weekly", provenance="jd_regex")
    result = normalize_observation(obs)
    assert result.salary_min == 104_000
    assert result.salary_max == 156_000
    assert result.resolution == "salvaged_weekly"
    assert result.period == "unknown"  # m081 fold


def test_rung1_daily_salvaged_column_unknown():
    obs = SalaryObservation(400, 600, period="daily", provenance="jd_regex")
    result = normalize_observation(obs)
    assert result.salary_min == 104_000
    assert result.salary_max == 156_000
    assert result.resolution == "salvaged_daily"
    assert result.period == "unknown"


def test_rung1_known_period_out_of_bounds_implausible():
    # Hourly $1/hr -> $2080/yr, below floor even after honest conversion.
    obs = SalaryObservation(1, 2, period="hourly", provenance="jd_regex")
    result = normalize_observation(obs)
    assert result.salary_min is None
    assert result.salary_max is None
    assert result.resolution == "implausible"


# ---------------------------------------------------------------------------
# normalize_observation — Rung 2 (period unknown, in window)
# ---------------------------------------------------------------------------


def test_rung2_plausible_annual_ok():
    # "$120K - $150K" parsed -> unknown period, both in window -> assume annual.
    obs = parse_salary_text("$120K - $150K", provenance="jd_regex")
    assert obs is not None
    result = normalize_observation(obs)
    assert result.salary_min == 120_000
    assert result.salary_max == 150_000
    assert result.resolution == "ok"
    assert result.period == "unknown"


def test_rung2_gbp_currency_preserved():
    obs = parse_salary_text("£60,000 - £80,000", provenance="jd_regex")
    assert obs is not None
    result = normalize_observation(obs)
    assert result.salary_min == 60_000
    assert result.salary_max == 80_000
    assert result.currency == "GBP"
    assert result.resolution == "ok"


def test_rung2_k_elision_end_to_end():
    obs = parse_salary_text("$120 - $150", provenance="feed_string")
    assert obs is not None
    result = normalize_observation(obs)
    assert result.salary_min == 120_000
    assert result.salary_max == 150_000
    assert result.resolution == "ok"


# ---------------------------------------------------------------------------
# normalize_observation — Rung 3 (corroborated cents, ats_structured only)
# ---------------------------------------------------------------------------


def test_rung3_greenhouse_cents_salvaged():
    # §2 S1: Northbeam Greenhouse cents (17M/20M raw) -> $170k/$200k.
    obs = SalaryObservation(17_000_000, 20_000_000, period="unknown", provenance="ats_structured")
    result = normalize_observation(obs)
    assert result.salary_min == 170_000
    assert result.salary_max == 200_000
    assert result.resolution == "salvaged_cents"


def test_rung3_cents_only_for_ats_structured():
    # Same values, jd_regex provenance -> NOT salvaged (load-bearing restriction).
    obs = SalaryObservation(17_000_000, 20_000_000, period="unknown", provenance="jd_regex")
    result = normalize_observation(obs)
    assert result.salary_min is None
    assert result.salary_max is None
    assert result.resolution == "implausible"


def test_rung3_funding_numbers_stay_quarantined():
    # "Series B funding: $10M - $50M" parsed as feed_string text must NOT mint salary.
    obs = parse_salary_text("Series B funding: $10M - $50M", provenance="feed_string")
    assert obs is not None
    result = normalize_observation(obs)
    assert result.salary_min is None
    assert result.salary_max is None
    assert result.resolution == "implausible"


def test_rung3_cents_requires_both_sides_in_bounds_after_div():
    # 17M÷100 = 170k (ok) but 2M÷100 = 20k (below floor) -> not corroborated.
    obs = SalaryObservation(17_000_000, 2_000_000, period="unknown", provenance="ats_structured")
    result = normalize_observation(obs)
    assert result.resolution == "implausible"


# ---------------------------------------------------------------------------
# normalize_observation — Rung 4 (no uncued guessing)
# ---------------------------------------------------------------------------


def test_rung4_bare_low_value_no_period_implausible():
    # §2 S3: bare (46, None, unknown) -> quarantine, never guess hourly.
    obs = SalaryObservation(46, None, period="unknown", provenance="feed_string")
    result = normalize_observation(obs)
    assert result.salary_min is None
    assert result.salary_max is None
    assert result.resolution == "implausible"


def test_rung4_sub_floor_pair_no_monthly_guess():
    # Values in [1k, 30k) never salvage as monthly without a cue.
    obs = SalaryObservation(4_000, 25_000, period="unknown", provenance="feed_string")
    result = normalize_observation(obs)
    assert result.resolution == "implausible"


def test_rung4_jooble_junk_implausible():
    # §2 S2 jooble-shaped junk: 3k min stapled to 251k max, no units.
    obs = SalaryObservation(3_000, 251_000, period="unknown", provenance="feed_string")
    result = normalize_observation(obs)
    # 3k below floor -> one side fails rung 1-2; not cents -> quarantine.
    assert result.salary_min is None
    assert result.salary_max is None
    assert result.resolution == "implausible"


# ---------------------------------------------------------------------------
# normalize_observation — Rung 5 (pair discipline / single-sided)
# ---------------------------------------------------------------------------


def test_rung5_single_sided_min_only():
    obs = SalaryObservation(120_000, None, period="unknown", provenance="feed_string")
    result = normalize_observation(obs)
    assert result.salary_min == 120_000
    assert result.salary_max is None
    assert result.resolution == "ok"


def test_rung5_single_sided_max_only():
    obs = SalaryObservation(None, 150_000, period="unknown", provenance="feed_string")
    result = normalize_observation(obs)
    assert result.salary_min is None
    assert result.salary_max == 150_000
    assert result.resolution == "ok"


def test_rung5_single_sided_hourly():
    obs = SalaryObservation(50, None, period="hourly", provenance="jd_regex")
    result = normalize_observation(obs)
    assert result.salary_min == 104_000
    assert result.salary_max is None
    assert result.resolution == "salvaged_hourly"


def test_rung5_single_sided_cents():
    obs = SalaryObservation(17_000_000, None, period="unknown", provenance="ats_structured")
    result = normalize_observation(obs)
    assert result.salary_min == 170_000
    assert result.salary_max is None
    assert result.resolution == "salvaged_cents"


# ---------------------------------------------------------------------------
# normalize_observation — Rung 6 (inversion)
# ---------------------------------------------------------------------------


def test_rung6_inversion_swapped_within_10x():
    obs = SalaryObservation(150_000, 120_000, period="unknown", provenance="feed_string")
    result = normalize_observation(obs)
    assert result.salary_min == 120_000
    assert result.salary_max == 150_000
    assert result.resolution == "ok"


def test_rung6_inversion_over_10x_implausible():
    # 5M min vs 100k max -> ratio 50 -> not a swap, quarantine.
    obs = SalaryObservation(5_000_000, 100_000, period="unknown", provenance="feed_string")
    result = normalize_observation(obs)
    assert result.salary_min is None
    assert result.salary_max is None
    assert result.resolution == "implausible"


# ---------------------------------------------------------------------------
# normalize_observation — empty
# ---------------------------------------------------------------------------


def test_empty_observation():
    obs = SalaryObservation(None, None, period="unknown", provenance="feed_string")
    result = normalize_observation(obs)
    assert result.salary_min is None
    assert result.salary_max is None
    assert result.resolution == "empty"


def test_invalid_period_treated_as_unknown():
    obs = SalaryObservation(120_000, 150_000, period="fortnightly", provenance="feed_string")
    result = normalize_observation(obs)
    assert result.salary_min == 120_000
    assert result.salary_max == 150_000
    assert result.resolution == "ok"


# ---------------------------------------------------------------------------
# Immutability / value-object discipline
# ---------------------------------------------------------------------------


def test_observation_is_frozen():
    obs = SalaryObservation(120_000, 150_000)
    with pytest.raises(Exception):
        obs.min_value = 0  # type: ignore[misc]


def test_normalized_is_frozen():
    result = normalize_observation(SalaryObservation(120_000, 150_000))
    assert isinstance(result, NormalizedSalary)
    with pytest.raises(Exception):
        result.salary_min = 0  # type: ignore[misc]


def test_normalize_does_not_mutate_input():
    obs = SalaryObservation(42, 51, period="hourly", provenance="jd_regex")
    normalize_observation(obs)
    assert obs.min_value == 42
    assert obs.max_value == 51
    assert obs.period == "hourly"

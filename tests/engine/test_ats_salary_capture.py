"""Tests for the ATS salary capture → normalize bridge (Data Integrity Overhaul P1.3).

Covers the shared ``_salary`` helper and the Lever/Ashby/Pinpoint capture sites
that delegate to it. The Greenhouse-specific decode is covered in
``test_greenhouse_layer1.py``.
"""

from __future__ import annotations

from jobcannon.engine.salary_normalizer import SalaryObservation, normalize_observation
from jobcannon.engine.ats_platforms._platforms_ashby import _posting_to_job as ashby_job
from jobcannon.engine.ats_platforms._platforms_lever import _posting_to_job as lever_job
from jobcannon.engine.ats_platforms._platforms_pinpoint import _posting_to_job as pinpoint_job
from jobcannon.engine.ats_platforms._salary import (
    build_salary_fields,
    normalize_currency,
    period_from_interval,
)

# ---------------------------------------------------------------------------
# period_from_interval
# ---------------------------------------------------------------------------


class TestPeriodFromInterval:
    def test_lever_vocabulary(self):
        assert period_from_interval("per-year-salary") == "annual"
        assert period_from_interval("per-hour-wage") == "hourly"
        assert period_from_interval("per-month-salary") == "monthly"
        assert period_from_interval("per-week-salary") == "weekly"
        assert period_from_interval("per-day-wage") == "daily"

    def test_ashby_vocabulary(self):
        assert period_from_interval("1 YEAR") == "annual"
        assert period_from_interval("HOUR") == "hourly"

    def test_unknown_and_empty(self):
        assert period_from_interval(None) == "unknown"
        assert period_from_interval("") == "unknown"
        assert period_from_interval("one-time") == "unknown"


# ---------------------------------------------------------------------------
# normalize_currency (m081 allowlist)
# ---------------------------------------------------------------------------


class TestNormalizeCurrency:
    def test_allowlisted_pass_through(self):
        assert normalize_currency("eur") == "EUR"
        assert normalize_currency("GBP") == "GBP"

    def test_off_allowlist_folds_to_usd(self):
        # A code the m081 CHECK would reject must fold to USD, never abort upsert.
        assert normalize_currency("JPY") == "USD"
        assert normalize_currency(None) == "USD"


# ---------------------------------------------------------------------------
# build_salary_fields
# ---------------------------------------------------------------------------


class TestBuildSalaryFields:
    def test_empty_emits_no_observation(self):
        out = build_salary_fields(None, None)
        assert out["salary_min"] is None
        assert out["salary_provenance"] is None
        assert out["salary_observation"] is None

    def test_annual_in_window_ok(self):
        out = build_salary_fields(150_000, 200_000, period="annual", currency="USD")
        assert out["salary_min"] == 150_000
        assert out["salary_max"] == 200_000
        assert out["salary_period"] == "annual"
        assert out["salary_provenance"] == "ats_structured"
        # D-1: the observation records the raw values verbatim.
        assert out["salary_observation"]["min_value"] == 150_000
        assert out["salary_observation"]["provenance"] == "ats_structured"

    def test_hourly_annualizes(self):
        out = build_salary_fields(64, 90, period="hourly")
        assert out["salary_min"] == 133_120
        assert out["salary_max"] == 187_200
        assert out["salary_period"] == "hourly"

    def test_off_allowlist_currency_folds(self):
        out = build_salary_fields(150_000, 200_000, period="annual", currency="JPY")
        assert out["salary_currency"] == "USD"


# ---------------------------------------------------------------------------
# Lever capture
# ---------------------------------------------------------------------------


class TestLeverSalaryCapture:
    def test_direct_annual_dollars(self):
        posting = {
            "text": "Staff DS",
            "hostedUrl": "https://jobs.lever.co/acme/x",
            "salaryRange": {"min": 180_000, "max": 250_000, "currency": "USD"},
        }
        out = lever_job(posting, "acme")
        assert out["salary_min"] == 180_000
        assert out["salary_max"] == 250_000
        assert out["salary_period"] == "unknown"  # no interval exposed
        assert out["salary_provenance"] == "ats_structured"
        assert out["salary_observation"]["min_value"] == 180_000

    def test_interval_hourly_annualizes(self):
        posting = {
            "text": "Contractor",
            "hostedUrl": "https://jobs.lever.co/acme/y",
            "salaryRange": {
                "min": 50,
                "max": 75,
                "currency": "USD",
                "interval": "per-hour-wage",
            },
        }
        out = lever_job(posting, "acme")
        assert out["salary_min"] == 104_000  # 50 × 2080
        assert out["salary_max"] == 156_000  # 75 × 2080
        assert out["salary_period"] == "hourly"

    def test_absent_salary_no_observation(self):
        posting = {"text": "X", "hostedUrl": "https://jobs.lever.co/acme/z", "salaryRange": None}
        out = lever_job(posting, "acme")
        assert out["salary_min"] is None
        assert out["salary_observation"] is None
        assert out["salary_provenance"] is None


# ---------------------------------------------------------------------------
# Ashby capture
# ---------------------------------------------------------------------------


class TestAshbySalaryCapture:
    def test_base_salary_component(self):
        posting = {
            "title": "ML Eng",
            "jobUrl": "https://jobs.ashbyhq.com/Acme/x",
            "compensation": {
                "summaryComponents": [
                    {
                        "compensationType": "base_salary",
                        "minValue": 200_000,
                        "maxValue": 280_000,
                        "currencyCode": "USD",
                        "interval": "1 YEAR",
                    }
                ]
            },
        }
        out = ashby_job(posting, "Acme")
        assert out["salary_min"] == 200_000
        assert out["salary_max"] == 280_000
        assert out["salary_period"] == "annual"
        assert out["salary_provenance"] == "ats_structured"
        assert out["salary_observation"]["max_value"] == 280_000

    def test_no_base_salary_component_no_observation(self):
        posting = {
            "title": "Eng",
            "jobUrl": "https://jobs.ashbyhq.com/Acme/y",
            "compensation": {"summaryComponents": [], "compensationTierSummary": "Equity"},
        }
        out = ashby_job(posting, "Acme")
        assert out["salary_min"] is None
        assert out["salary_observation"] is None


# ---------------------------------------------------------------------------
# Pinpoint capture
# ---------------------------------------------------------------------------


class TestPinpointSalaryCapture:
    def test_direct_compensation(self):
        posting = {
            "title": "Analyst",
            "url": "https://acme.pinpointhq.com/jobs/1",
            "compensation_minimum": 120_000,
            "compensation_maximum": 160_000,
        }
        out = pinpoint_job(posting, "acme")
        assert out["salary_min"] == 120_000
        assert out["salary_max"] == 160_000
        assert out["salary_provenance"] == "ats_structured"
        assert out["salary_observation"]["min_value"] == 120_000

    def test_non_numeric_compensation_ignored(self):
        posting = {
            "title": "Analyst",
            "url": "https://acme.pinpointhq.com/jobs/2",
            "compensation_minimum": "competitive",
            "compensation_maximum": None,
        }
        out = pinpoint_job(posting, "acme")
        assert out["salary_min"] is None
        assert out["salary_observation"] is None


# ---------------------------------------------------------------------------
# Observation round-trips through the normalizer (resolution codes)
# ---------------------------------------------------------------------------


def test_unitless_cents_observation_salvages():
    """A Greenhouse-style unit-less raw-cents observation salvages to dollars."""
    out = build_salary_fields(17_000_000, 20_000_000, period="unknown")
    # Strip the stamped resolution key (P1.6) — it is a normalizer output, not a
    # raw SalaryObservation field — before reconstructing the observation.
    raw_obs = {k: v for k, v in out["salary_observation"].items() if k != "resolution"}
    obs = SalaryObservation(**raw_obs)
    norm = normalize_observation(obs)
    assert norm.salary_min == 170_000
    assert norm.resolution == "salvaged_cents"
    # The capture site stamps the salvage verdict onto the persisted observation.
    assert out["salary_observation"]["resolution"] == "salvaged_cents"

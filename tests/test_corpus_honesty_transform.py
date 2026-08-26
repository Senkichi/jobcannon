import pandas as pd
import pytest

from analyses.corpus_honesty.transform import (
    effective_stale_aggregates,
    headline_ratio,
    provenance_aggregates,
)


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        rows, columns=["bucket", "is_stale", "expiry_status", "has_jd", "is_scored"]
    )


def test_provenance_aggregates_basic_rates():
    df = _df(
        [
            {
                "bucket": "ats_confirmed",
                "is_stale": False,
                "expiry_status": "expired",
                "has_jd": True,
                "is_scored": True,
            },
            {
                "bucket": "ats_confirmed",
                "is_stale": False,
                "expiry_status": "live",
                "has_jd": True,
                "is_scored": False,
            },
            {
                "bucket": "aggregator_only",
                "is_stale": True,
                "expiry_status": "live",
                "has_jd": False,
                "is_scored": False,
            },
            {
                "bucket": "aggregator_only",
                "is_stale": True,
                "expiry_status": "live",
                "has_jd": True,
                "is_scored": True,
            },
        ]
    )
    result = provenance_aggregates(df)
    assert result["ALL"]["n"] == 4
    ac = result["ats_confirmed"]
    assert ac["n"] == 2
    assert ac["stale_rate"] == 0.0
    assert ac["expired_rate"] == 0.5
    assert ac["live_rate"] == 0.5
    assert ac["has_jd_rate"] == 1.0
    assert ac["scored_rate"] == 0.5
    ao = result["aggregator_only"]
    assert ao["n"] == 2
    assert ao["stale_rate"] == 1.0
    assert ao["live_rate"] == 1.0


def test_headline_ratio():
    aggregates = {
        "ats_confirmed": {"stale_rate": 0.01},
        "aggregator_only": {"stale_rate": 0.28},
    }
    assert headline_ratio(aggregates) == pytest.approx(28.0)


def test_headline_ratio_none_on_zero_denominator():
    aggregates = {
        "ats_confirmed": {"stale_rate": 0.0},
        "aggregator_only": {"stale_rate": 0.28},
    }
    assert headline_ratio(aggregates) is None


def test_headline_ratio_none_on_missing_bucket():
    assert headline_ratio({}) is None


def test_effective_stale_aggregates_ors_in_expired_status():
    # is_stale flag is False, but expiry_status is already 'expired' -
    # the effective-stale variant must count this row as stale.
    df = _df(
        [
            {
                "bucket": "aggregator_only",
                "is_stale": False,
                "expiry_status": "expired",
                "has_jd": True,
                "is_scored": True,
            },
            {
                "bucket": "aggregator_only",
                "is_stale": False,
                "expiry_status": "live",
                "has_jd": True,
                "is_scored": True,
            },
        ]
    )
    primary = provenance_aggregates(df)
    assert primary["aggregator_only"]["stale_rate"] == 0.0

    effective = effective_stale_aggregates(df)
    assert effective["aggregator_only"]["stale_rate"] == 0.5

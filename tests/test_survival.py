import pandas as pd

from analyses.posting_lifespan.survival import km_by_platform


def _df(n_events: int, duration: float, platform: str = "greenhouse") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "platform": [platform] * n_events,
            "duration_days": [duration] * n_events,
            "observed": [1] * n_events,
        }
    )


def test_km_all_die_at_10_days():
    df = _df(250, 10.0)
    result = km_by_platform(df)
    assert result["greenhouse"]["median_days"] == 10.0
    assert result["greenhouse"]["n"] == 250
    assert result["greenhouse"]["events"] == 250
    assert result["greenhouse"]["survival"][7] == 1.0
    assert result["greenhouse"]["survival"][14] == 0.0


def test_min_stratum_floor_drops_small_platforms():
    df = pd.concat([_df(250, 10.0), _df(50, 5.0, platform="lever")])
    result = km_by_platform(df)
    assert "lever" not in result
    assert "greenhouse" in result
    assert result["ALL"]["n"] == 300  # small strata still count in ALL

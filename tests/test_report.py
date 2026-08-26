import pandas as pd

from analyses.posting_lifespan.report import write_outputs
from analyses.posting_lifespan.survival import km_by_platform


def _results():
    df = pd.DataFrame(
        {"platform": ["greenhouse"] * 250, "duration_days": [10.0] * 250, "observed": [1] * 250}
    )
    return km_by_platform(df)


def test_write_outputs(tmp_path):
    exclusions = {
        "total": 300,
        "no_company_join": 10,
        "null_platform": 20,
        "bad_first_seen": 3,
        "negative_window": 0,
        "inconclusive_or_null_expiry": 17,
        "usable": 250,
    }
    write_outputs(_results(), _results(), exclusions, tmp_path)
    assert (tmp_path / "aggregates.csv").is_file()
    assert (tmp_path / "figures" / "km_by_platform.png").is_file()
    numbers = (tmp_path / "NUMBERS.md").read_text(encoding="utf-8")
    for section in [
        "## Headline",
        "## Exclusion accounting",
        "## Method",
        "## Results",
        "## Robustness",
        "## Caveats",
        "## SQL appendix",
    ]:
        assert section in numbers
    df = pd.read_csv(tmp_path / "aggregates.csv")
    assert set(df.columns) == {
        "variant",
        "platform",
        "n",
        "events",
        "median_days",
        "s7",
        "s14",
        "s30",
        "s60",
    }
    assert "greenhouse" in set(df["platform"])

import pandas as pd

from analyses.corpus_honesty.report import write_outputs
from analyses.corpus_honesty.transform import (
    effective_stale_aggregates,
    provenance_aggregates,
)


def _results():
    df = pd.DataFrame(
        {
            "bucket": ["ats_confirmed"] * 100 + ["aggregator_only"] * 100,
            "is_stale": [False] * 100 + [True] * 40 + [False] * 60,
            "expiry_status": ["live"] * 100 + ["expired"] * 40 + ["live"] * 60,
            "has_jd": [True] * 200,
            "is_scored": [True] * 200,
        },
        columns=["bucket", "is_stale", "expiry_status", "has_jd", "is_scored"],
    )
    return df


def test_write_outputs(tmp_path):
    df = _results()
    primary = provenance_aggregates(df)
    excl_largest = provenance_aggregates(df)  # same shape is fine for this test
    effective_stale = effective_stale_aggregates(df)
    exclusions = {
        "total": 210,
        "malformed_sources_json": 0,
        "no_attributable_source": 5,
        "unrecognized_source_tag": 5,
        "usable": 200,
    }
    write_outputs(primary, excl_largest, effective_stale, exclusions, tmp_path)

    assert (tmp_path / "aggregates.csv").is_file()
    assert (tmp_path / "figures" / "stale_rate_by_provenance.png").is_file()

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

    csv_df = pd.read_csv(tmp_path / "aggregates.csv")
    expected_cols = {
        "variant",
        "bucket",
        "n",
        "stale_n",
        "stale_rate",
        "has_jd_rate",
        "scored_rate",
        "live_rate",
        "expired_rate",
        "inconclusive_rate",
        "null_rate",
    }
    assert expected_cols <= set(csv_df.columns)
    assert "ats_confirmed" in set(csv_df["bucket"])
    assert "primary" in set(csv_df["variant"])

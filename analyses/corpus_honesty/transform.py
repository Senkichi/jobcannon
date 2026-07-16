"""Per-provenance-class aggregates: counts, staleness, expiry distribution,
enrichment coverage, and the headline aggregator-vs-ATS staleness ratio.
"""

from __future__ import annotations

import pandas as pd

EXPIRY_STATUSES = ["live", "expired", "inconclusive", "null"]

# The single largest third-party aggregator source tag by raw occurrence in
# the live corpus (verified 2026-07-16: portal_jooble 3,463 > dataforseo
# 3,005 > portal_adzuna 1,548 > ...). Used only by the "drop the largest
# aggregator" robustness variant in report.py - never by the primary result.
LARGEST_AGGREGATOR_SOURCE = "portal_jooble"


def _bucket_stats(df: pd.DataFrame) -> dict:
    n = len(df)
    if n == 0:
        stats = {
            "n": 0,
            "stale_n": 0,
            "stale_rate": float("nan"),
            "has_jd_rate": float("nan"),
            "scored_rate": float("nan"),
        }
        for status in EXPIRY_STATUSES:
            stats[f"{status}_n"] = 0
            stats[f"{status}_rate"] = float("nan")
        return stats

    stale_n = int(df["is_stale"].sum())
    stats = {
        "n": n,
        "stale_n": stale_n,
        "stale_rate": stale_n / n,
        "has_jd_rate": float(df["has_jd"].sum()) / n,
        "scored_rate": float(df["is_scored"].sum()) / n,
    }
    expiry_counts = df["expiry_status"].value_counts()
    for status in EXPIRY_STATUSES:
        c = int(expiry_counts.get(status, 0))
        stats[f"{status}_n"] = c
        stats[f"{status}_rate"] = c / n
    return stats


def provenance_aggregates(df: pd.DataFrame) -> dict[str, dict]:
    """One stats dict per provenance bucket, plus an 'ALL' rollup."""
    results: dict[str, dict] = {"ALL": _bucket_stats(df)}
    for bucket, group in df.groupby("bucket"):
        results[str(bucket)] = _bucket_stats(group)
    return results


def headline_ratio(
    aggregates: dict[str, dict],
    numerator: str = "aggregator_only",
    denominator: str = "ats_confirmed",
) -> float | None:
    """numerator's stale_rate / denominator's stale_rate, or None if undefined."""
    denom = aggregates.get(denominator, {}).get("stale_rate")
    numer = aggregates.get(numerator, {}).get("stale_rate")
    if not denom or numer is None or pd.isna(denom) or pd.isna(numer):
        return None
    return numer / denom


def effective_stale_aggregates(df: pd.DataFrame) -> dict[str, dict]:
    """Robustness variant: alternative staleness definition.

    `is_stale` (the primary metric) is a flag the pipeline sets independently
    of `expiry_status`. This variant instead treats a posting as stale iff
    is_stale is set OR expiry_status has already resolved to 'expired' - a
    broader, more inclusive definition. Reported alongside the primary
    result to show the qualitative finding (ATS-confirmed postings are far
    less likely to be a stale republish) isn't an artifact of exactly how
    "stale" is defined.
    """
    effective = df.copy()
    effective["is_stale"] = df["is_stale"] | (df["expiry_status"] == "expired")
    return provenance_aggregates(effective)

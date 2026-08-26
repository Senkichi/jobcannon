"""Kaplan-Meier lifespan estimates by ATS platform (right-censored)."""

import pandas as pd
from lifelines import KaplanMeierFitter

MIN_STRATUM_N = 200
SURVIVAL_GRID_DAYS = [7, 14, 30, 60]


def _fit_one(df: pd.DataFrame) -> dict:
    kmf = KaplanMeierFitter()
    kmf.fit(df["duration_days"], event_observed=df["observed"])
    survival = {d: float(kmf.predict(d)) for d in SURVIVAL_GRID_DAYS}
    return {
        "n": int(len(df)),
        "events": int(df["observed"].sum()),
        "median_days": float(kmf.median_survival_time_),
        "survival": survival,
    }


def km_by_platform(df: pd.DataFrame, min_n: int = MIN_STRATUM_N) -> dict[str, dict]:
    results: dict[str, dict] = {"ALL": _fit_one(df)}
    for platform, group in df.groupby("platform"):
        if len(group) >= min_n:
            results[str(platform)] = _fit_one(group)
    return results

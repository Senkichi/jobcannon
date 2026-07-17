"""ATS salary capture → normalize bridge (Data Integrity Overhaul P1.3).

The single point where an ATS platform scanner turns the raw structured pay
values it decoded into the canonical salary job-dict fields. Every salary-emitting
scanner (Greenhouse, Lever, Ashby, Pinpoint, ...) delegates here so the
capture → normalize → reconcile contract (tracking issue #393) is enforced in
exactly one place rather than re-implemented per platform.

Design rules honored (binding — see #393):

  * **D-1 (Lossless capture).** The scanner's RAW per-period values — what the
    source actually asserted, e.g. ``$64/hour`` — are wrapped verbatim in a
    :class:`~jobcannon.engine.salary_normalizer.SalaryObservation` and returned (as a
    dict) for the append-only ``salary_observations`` log. The observation is the
    raw evidence, NOT the annualized canonical pair; ``raw_text`` retains the
    verbatim API fragment for healing/quarantine.
  * **D-2 (Single normalizer).** Annualization and salvage are delegated to
    :func:`~jobcannon.engine.salary_normalizer.normalize_observation`. No scanner does
    its own unit math — only the lossless source-specific *decode* (e.g.
    Greenhouse's cents-vs-dollars question) stays at the capture site.
  * **D-3 (Salvage ladder).** Implausible values are quarantined (canonical NULL,
    evidence retained) by the normalizer, never silently dropped or kept.
  * **D-4 (Trust rank).** ATS structured pay is the highest-trust salary source,
    provenance ``ats_structured`` (rank 4); the reconciler ranks it on upsert.
"""

from __future__ import annotations

from jobcannon.engine.salary_normalizer import (
    SalaryObservation,
    normalize_observation,
    observation_to_dict,
)

# Substring → normalizer period. Handles the divergent interval vocabularies ATS
# APIs expose (Lever's "per-year-salary", Ashby's "1 YEAR", Greenhouse's "year")
# with one case-insensitive substring match. "year"/"annual" precede the others
# so an "annual" token never falls through. weekly/daily are preserved here — the
# normalizer annualizes them and the m081 column folds them to 'unknown'.
_INTERVAL_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("year", "annual"),
    ("annual", "annual"),
    ("hour", "hourly"),
    ("month", "monthly"),
    ("week", "weekly"),
    ("day", "daily"),
)


def period_from_interval(value: str | None) -> str:
    """Map a free-form ATS interval token to a normalizer period (else 'unknown')."""
    if not value:
        return "unknown"
    low = str(value).lower()
    for keyword, period in _INTERVAL_KEYWORDS:
        if keyword in low:
            return period
    return "unknown"


# m081 ``salary_currency`` CHECK allowlist. A code outside it would abort the
# upsert, so a source currency we cannot map folds to 'USD' — the verbatim source
# currency survives in the observation ``raw_text`` (D-1), and non-USD comp is
# excluded from display rather than converted (explicit non-goal).
_CURRENCY_ALLOWLIST: frozenset[str] = frozenset(
    {"USD", "GBP", "EUR", "CAD", "AUD", "INR", "SGD", "UNKNOWN"}
)


def normalize_currency(value: str | None) -> str:
    """Fold a source currency code to the m081 allowlist (default 'USD')."""
    if not value:
        return "USD"
    code = str(value).strip().upper()
    return code if code in _CURRENCY_ALLOWLIST else "USD"


def _empty_salary_fields(currency: str = "USD") -> dict:
    """Job-dict salary fields for a posting with no pay range (no observation emitted)."""
    return {
        "salary_min": None,
        "salary_max": None,
        "salary_period": "unknown",
        "salary_currency": currency,
        "salary_provenance": None,
        "salary_observation": None,
    }


def build_salary_fields(
    min_value: float | None,
    max_value: float | None,
    *,
    period: str = "unknown",
    currency: str | None = "USD",
    raw_text: str | None = None,
) -> dict:
    """Build the canonical salary job-dict fields for an ATS structured pay range.

    Wraps the RAW per-period values a scanner decoded into a lossless
    :class:`SalaryObservation` (provenance ``ats_structured``, D-1/D-4), runs the
    single normalizer's salvage ladder (D-2/D-3), and returns both the canonical
    columns and the raw observation dict for the append-only log.

    Args:
        min_value: Lower bound the source asserted, per ``period`` (raw, not
            annualized), or None.
        max_value: Upper bound the source asserted, per ``period``, or None.
        period: Source period token from :func:`period_from_interval` (or a
            platform's own mapper). 'unknown' when the API exposes no interval.
        currency: Source currency code (recorded, never converted).
        raw_text: Verbatim source JSON fragment, retained on the observation.

    Returns:
        A dict with ``salary_min``/``salary_max`` (annualized USD-equivalent ints
        or None when quarantined), ``salary_period`` (m081 column-safe source
        period), ``salary_currency``, ``salary_provenance`` (``ats_structured``
        when a value is present, else None), and ``salary_observation`` (the RAW
        observation as a dict, or None when no value is present).
    """
    currency = normalize_currency(currency)
    if min_value is None and max_value is None:
        return _empty_salary_fields(currency)

    observation = SalaryObservation(
        min_value=min_value,
        max_value=max_value,
        period=period,
        currency=currency,
        provenance="ats_structured",
        raw_text=raw_text,
    )
    normalized = normalize_observation(observation)
    return {
        "salary_min": normalized.salary_min,
        "salary_max": normalized.salary_max,
        "salary_period": normalized.period,
        "salary_currency": normalized.currency,
        "salary_provenance": "ats_structured",
        # Stamp the salvage verdict (P1.6) onto the lossless record so a NULLed
        # implausible ATS pair still routes to quarantine via from_job (D-3/D-9).
        "salary_observation": observation_to_dict(observation, normalized.resolution),
    }

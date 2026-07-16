"""Single normalization point for salary observations (Data Integrity Overhaul P1.1).

This is the **foundation layer** of the salary capture -> normalize -> reconcile
architecture (tracking issue #393). It owns:

  * ``SalaryObservation`` — a lossless value object recording what a single source
    asserted about pay, with unit/currency/provenance metadata (design rule D-1).
  * ``parse_salary_text`` — the single text parser that replaces the five+ scattered,
    unvalidated salary regexes catalogued in the plan's §1.2 (design rule D-2).
  * ``normalize_observation`` — the single pure function that converts an observation
    into canonical annualized-USD form via an explicit **salvage ladder** (D-3).

Design constraints (binding — see #393):

  * **No imports from ``job_finder.web``.** This module is a leaf dependency that the
    capture sites (ATS scanners, SERP/feed sources, email parsers) and the existing
    ``salary_extractor`` will delegate to in sibling PRs (#380, #382, #383). It must
    stay importable without pulling in Flask/app state.
  * **Pure functions, no I/O.** The only side effect permitted is the module logger.
  * **Immutability.** Both dataclasses are ``frozen``; functions return new objects.

Canonical semantics produced here (write into every P1 issue):
  ``salary_min``/``salary_max`` are **annualized USD-equivalent integers** within
  ``[MIN_PLAUSIBLE_ANNUAL, MAX_PLAUSIBLE_ANNUAL]``. The reported ``period`` is the
  *source* period the posting actually stated; the column-safe mapping
  (``period_for_column``) folds 'weekly'/'daily' to 'unknown' to respect the m081
  CHECK allowlist. ``currency`` is recorded, never converted (multi-currency
  conversion is an explicit non-goal). ``provenance`` records the writer class so the
  reconciler (P1.3, design rule D-4) can rank trust.

The salvage ladder NEVER silently discards evidence and NEVER silently keeps an
aberrant value: an unsalvageable observation yields ``salary_min/max = None`` with a
``resolution`` code, and the verbatim ``raw_text`` is retained on the observation for
healing + ``/admin/review`` quarantine (D-3, D-9, D-12).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single source of truth for plausibility bounds and trust ranks.
# salary_extractor + the feed/email parsers import these after P1.2/P1.3 so the
# [$30K, $5M] window stops being copy-pasted into six modules (§1.2).
# ---------------------------------------------------------------------------

MIN_PLAUSIBLE_ANNUAL = 30_000
MAX_PLAUSIBLE_ANNUAL = 5_000_000

# Trust ranking for pair-atomic reconciliation (design rule D-4). Higher wins.
# email_snippet and feed_string deliberately tie at 1.
PROVENANCE_RANK: dict[str, int] = {
    "ats_structured": 4,
    "jd_regex": 3,
    "llm_extract": 2,
    "email_snippet": 1,
    "feed_string": 1,
}

# Annualization multipliers. A "known" period multiplies the per-period figure to
# an annual figure. 'annual' and 'unknown' are identity.
_ANNUALIZE_FACTORS: dict[str, int] = {
    "annual": 1,
    "hourly": 2080,  # 40 h/wk * 52 wk
    "daily": 260,  # 5 d/wk * 52 wk
    "weekly": 52,
    "monthly": 12,
    "unknown": 1,
}

# Valid source periods an observation may carry.
VALID_PERIODS: frozenset[str] = frozenset(_ANNUALIZE_FACTORS)

# m081 CHECK allowlist for the salary_period column. weekly/daily collapse to
# 'unknown' in the column; their true period lives in the observation log.
_COLUMN_PERIODS: frozenset[str] = frozenset({"annual", "hourly", "monthly", "unknown"})

# Resolution codes that mean the salvage ladder produced a usable canonical pair.
# A capture site writes the canonical (min, max) ONLY for these; any other code
# ('implausible'/'empty') yields a NULL pair with the observation retained (D-3).
RESOLVED_RESOLUTIONS: frozenset[str] = frozenset(
    {
        "ok",
        "salvaged_hourly",
        "salvaged_daily",
        "salvaged_weekly",
        "salvaged_monthly",
        "salvaged_cents",
    }
)

# Map non-annual salvage periods to their resolution code.
_SALVAGE_RESOLUTION: dict[str, str] = {
    "hourly": "salvaged_hourly",
    "daily": "salvaged_daily",
    "weekly": "salvaged_weekly",
    "monthly": "salvaged_monthly",
}

# Currency cue -> ISO code. Default is USD when no cue is present.
_CURRENCY_CUES: tuple[tuple[str, str], ...] = (
    # Order matters: multi-char prefixes before bare symbols.
    ("CA$", "CAD"),
    ("C$", "CAD"),
    ("A$", "AUD"),
    ("S$", "SGD"),
    ("£", "GBP"),
    ("€", "EUR"),
    ("₹", "INR"),
)

# Period cues, longest/most-specific first so "per hour" wins before a bare "hour"
# substring inside another word can ever matter. Each entry maps a regex to a period.
_PERIOD_CUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/\s*hr\b|per\s+hour|an?\s+hour|hourly", re.IGNORECASE), "hourly"),
    (
        re.compile(r"/\s*yr\b|per\s+year|a\s+year|annually|/\s*annum|per\s+annum", re.IGNORECASE),
        "annual",
    ),
    (re.compile(r"/\s*mo\b|per\s+month|monthly", re.IGNORECASE), "monthly"),
    (re.compile(r"/\s*wk\b|per\s+week|weekly", re.IGNORECASE), "weekly"),
    (re.compile(r"/\s*day\b|per\s+day|daily", re.IGNORECASE), "daily"),
)

# Range parser. Captures both sides plus optional K/M unit on each. A currency
# symbol/code prefix (``$``, ``£``, ``€``, ``USD``, ``CA$`` ...) may precede either
# number; we skip it here and resolve currency separately from the whole string so
# cue position doesn't matter. ``_CURR_PREFIX`` is non-capturing.
_CURR_PREFIX = r"(?:USD|CAD|EUR|GBP|AUD|SGD|INR|[$£€₹CASca]{0,3})\s*"
# ``(?![A-Za-z])`` after each unit prevents grabbing the leading letter of a
# trailing word ("3-5 months" must not read 5 as 5M).
_RANGE_RE = re.compile(
    rf"{_CURR_PREFIX}(?P<low>\d[\d,]*\.?\d*)\s*(?P<low_unit>[KkMm])?(?![A-Za-z])"
    r"\s*(?:to|-|–|—|~)\s*"
    rf"{_CURR_PREFIX}(?P<high>\d[\d,]*\.?\d*)\s*(?P<high_unit>[KkMm])?(?![A-Za-z])",
)


@dataclass(frozen=True)
class SalaryObservation:
    """A single, lossless salary assertion from one source (design rule D-1).

    Capture sites build one of these at the boundary where unit/currency metadata
    still exists, and pass it to :func:`normalize_observation`. Values are taken at
    face value: any K-elision or text munging happens in :func:`parse_salary_text`,
    never here. Direct-value feeds (RemoteOK, Lever, Ashby, ...) construct this
    directly with the numbers the API returned.

    Attributes:
        min_value: Lower bound the source asserted (per ``period``), or None.
        max_value: Upper bound the source asserted (per ``period``), or None.
        period: Source period the posting stated. One of ``VALID_PERIODS``.
        currency: Source currency (ISO-ish code). Recorded, never converted.
        provenance: Writer class, one of the ``PROVENANCE_RANK`` keys; drives trust ranking.
        raw_text: Verbatim source string/JSON fragment, retained for healing + debug.
    """

    min_value: float | None
    max_value: float | None
    period: str = "unknown"
    currency: str = "USD"
    provenance: str = "feed_string"
    raw_text: str | None = None


@dataclass(frozen=True)
class NormalizedSalary:
    """Canonical salary produced by the single normalizer.

    ``salary_min``/``salary_max`` are annualized USD-equivalent integers within the
    plausibility window, or None when the observation could not be salvaged. ``period``
    is the column-safe source period (m081 allowlist). ``resolution`` records which
    ladder rung fired so healing stats can count conversions.
    """

    salary_min: int | None
    salary_max: int | None
    period: str
    currency: str
    provenance: str
    resolution: str  # see _SALVAGE_RESOLUTION + 'ok'/'salvaged_cents'/'implausible'/'empty'


def annualize(value: float, period: str) -> float:
    """Annualize a per-period figure to an annual figure.

    ×2080 (hourly) / ×260 (daily) / ×52 (weekly) / ×12 (monthly) / ×1 (annual/unknown).
    Unknown periods are treated as identity (the salvage ladder decides what to do
    with an unknown-period value; this function only does the arithmetic).
    """
    return value * _ANNUALIZE_FACTORS.get(period, 1)


def period_for_column(period: str) -> str:
    """Fold a source period to the m081 ``salary_period`` CHECK allowlist.

    weekly/daily -> 'unknown' (their true period lives in salary_observations); the
    rest pass through. This is the only place the column-vs-observation period
    distinction is encoded.
    """
    return period if period in _COLUMN_PERIODS else "unknown"


def detect_currency(text: str) -> str:
    """Resolve the source currency from cue symbols/prefixes. Default USD."""
    for cue, code in _CURRENCY_CUES:
        if cue in text:
            return code
    return "USD"


def detect_period(text: str) -> str:
    """Resolve the source period from textual cues. Returns 'unknown' when none."""
    for pattern, period in _PERIOD_CUES:
        if pattern.search(text):
            return period
    return "unknown"


def _expand_amount(raw: str, unit: str | None) -> float | None:
    """Convert '120K' / '1.5M' / '150,000' to a float. None on parse error."""
    try:
        val = float(raw.replace(",", ""))
    except ValueError:
        return None
    if unit:
        upper = unit.upper()
        if upper == "K":
            val *= 1_000
        elif upper == "M":
            val *= 1_000_000
    return val


def parse_salary_text(text: str | None, *, provenance: str) -> SalaryObservation | None:
    """Parse a free-text salary range into a lossless :class:`SalaryObservation`.

    Unifies the five+ scattered, unvalidated salary regexes (§1.2) into one parser
    that ALSO captures the period and currency metadata the old parsers discarded
    (design rule D-2). It does NOT apply plausibility bounds or unit math beyond
    K/M-suffix expansion and K-elision — that is the normalizer's job (D-3). The
    returned observation carries raw values per the detected ``period``.

    Handled inputs:
        ``$120K - $150K`` / ``$120,000 - $150,000`` / ``120K to 150K``
        ``$120 - $150``  (K-elision: both < 1000, no units -> thousands)
        ``$42 - $51 an hour``  (period cue -> hourly, raw values 42/51)
        ``£60,000 - £80,000``  (currency cue -> GBP)

    K-elision lives here and ONLY here: if both sides are < 1000 with no K/M unit,
    they are interpreted as thousands. :func:`normalize_observation` never re-applies
    this — an observation's values are taken at face value.

    Returns:
        A :class:`SalaryObservation`, or ``None`` when no range is found. Single
        values stay out of scope (ambiguous min/max attribution), matching the
        existing ``extract_salary_from_text`` contract.
    """
    if not text:
        return None
    match = _RANGE_RE.search(text)
    if not match:
        return None

    low = _expand_amount(match.group("low"), match.group("low_unit"))
    high = _expand_amount(match.group("high"), match.group("high_unit"))
    if low is None or high is None:
        return None

    period = detect_period(text)
    currency = detect_currency(text)

    # K-elision (parse layer only): "$120 - $150" with no units, both < 1000.
    # Suppressed when a period cue is present — "$42 - $51 an hour" is genuinely
    # hourly dollars, not thousands; the normalizer annualizes it via rung 1.
    both_units_missing = not match.group("low_unit") and not match.group("high_unit")
    if period == "unknown" and both_units_missing and low < 1000 and high < 1000:
        low *= 1_000
        high *= 1_000
    return SalaryObservation(
        min_value=low,
        max_value=high,
        period=period,
        currency=currency,
        provenance=provenance,
        raw_text=text,
    )


def _in_bounds(value: float) -> bool:
    """True if an annualized value is within the plausibility window."""
    return MIN_PLAUSIBLE_ANNUAL <= value <= MAX_PLAUSIBLE_ANNUAL


def _implausible(obs: SalaryObservation) -> NormalizedSalary:
    """Quarantine result: canonical values NULL, evidence retained on the observation."""
    return NormalizedSalary(
        salary_min=None,
        salary_max=None,
        period=period_for_column(obs.period),
        currency=obs.currency,
        provenance=obs.provenance,
        resolution="implausible",
    )


def _resolve_side(value: float, period: str) -> tuple[int, str] | None:
    """Resolve a single side under the salvage ladder rungs 1-2.

    Returns ``(annualized_int, hypothesis)`` where hypothesis is the period that made
    it land in-bounds ('annual' for the unknown->assume-annual rung, or the known
    period). Returns ``None`` when this side cannot be resolved under rungs 1-2.

    Rung 1 (period known): annualize honestly; in-bounds -> resolved.
    Rung 2 (period unknown): assume annual; in-bounds -> resolved.
    Cents (rung 3) and quarantine (rung 4) are handled by the caller, which needs
    both sides to agree before reinterpreting.
    """
    if period == "unknown":
        if _in_bounds(value):
            return int(value), "annual"
        return None
    annual = annualize(value, period)
    if _in_bounds(annual):
        return int(annual), period
    return None


def _try_cents(obs: SalaryObservation, sides: list[float]) -> NormalizedSalary | None:
    """Rung 3: corroborated cents reinterpretation (design rule D-3).

    Only fires for ``provenance == 'ats_structured'`` with an unknown period: a
    structured cents field (Greenhouse min_cents/max_cents) lands raw as e.g.
    17_000_000 for $170k. The corroboration is that ÷100 lands every present side in
    bounds. The provenance restriction is load-bearing — applied to text this would
    mint fake salaries from funding numbers ("Series B: $10M - $50M").
    """
    if obs.provenance != "ats_structured" or obs.period != "unknown":
        return None
    if not all(value > MAX_PLAUSIBLE_ANNUAL for value in sides):
        return None
    if not all(_in_bounds(value / 100) for value in sides):
        return None
    has_min = obs.min_value is not None
    has_max = obs.max_value is not None
    salary_min = int(obs.min_value / 100) if has_min else None
    salary_max = int(obs.max_value / 100) if has_max else None
    return NormalizedSalary(
        salary_min=salary_min,
        salary_max=salary_max,
        period=period_for_column(obs.period),
        currency=obs.currency,
        provenance=obs.provenance,
        resolution="salvaged_cents",
    )


def normalize_observation(obs: SalaryObservation) -> NormalizedSalary:
    """Convert an observation to canonical annualized-USD form via the salvage ladder.

    The single normalizer (design rule D-2). Implements the salvage-before-discard,
    flag-before-guess ladder (design rule D-3):

      1. **period known** -> annualize both sides; in-bounds -> ``ok`` (or
         ``salvaged_hourly``/``_daily``/``_weekly``/``_monthly`` so healing stats can
         count conversions). Out of bounds after honest conversion -> ``implausible``.
      2. **period unknown, value(s) in window** -> assume annual, ``ok``.
      3. **period unknown AND provenance == 'ats_structured'**, all sides > MAX and
         ÷100 lands in-bounds -> cents, ``salvaged_cents`` (corroborated).
      4. **anything else** -> ``implausible`` (NULL canonical, evidence retained). No
         uncued unit guessing, ever.
      5. **Pair discipline:** a single present side is normalized alone. If the two
         sides only resolve under *different* hypotheses -> ``implausible`` (no
         Franken-pairs at birth).
      6. min > max after normalization -> swap if ratio ≤ 10, else ``implausible``
         (preserves the legacy ``_normalize_salary`` semantics, now pre-persistence).

    Returns a :class:`NormalizedSalary`; never raises on bad input.
    """
    if obs.min_value is None and obs.max_value is None:
        return NormalizedSalary(
            salary_min=None,
            salary_max=None,
            period=period_for_column(obs.period),
            currency=obs.currency,
            provenance=obs.provenance,
            resolution="empty",
        )

    period = obs.period if obs.period in VALID_PERIODS else "unknown"
    obs = replace(obs, period=period)

    present = [v for v in (obs.min_value, obs.max_value) if v is not None]

    # Rungs 1-2: try honest resolution side-by-side.
    resolved = [_resolve_side(v, period) for v in present]

    if all(r is not None for r in resolved):
        hypotheses = {r[1] for r in resolved}  # type: ignore[index]
        # Rung 5: both sides must agree on the hypothesis.
        if len(hypotheses) > 1:
            return _implausible(obs)
        hypothesis = next(iter(hypotheses))
        values = [r[0] for r in resolved]  # type: ignore[index]
        return _finalize(obs, values, hypothesis)

    # Rung 3: corroborated cents reinterpretation (whole pair).
    cents = _try_cents(obs, present)
    if cents is not None:
        return cents

    # Rung 4: quarantine.
    return _implausible(obs)


def _finalize(obs: SalaryObservation, values: list[int], hypothesis: str) -> NormalizedSalary:
    """Assemble the resolved pair: rung-6 inversion handling + resolution code.

    ``values`` are the annualized integers for the present sides, in the order the
    present sides appeared (min before max).
    """
    has_min = obs.min_value is not None
    has_max = obs.max_value is not None

    if has_min and has_max:
        salary_min, salary_max = values[0], values[1]
        # Rung 6: inversion handling.
        if salary_min > salary_max:
            if salary_min <= salary_max * 10:
                salary_min, salary_max = salary_max, salary_min
            else:
                return _implausible(obs)
    elif has_min:
        salary_min, salary_max = values[0], None
    else:
        salary_min, salary_max = None, values[0]

    if hypothesis == "annual":
        resolution = "ok"
    else:
        resolution = _SALVAGE_RESOLUTION.get(hypothesis, "ok")

    return NormalizedSalary(
        salary_min=salary_min,
        salary_max=salary_max,
        period=period_for_column(obs.period),
        currency=obs.currency,
        provenance=obs.provenance,
        resolution=resolution,
    )


# ---------------------------------------------------------------------------
# Capture-site convenience (P1.4)
# ---------------------------------------------------------------------------


def observation_to_dict(obs: SalaryObservation, resolution: str | None = None) -> dict:
    """Serialize an observation to the JSON-log dict shape used by the append-log.

    This is the lossless record persisted in the ``salary_observations`` column
    (D-1). The key names match what ``db._jobs._merge_salary_observations`` and
    the m107 healing migration read.

    ``resolution`` (P1.6, D-3/D-9) stamps the salvage-ladder verdict the single
    normalizer reached for this observation (``ok``/``salvaged_*``/``implausible``/
    ``empty``) onto the persisted record. Downstream quarantine detection
    (``ParsedJob.from_job``, ``data_enricher``) reads it to route an
    implausible-but-retained observation through ``unresolved_reasons`` without
    re-normalizing. Omitted (key absent) when ``None`` so legacy records and
    callers that do not normalize stay byte-identical; ``.get("resolution")``
    then reads as "not implausible".
    """
    record = {
        "min_value": obs.min_value,
        "max_value": obs.max_value,
        "period": obs.period,
        "currency": obs.currency,
        "provenance": obs.provenance,
        "raw_text": obs.raw_text,
    }
    if resolution is not None:
        record["resolution"] = resolution
    return record


def salary_capture_fields(obs: SalaryObservation | None) -> dict:
    """Build the Job salary kwargs for a capture-site observation (D-1/D-2/D-3).

    The single delegation point the feed/SERP capture sites use instead of their
    old bespoke regex + unit math (design rule D-2). Given a lossless
    :class:`SalaryObservation` (or None when the source asserted no salary), it
    runs the observation through the single normalizer and returns a dict of the
    salary-related ``Job`` constructor kwargs:

        salary_min / salary_max  — the annualized-USD canonical pair, populated
            ONLY when the salvage ladder resolved the value; an implausible /
            sub-floor / period-less value leaves them unset (Job default None).
        salary_period / salary_currency — the column-safe source period and the
            recorded currency (set whenever a value resolved; currency is always
            carried so display can show it).
        salary_provenance       — the writer class (drives trust ranking, D-4).
        salary_observations      — the lossless append-log seed: ALWAYS the
            verbatim observation when one exists, so evidence survives even when
            the canonical pair is NULLed (D-3, D-9, D-12).

    Returns ``{}`` for a ``None`` input so ``Job(**fields)`` falls back to the
    salary defaults (no observation, no provenance). Spread into the Job:
    ``Job(..., **salary_capture_fields(obs))``.
    """
    if obs is None:
        return {}
    normalized = normalize_observation(obs)
    fields: dict = {
        "salary_currency": normalized.currency,
        "salary_provenance": obs.provenance,
        # Stamp the salvage verdict (P1.6) so ParsedJob.from_job can quarantine an
        # implausible-but-retained observation (canonical NULL + salary_implausible).
        "salary_observations": [observation_to_dict(obs, normalized.resolution)],
    }
    if normalized.resolution in RESOLVED_RESOLUTIONS:
        fields["salary_min"] = normalized.salary_min
        fields["salary_max"] = normalized.salary_max
        fields["salary_period"] = normalized.period
    return fields

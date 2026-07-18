"""Comp-transparency structural axis: does the posting state real pay?

Reuses the engine's salary grammar (``jobcannon.engine.salary_normalizer``)
as a READ-ONLY classifier — this module NEVER writes ``salary_min``/
``salary_max``; those columns have a single writer
(``jobcannon.db._jobs.upsert_job``, via the capture-site salary functions).

A structured salary already present on the row wins outright (``"structured"``
method, unambiguous ``True``). Otherwise the JD body is scanned sentence-by-
sentence, and a sentence must clear THREE gates before it counts as a pay
disclosure:

  1. Currency anchor (``_CURRENCY_ANCHOR_RE``): a ``$``/``£``/``€``/``₹``
     symbol, an ISO code, or a spelled-out currency word ("dollars"). A bare
     numeric range with no currency ("supports 200-500 users daily") is
     headcount prose, not pay.
  2. Pay context (``_PAY_CONTEXT_RE``): the sentence must actually be ABOUT
     compensation (salary / compensation / wage / pay / OTE / ...), not merely
     contain a dollar amount. This is what separates "base salary of $120,000"
     from "$2,000,000 in revenue", "$300,000 research grant", "manages a
     $2,000,000 budget", or "save customers $100,000 annually". Bare period
     cues ("annually", "per year") are deliberately NOT pay context — they
     attach to non-pay figures just as readily.
  3. A resolvable disclosure: EITHER a resolvable salary *range* (via the same
     parser/normalizer the engine uses for real salary capture), OR a lone
     *figure* that is additionally labeled as base pay (``_BASE_PAY_ANCHOR_RE``).
     A bare currency figure without a base-pay label is too easily a bonus /
     equity / funding / budget number, so a single figure is trusted only when
     the sentence explicitly labels it ("base salary of $120,000").

A disclosed figure whose sentence ALSO quotes a non-base-pay trap keyword
(401k, sign-on/relocation bonus, equity, revenue/ARR, funding round, stipend/
per-diem) is flagged ``"ambiguous"`` rather than ``True`` — UNLESS the sentence
carries a base-pay label, in which case a trailing benefits mention
("base salary $120k-$150k, plus equity and 401k") does not make the labeled
base range ambiguous.
"""

from __future__ import annotations

import re

from jobcannon.engine.salary_normalizer import (
    MAX_PLAUSIBLE_ANNUAL,
    MIN_PLAUSIBLE_ANNUAL,
    RESOLVED_RESOLUTIONS,
    annualize,
    detect_period,
    normalize_observation,
    parse_salary_text,
)

# Gate 1: an explicit currency indicator (symbol, ISO code, or spelled-out
# word). Keeps the optional-currency range grammar from minting salaries out of
# bare numeric prose.
_CURRENCY_ANCHOR_RE = re.compile(
    r"[$£€₹]|\b(?:USD|CAD|EUR|GBP|AUD|SGD|INR|dollars?|euros?|pounds?)\b",
    re.IGNORECASE,
)

# Gate 2: positive compensation vocabulary. Bare period cues (annually / per
# year) are intentionally excluded — they co-occur with revenue/budget/savings
# figures too. Pay verbs (pay/paid/pays) ARE included for recall of phrasings
# like "this role pays $120k to $150k".
_PAY_CONTEXT_RE = re.compile(
    r"\b(?:salary|salaries|compensation|comp|remuneration|wage|wages"
    r"|base\s+pay|pay\s+range|pay\s+rate|hourly\s+rate|hourly\s+pay"
    r"|ote|on[-\s]target\s+earnings|pay|pays|paid|paying)\b",
    re.IGNORECASE,
)

# A lone currency-anchored figure ("$120,000 per year", "$120K"). Currency
# prefix REQUIRED so a bare number never registers. Used only when the sentence
# also carries a base-pay label (see _classify_sentence).
_SINGLE_FIGURE_RE = re.compile(
    r"(?:[$£€₹]|\b(?:USD|CAD|EUR|GBP|AUD|SGD|INR)\b)\s*"
    r"(?P<amt>\d[\d,]*\.?\d*)\s*(?P<unit>[KkMm])?(?![A-Za-z])",
    re.IGNORECASE,
)

_EXCLUSION_TRAP_RE = re.compile(
    r"401\(?k\)?|\bbonus(?:es)?\b|\bcommission\b|sign[- ]on bonus|signing bonus"
    r"|relocation (?:bonus|assistance|package)"
    r"|equity|RSUs?|stock options?|\bARR\b|annual revenue|revenue growth"
    r"|series [a-e]\b|\bstipend\b|per diem",
    re.IGNORECASE,
)

# A base-pay LABEL that ties a figure to base salary. Deliberately narrow: only
# noun phrases that genuinely label a figure as base pay — NOT a bare "salary
# is" / "salary of", which fire on "salary is not the focus ... $200k equity"
# and would wrongly exempt the trap / mint a single-figure disclosure.
_BASE_PAY_ANCHOR_RE = re.compile(
    r"\b(?:base\s+(?:salary|pay|compensation)|salary\s+range|annual\s+salary"
    r"|starting\s+salary|compensation\s+range|pay\s+range)\b",
    re.IGNORECASE,
)

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n\s*\n")


def _single_figure_disclosed(sentence: str) -> bool:
    """True when the sentence states a lone, currency-anchored, plausible pay figure."""
    match = _SINGLE_FIGURE_RE.search(sentence)
    if match is None:
        return False
    try:
        value = float(match.group("amt").replace(",", ""))
    except ValueError:
        return False
    unit = (match.group("unit") or "").upper()
    if unit == "K":
        value *= 1_000
    elif unit == "M":
        value *= 1_000_000
    annual = annualize(value, detect_period(sentence))
    return MIN_PLAUSIBLE_ANNUAL <= annual <= MAX_PLAUSIBLE_ANNUAL


def _classify_sentence(sentence: str) -> dict | None:
    """Classify one currency-anchored sentence; None when it discloses no pay."""
    if not _PAY_CONTEXT_RE.search(sentence):
        return None
    has_label = bool(_BASE_PAY_ANCHOR_RE.search(sentence))
    obs = parse_salary_text(sentence, provenance="jd_regex")
    range_disclosed = (
        obs is not None and normalize_observation(obs).resolution in RESOLVED_RESOLUTIONS
    )
    single_disclosed = has_label and _single_figure_disclosed(sentence)
    if not (range_disclosed or single_disclosed):
        return None
    if _EXCLUSION_TRAP_RE.search(sentence) and not has_label:
        return {
            "value": "ambiguous",
            "method": "regex_grammar",
            "candidate_sentence": sentence[:200],
        }
    return {"value": True, "method": "regex_grammar"}


def score_comp_transparency(salary_min: object, salary_max: object, jd_full: str | None) -> dict:
    if salary_min is not None or salary_max is not None:
        return {"value": True, "method": "structured"}
    if not jd_full:
        return {"value": False, "method": "regex_grammar"}
    for sentence in _SENT_SPLIT_RE.split(jd_full):
        if not _CURRENCY_ANCHOR_RE.search(sentence):
            continue
        verdict = _classify_sentence(sentence)
        if verdict is not None:
            return verdict
    return {"value": False, "method": "regex_grammar"}

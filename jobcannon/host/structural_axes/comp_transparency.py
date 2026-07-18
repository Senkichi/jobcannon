"""Comp-transparency structural axis: does the posting state real pay?

Reuses the engine's salary grammar (``jobcannon.engine.salary_normalizer``)
as a READ-ONLY classifier — this module NEVER writes ``salary_min``/
``salary_max``; those columns have a single writer
(``jobcannon.db._jobs.upsert_job``, via the capture-site salary functions).

A structured salary already present on the row wins outright (``"structured"``
method, unambiguous ``True``). Otherwise the JD body is scanned sentence-by-
sentence. A sentence is only considered a pay disclosure when it carries an
explicit **currency anchor** (a ``$``/``£``/``€``/``₹`` symbol or a
``USD``/``GBP``/... code): a bare numeric range with no currency ("supports
200-500 users daily", "serves 100K-500K users") is headcount/scale prose, not
pay, and the underlying range grammar — whose currency prefix is optional —
would otherwise misread it as a salary once an unrelated period word
("daily") lets the salvage ladder annualize it into the plausibility window.

Within a currency-anchored sentence we accept either a resolvable *range* (via
the same parser/normalizer the engine uses for real salary capture) or a lone
currency-anchored *figure* ("base salary of $120,000 per year"), so a
single-value disclosure is not silently missed. A disclosed figure whose
sentence ALSO quotes a non-base-pay trap keyword (401k, sign-on/relocation
bonus, equity, revenue/ARR, funding round, stipend/per-diem) is flagged
``"ambiguous"`` rather than ``True`` — UNLESS the sentence explicitly anchors
the figure as base pay ("base salary", "salary range", ...), in which case a
trailing benefits mention ("$120k-$150k, plus equity and 401k match") does not
make the base-pay range itself ambiguous.
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

# A sentence discloses pay only if it carries an explicit currency indicator.
# Bare numeric ranges ("200-500 users") and bare magnitudes ("100K-500K users")
# are NOT pay — requiring a currency symbol/code is the single anchor that keeps
# the optional-currency range grammar from minting salaries out of scale prose.
_CURRENCY_ANCHOR_RE = re.compile(r"[$£€₹]|\b(?:USD|CAD|EUR|GBP|AUD|SGD|INR)\b", re.IGNORECASE)

# A lone currency-anchored figure ("$120,000 per year", "$120K annually"). The
# currency prefix is REQUIRED here (unlike the engine range grammar) so a bare
# single number can never register as pay.
_SINGLE_FIGURE_RE = re.compile(
    r"(?:[$£€₹]|\b(?:USD|CAD|EUR|GBP|AUD|SGD|INR)\b)\s*"
    r"(?P<amt>\d[\d,]*\.?\d*)\s*(?P<unit>[KkMm])?(?![A-Za-z])",
    re.IGNORECASE,
)

_EXCLUSION_TRAP_RE = re.compile(
    r"401\(?k\)?|sign[- ]on bonus|signing bonus|relocation (?:bonus|assistance|package)"
    r"|equity|RSUs?|stock options?|\bARR\b|annual revenue|revenue growth"
    r"|series [a-e]\b|\bstipend\b|per diem",
    re.IGNORECASE,
)

# When the sentence explicitly frames the figure as base pay, a trailing
# benefits mention does not make it ambiguous — the range IS the base salary.
_BASE_PAY_ANCHOR_RE = re.compile(
    r"\b(base (?:salary|pay|compensation)|salary range|salary of|annual salary"
    r"|starting salary|pay range|compensation range|salary is)\b",
    re.IGNORECASE,
)

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


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
    obs = parse_salary_text(sentence, provenance="jd_regex")
    disclosed = obs is not None and normalize_observation(obs).resolution in RESOLVED_RESOLUTIONS
    if not disclosed:
        disclosed = _single_figure_disclosed(sentence)
    if not disclosed:
        return None
    if _EXCLUSION_TRAP_RE.search(sentence) and not _BASE_PAY_ANCHOR_RE.search(sentence):
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

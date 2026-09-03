# PORTED from job_finder/db/_classification.py @ 9d063804c86d76dec470dfb221db44fe7e716be3 (private job-cannon). Ledger L-0003.
"""v3.0 ordinal scoring — JobAssessment dataclass + Python-derived classification rule.

Pure rule logic. No DB side-effects. In the private repo this module lived at
`job_finder/db/_classification.py` and was re-exported via `job_finder/db/__init__.py`
for callers that persist assessments; here it is the canonical home, imported
directly as `jobcannon.engine.classification`.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass

from jobcannon.engine.constants import SUB_SCORE_KEYS as _SUB_SCORE_KEYS
from jobcannon.engine.enrichment_states import LOW_SIGNAL_TERMINAL

# Canonical sub-score key order (matches CONTEXT D-05 and the v3 scoring prompt's
# JSON schema). Used for JSON serialization stability and for derive_classification.
# Single source of truth is jobcannon.engine.constants.SUB_SCORE_KEYS; this private
# alias preserves the historical `from ._classification import _SUB_SCORE_KEYS`
# import surface (db/__init__.py re-export, _assessment_writer).

# Tiers from which no further automatic enrichment will run AND the JD is genuinely
# unobtainable. A job at one of these tiers with a short JD has no signal ->
# low_signal, not a rubric-noise reject. Single source of truth lives in
# jobcannon.engine.enrichment_states.LOW_SIGNAL_TERMINAL (F1 fix); aliased here as a
# string frozenset (StrEnum members compare equal to their string values, so
# membership tests against raw enrichment_tier strings are unchanged).
_TERMINAL_ENRICHMENT_TIERS: frozenset[str] = frozenset(LOW_SIGNAL_TERMINAL)

# Positive-evidence thresholds for the "apply" verdict. On the 1-5
# ordinal scale, 3 means "neutral / couldn't tell" — the *absence of weakness*,
# not the *presence of strength*. "apply" (the strongest positive class, the one
# the user acts on) must require affirmative fit evidence: a minimum mean AND a
# minimum count of genuinely strong (>= 4) axes. Defaults are overridable via
# config.scoring.apply_mean_floor / scoring.apply_min_strong_axes, threaded
# through persist_job_assessment the same way low_signal_threshold is.
DEFAULT_APPLY_MEAN_FLOOR: float = 3.5
DEFAULT_APPLY_MIN_STRONG_AXES: int = 3
# An axis is "strong" when it carries positive (not merely non-negative) signal.
_STRONG_AXIS_FLOOR: int = 4

# Substitution marker for an excluded axis (see derive_classification's
# excluded_axes). Exclusion works by substitution, never key removal — the
# six-key domain guard forbids five-key dicts — so the marker must itself be
# a valid ordinal (int in 1..5) to pass the value guard. It is the neutral
# midpoint so an excluded slot can never trip the any-axis-1 reject. The mean
# and strong-axis computations consult the excluded-axis set, so the marker
# never contributes to them; the flat-neutral tell instead reads the RAW
# pre-substitution vector, so a marker can neither manufacture an all-3s
# vector nor suppress the tell on a genuinely flat one.
_EXCLUDED_AXIS_MARKER: int = 3

# Neutral midpoint of the 1-5 ordinal scale. ``comp_fit`` is forced to this
# value when a row carries no parsed compensation signal (issue #1969):
# ``comp_fit`` is a comparison against *stated* compensation, and with nothing
# stated there is nothing to compare against, so a non-neutral score is
# unsupported by construction. This is not a new product decision — 3 is the
# de-facto production behaviour on the majority of no-salary postings and the
# value the nightly auditor assumes — it is only codified as a named constant
# so the override site reads its intent instead of a magic number.
COMP_FIT_NEUTRAL: int = 3

# Classification rule-version stamp. Bumped every time the rule in
# ``derive_classification`` (or the thresholds that feed it) changes in a way
# that can invalidate already-derived verdicts. In the private original,
# ``persist_job_assessment`` stamps this onto a ``jobs.classification_rule_version``
# column at write time so a rule change makes the affected cohort immediately
# enumerable via ``WHERE classification_rule_version < CLASSIFICATION_RULE_VERSION``
# instead of being inferred later from a health signal. That write path (the
# column, its migration, and the redrive sweep) has no public counterpart yet —
# this port carries only the version constant itself, so a future hosted
# persistence layer has a stable value to stamp and this module's own history
# of its rule is recorded in one place.
#
# Version history:
#   1 — the rule as ported: low_signal branch + positive-evidence "apply" gate
#       + location-policy effective_location_fit. Bump to 2 (3, ...) on the
#       NEXT rule change that can invalidate stored verdicts.
CLASSIFICATION_RULE_VERSION: int = 1


def is_non_degenerate_low_signal(
    sub_scores: dict,
    enrichment_tier: str | None,
    jd_full_length: int,
    low_signal_threshold: int,
) -> bool:
    """Return True if the row is low_signal for a non-degenerate reason.

    Two non-degenerate paths to ``low_signal``:
      1. enrichment is exhausted (a terminal tier) AND the full JD is shorter
         than the low_signal threshold — the model has no reliable text to score.
      2. every sub-score is exactly the neutral midpoint (3) — the model did not
         discriminate on any axis.

    This helper is intentionally separate from ``derive_classification`` so the
    backfill/reconciliation paths can reuse the same rule without duplicating it.
    """
    if enrichment_tier in _TERMINAL_ENRICHMENT_TIERS and jd_full_length < low_signal_threshold:
        return True
    return all(v == 3 for v in sub_scores.values())


def get_effective_location_fit(verdict_json: str | None) -> int | None:
    """Extract ``effective_location_fit`` from a serialized LocationPolicy
    verdict, or None when absent/malformed.

    This is the single parsing point for the policy override. A missing,
    empty, malformed, or non-integer value (including bool and float) is
    treated as ``None``: callers that display the value to the auditor must
    not silently substitute the raw LLM ``location_fit`` for a policy value
    that does not exist.
    """
    if not verdict_json:
        return None
    try:
        data = json.loads(verdict_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("effective_location_fit")
    # bool is an int subclass; exclude it the same way derive_classification does.
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def effective_sub_scores(
    sub_scores: dict,
    location_policy_verdict_json: str | None,
) -> dict:
    """Return ``sub_scores`` with ``location_fit`` swapped to the policy's effective value.

    Single enforcement point for the raw-vs-effective ``location_fit`` split:
    in the private original, the assessment writer
    stores the LLM's raw ``location_fit`` in ``sub_scores_json`` (so the UI
    can show what the model actually said) but derives classification from
    the location policy's ``effective_location_fit``. This schema has no
    ``sub_scores_json`` column yet (see ``jobcannon/db/compat.py``); the
    helper is ported ahead of that writer so any future consumer that
    re-derives classification from stored sub-scores consults the stored
    location-policy verdict — skipping that consultation systematically
    disagrees with the stored classification for every policy-adjusted row.

    This helper is that consultation. Call it on the parsed ``sub_scores`` dict
    plus the raw serialized verdict before passing the result to
    ``derive_classification``. When no verdict is stored (None, empty,
    malformed, or missing/invalid ``effective_location_fit``) the original dict
    is returned unchanged — the no-policy case classifies identically either way.

    Args:
        sub_scores: parsed ``sub_scores_json`` dict (the raw LLM sub-scores).
        location_policy_verdict_json: serialized location-policy verdict JSON
            string, or None when the row has no stored verdict.

    Returns:
        A new dict with ``location_fit`` replaced by the verdict's
        ``effective_location_fit`` when a valid verdict is present, otherwise
        the original ``sub_scores`` dict unchanged.
    """
    effective = get_effective_location_fit(location_policy_verdict_json)
    if effective is None:
        return sub_scores
    return {**sub_scores, "location_fit": effective}


@dataclass(frozen=True)
class JobAssessment:
    """Unified v3.0 scoring result. Replaces HaikuScore + SonnetScore pair.

    Per CONTEXT D-05 (Phase 34):

      sub_scores: dict[str, int] with 6 keys (title_fit, location_fit, comp_fit,
          domain_match, seniority_match, skills_match) — each 1-5 integer.
      classification: one of apply|consider|skip|reject. Typically a sentinel
          empty string at construction time; derive_classification() at persist
          time computes the authoritative value (see D-06 rule and D-07 note
          that legitimacy_note is read from the jobs row, not from the LLM).
      rationale: dict with keys strengths, gaps, talking_points,
          resume_priority_skills (each a list[str]); serialized to the reused
          fit_analysis column per D-08.
      provider: cascade-attribution string (e.g., "ollama", "anthropic") or None.
      degenerate: quality-floor flag. True only when EVERY provider
          in the cascade returned a no-signal (uniform axes + empty rationale)
          assessment, so the dispatcher accepted one flagged rather than
          raising. derive_classification routes a degenerate assessment to
          "low_signal" instead of fabricating an apply/consider verdict.
      comp_fit_override: issue #1969 precondition record. Set by ``score_job``
          when the row carries no parsed compensation signal — ``comp_fit`` is
          forced to the neutral midpoint (3) and this dict records the reason,
          the forced value, and the model's original value so the decision is
          auditable on the row (persisted into ``fit_analysis``) rather than a
          silent overwrite. None means no override was applied (the model's
          ``comp_fit`` is used as-is).
    """

    sub_scores: dict
    classification: str
    rationale: dict
    provider: str | None = None
    degenerate: bool = False
    comp_fit_override: dict | None = None


def derive_classification(
    sub_scores: dict,
    legitimacy_note: str | None,
    enrichment_tier: str | None = None,
    jd_full_length: int = 0,
    low_signal_threshold: int = 1500,
    apply_mean_floor: float = DEFAULT_APPLY_MEAN_FLOOR,
    apply_min_strong_axes: int = DEFAULT_APPLY_MIN_STRONG_AXES,
    *,
    degenerate: bool = False,
    excluded_axes: Collection[str] = (),
) -> str:
    """Python-derived 5-way classification — NOT LLM-emitted (CONTEXT D-06, anti-pattern 3).

    Rule precedence (per spec D-2.5):
      1. legitimacy_note truthy            -> "reject"
      2. degenerate                        -> "low_signal"
      3. enrichment exhausted + short jd   -> "low_signal"
      4. flat-neutral vector (all == 3)    -> "low_signal"
      5. any sub-score == 1                -> "reject"
      6. positive evidence                 -> "apply"
      7. all sub-scores >= 2               -> "consider"
      8. otherwise                         -> "skip"

    The ``degenerate`` branch handles the all-providers-degenerate
    case: when every provider in the cascade returned a uniform no-signal axis
    vector, the dispatcher accepts one flagged ``degenerate=True``. Such a
    vector carries no real signal, so it must NOT be allowed to classify as
    ``apply`` (uniform 5s) or ``consider``. It sits AFTER the legitimacy reject
    (a flagged scam is still a reject) and is independent of the
    enrichment/jd-length low_signal rule — a degenerate score is no-signal even
    with a long JD. Composes with the upstream quality floor as belt-and-braces.

    The low_signal branch surfaces genuinely-no-signal jobs (enrichment cascade
    exhausted AND jd_full below threshold) honestly instead of rolling them
    into apply/consider/skip via unreliable rubric outputs. The branch sits
    BEFORE the any-axis-1 reject check on purpose: a job with insufficient JD
    text cannot be confidently rejected on rubric outputs (the 1 itself may be
    a hallucination from the model scoring against an empty prompt).

    The flat-neutral branch (3) is branch (C): on the 1-5
    scale 3 means "couldn't tell", so a vector that is degenerate at the neutral
    midpoint (all six axes present AND all == 3) is a strong tell the model did
    not discriminate. It is surfaced as low_signal honestly, independent of JD
    length and enrichment_tier — which also covers the agentic-tier cohort that
    the exact-string enrichment match in branch 2 misses.

    The "apply" branch (5) is branch (B): "apply" is the strongest
    positive class (the one the user acts on) and must require the *presence of
    strength*, not merely the *absence of weakness*. It fires only when no axis
    is weak (all >= 3), at least ``apply_min_strong_axes`` axes are strong
    (>= 4), AND the mean is at least ``apply_mean_floor``. An all-3s vector
    (mean 3.0, 0 strong axes) never reaches here — it is caught by branch 3 —
    and near-neutral vectors like {4,3,3,3,3,3} fall through to "consider".

    Partial-vector defense (couples with ``_coerce_assessment``'s fail-closed
    coercion in ``job_scorer.py``): the domain guard below requires all
    six canonical keys before any sub-score branch runs, so a vector missing an
    axis raises ValueError rather than reaching "apply" over a partial dict.

    For integer 1-5 sub-scores, branch 7 ("skip") is effectively unreachable —
    any value below 2 is 1, which already triggered reject at branch 4. The
    branch remains for defense-in-depth against future sub-score domain changes
    (e.g., 0 added as a sentinel).

    Args:
        sub_scores: dict of the 6 ordinal sub-scores (1-5 integers).
        legitimacy_note: value of the jobs.legitimacy_note column; truthy means
            ingestion-time scam/exclusion detection flagged this row.
        enrichment_tier: value of jobs.enrichment_tier ('free' | 'ddg' | 'low'
            | 'serpapi' | 'mid' | 'exhausted' | 'agentic' | 'agentic_exhausted'
            | 'expired' | None). Only terminal tiers (those in
            _TERMINAL_ENRICHMENT_TIERS: 'exhausted', 'agentic',
            'agentic_exhausted', 'expired') participate in the low_signal rule;
            other tiers are still re-enrichment candidates.
        jd_full_length: character length of jobs.jd_full (0 when NULL).
        low_signal_threshold: jd_full_length below this triggers low_signal
            when enrichment is exhausted. Configurable via
            scoring.low_signal_jd_chars.
        apply_mean_floor: minimum mean across the six axes for an "apply"
            verdict. Configurable via scoring.apply_mean_floor (default 3.5).
        apply_min_strong_axes: minimum count of strong axes (>= 4) for an
            "apply" verdict. Configurable via scoring.apply_min_strong_axes
            (default 3).
        degenerate: flag from JobAssessment.degenerate. True only
            when the cascade quality floor accepted an all-providers-degenerate
            result. Routes to low_signal (no-signal vector, never apply).
        excluded_axes: axis names (a strict subset of the six canonical keys)
            that a policy layer has ruled non-scorable for this row — e.g.
            ``location_fit`` for a profile carrying no location constraint,
            where the model scored the axis against nothing and a 1 there is
            noise, not evidence. Exclusion is substitution, never key removal:
            the domain guard requires all six keys, so each excluded axis's
            value is replaced (in a NEW dict; the input is never mutated) by
            ``_EXCLUDED_AXIS_MARKER`` — a valid neutral ordinal, so the value
            guard holds and the any-axis-1 reject can never fire from an
            excluded slot. The mean, strong-axis count, and apply/consider
            all-of checks then iterate over NON-excluded axes only, so the
            marker never contributes arithmetically; and the flat-neutral tell
            requires zero exclusions, because a substituted neutral could
            manufacture an all-3s vector out of a real verdict. The three
            thresholds (apply_mean_floor, apply_min_strong_axes,
            _STRONG_AXIS_FLOOR) are deliberately NOT recalibrated for the
            smaller divisor. Raw values of excluded axes are still validated
            (garbage is rejected even in an excluded slot). Excluding every
            axis raises ValueError — nothing would remain to classify on.

    Returns:
        One of "reject", "low_signal", "apply", "consider", "skip".
    """
    if legitimacy_note:
        return "reject"
    if degenerate:
        return "low_signal"
    if enrichment_tier in _TERMINAL_ENRICHMENT_TIERS and jd_full_length < low_signal_threshold:
        return "low_signal"

    # Domain guard: reject malformed sub-score dicts loudly rather than
    # silently classifying garbage as "apply" (e.g. empty dict passes
    # all(v >= 3 ...) vacuously). bool is an int subclass and is excluded
    # because True/False are not ordinal scores.
    _expected = set(_SUB_SCORE_KEYS)
    _actual = set(sub_scores)
    if _actual != _expected:
        _missing = _expected - _actual
        _extra = _actual - _expected
        parts: list[str] = []
        if _missing:
            parts.append(f"missing keys: {sorted(_missing)}")
        if _extra:
            parts.append(f"extra keys: {sorted(_extra)}")
        raise ValueError(f"sub_scores has wrong keys — {'; '.join(parts)}")
    _bad = {
        k: v
        for k, v in sub_scores.items()
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 5)
    }
    if _bad:
        raise ValueError(f"sub_scores values must be int in 1..5 (got {_bad})")

    # Captured before marker substitution: the flat-neutral tell below reads
    # the caller's raw vector, never substituted markers.
    _raw_values = list(sub_scores.values())

    # Axis exclusion: substitution-with-a-marker plus a parallel excluded-axis
    # set — NEVER key removal (the domain guard above forbids five-key dicts,
    # deliberately: a partial vector must not classify). Runs AFTER the guards
    # so raw values are fully validated even in excluded slots, and builds a
    # new dict so the caller's input is never mutated. Substituting the
    # neutral marker (rather than merely ignoring the slot) is what makes the
    # any-axis-1 reject below safe by construction: no caller can pass a raw
    # 1 in an excluded axis and still trigger a reject from it.
    _excluded = frozenset(excluded_axes)
    if _excluded:
        _unknown = _excluded - _expected
        if _unknown:
            raise ValueError(f"excluded_axes has unknown axes: {sorted(_unknown)}")
        if _excluded == _expected:
            raise ValueError("excluded_axes cannot name all six axes — nothing left to classify")
        sub_scores = {**sub_scores, **dict.fromkeys(_excluded, _EXCLUDED_AXIS_MARKER)}

    # Branch (C): flat-neutral vector -> low_signal. All six axes
    # at the neutral midpoint means the model did not discriminate; surface it
    # honestly rather than promoting it. Runs before the any-axis-1 reject and
    # the apply branch; independent of JD length / enrichment_tier. The tell
    # reads _raw_values — the vector BEFORE marker substitution — which cuts
    # both ways: a substituted marker cannot manufacture an all-3s vector out
    # of a vector that carried a real (excluded) signal, and a vector the
    # model genuinely scored flat stays low_signal under any exclusion set.
    # Reading raw scores also keeps this tell consistent with
    # is_non_degenerate_low_signal, which reads the same raw vector and takes
    # no excluded_axes.
    if all(v == 3 for v in _raw_values):
        return "low_signal"

    # Reads the substituted vector: an excluded slot holds the neutral marker,
    # never a raw 1, so this can only fire from a non-excluded axis.
    _values = list(sub_scores.values())
    if any(v == 1 for v in _values):
        return "reject"

    # Branch (B): "apply" requires affirmative fit evidence, not
    # merely the absence of weakness. No weak axis (all >= 3), enough strong
    # axes (>= 4), AND a mean at or above the floor. Computed over NON-excluded
    # axes only: a substituted exclusion marker never contributes to the
    # strong-axis count or the mean.
    _included = [v for k, v in sub_scores.items() if k not in _excluded]
    _strong_axes = sum(1 for v in _included if v >= _STRONG_AXIS_FLOOR)
    _mean = sum(_included) / len(_included)
    if (
        all(v >= 3 for v in _included)
        and _strong_axes >= apply_min_strong_axes
        and _mean >= apply_mean_floor
    ):
        return "apply"

    if all(v >= 2 for v in _included):
        return "consider"
    return "skip"

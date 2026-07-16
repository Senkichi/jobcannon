"""v3.0 ordinal scoring — JobAssessment dataclass + Python-derived classification rule.

Pure rule logic. No DB side-effects. In the private repo this module lived at
`job_finder/db/_classification.py` and was re-exported via `job_finder/db/__init__.py`
for callers that persist assessments; here it is the canonical home, imported
directly as `jobcannon.engine.classification`.
"""

from __future__ import annotations

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

# Positive-evidence thresholds for the "apply" verdict (issue #210). On the 1-5
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
      degenerate: issue #227 quality-floor flag. True only when EVERY provider
          in the cascade returned a no-signal (uniform axes + empty rationale)
          assessment, so the dispatcher accepted one flagged rather than
          raising. derive_classification routes a degenerate assessment to
          "low_signal" instead of fabricating an apply/consider verdict.
    """

    sub_scores: dict
    classification: str
    rationale: dict
    provider: str | None = None
    degenerate: bool = False


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
) -> str:
    """Python-derived 5-way classification — NOT LLM-emitted (CONTEXT D-06, anti-pattern 3).

    Rule precedence (per spec D-2.5; positive-evidence rule per issue #210):
      1. legitimacy_note truthy            -> "reject"
      2. degenerate (issue #227)           -> "low_signal"
      3. enrichment exhausted + short jd   -> "low_signal"
      4. flat-neutral vector (all == 3)    -> "low_signal"
      5. any sub-score == 1                -> "reject"
      6. positive evidence                 -> "apply"
      7. all sub-scores >= 2               -> "consider"
      8. otherwise                         -> "skip"

    The ``degenerate`` branch (issue #227) handles the all-providers-degenerate
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

    The flat-neutral branch (3) is the issue #210 fix's branch (C): on the 1-5
    scale 3 means "couldn't tell", so a vector that is degenerate at the neutral
    midpoint (all six axes present AND all == 3) is a strong tell the model did
    not discriminate. It is surfaced as low_signal honestly, independent of JD
    length and enrichment_tier — which also covers the agentic-tier cohort that
    the exact-string enrichment match in branch 2 misses (issue #225).

    The "apply" branch (5) is issue #210's branch (B): "apply" is the strongest
    positive class (the one the user acts on) and must require the *presence of
    strength*, not merely the *absence of weakness*. It fires only when no axis
    is weak (all >= 3), at least ``apply_min_strong_axes`` axes are strong
    (>= 4), AND the mean is at least ``apply_mean_floor``. An all-3s vector
    (mean 3.0, 0 strong axes) never reaches here — it is caught by branch 3 —
    and near-neutral vectors like {4,3,3,3,3,3} fall through to "consider".

    Partial-vector defense (couples #227): the domain guard below requires all
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
            | None). Only terminal tiers (those in _TERMINAL_ENRICHMENT_TIERS:
            'exhausted', 'agentic', 'agentic_exhausted') participate in the
            low_signal rule; other tiers are still re-enrichment candidates.
        jd_full_length: character length of jobs.jd_full (0 when NULL).
        low_signal_threshold: jd_full_length below this triggers low_signal
            when enrichment is exhausted. Configurable via
            scoring.low_signal_jd_chars.
        apply_mean_floor: minimum mean across the six axes for an "apply"
            verdict. Configurable via scoring.apply_mean_floor (default 3.5).
        apply_min_strong_axes: minimum count of strong axes (>= 4) for an
            "apply" verdict. Configurable via scoring.apply_min_strong_axes
            (default 3).
        degenerate: issue #227 flag from JobAssessment.degenerate. True only
            when the cascade quality floor accepted an all-providers-degenerate
            result. Routes to low_signal (no-signal vector, never apply).

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

    # Branch (C): flat-neutral vector -> low_signal (issue #210). All six axes
    # at the neutral midpoint means the model did not discriminate; surface it
    # honestly rather than promoting it. Runs before the any-axis-1 reject and
    # the apply branch; independent of JD length / enrichment_tier. The domain
    # guard above guarantees all six keys are present here.
    _values = list(sub_scores.values())
    if all(v == 3 for v in _values):
        return "low_signal"

    if any(v == 1 for v in _values):
        return "reject"

    # Branch (B): "apply" requires affirmative fit evidence (issue #210), not
    # merely the absence of weakness. No weak axis (all >= 3), enough strong
    # axes (>= 4), AND a mean at or above the floor.
    _strong_axes = sum(1 for v in _values if v >= _STRONG_AXIS_FLOOR)
    _mean = sum(_values) / len(_values)
    if (
        all(v >= 3 for v in _values)
        and _strong_axes >= apply_min_strong_axes
        and _mean >= apply_mean_floor
    ):
        return "apply"

    if all(v >= 2 for v in _values):
        return "consider"
    return "skip"

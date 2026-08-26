"""Foundation-layer constants shared across all layers.

This module has no intra-package imports by design (it sits below db/, web/,
eval/, and scripts/), so any layer may import these vocabularies without a
cycle. Anything that enumerates the pipeline statuses, the six scoring axes,
or the classification verdicts MUST import from here rather than re-declaring
a literal — the guard tests (test_pipeline_status_*, test_scoring_vocabulary_*)
fail if a divergent copy is reintroduced.
"""

PIPELINE_STATUSES = (
    "discovered",
    "reviewing",
    "applied",
    "phone_screen",
    "technical",
    "onsite",
    "offer",
    "accepted",
    "archived",
    "rejected",
    "withdrawn",
    "dismissed",
)

VALID_PIPELINE_STATUSES = frozenset(PIPELINE_STATUSES)

# Canonical ordinal scoring axes — the six 1-5 sub-scores the v3 rubric emits
# at the TOP LEVEL of the assessment (NOT nested under "sub_scores"). Order
# matches the v3 prompt schema + CONTEXT D-05 and is load-bearing for JSON
# serialization stability and report column order. The frozen production
# schema (web/scoring_prompts/v3_scoring_prompt.JOB_ASSESSMENT_SCHEMA) is NOT
# derived from this (it must stay byte-stable for eval reproducibility) but is
# pinned against it by tests/test_scoring_vocabulary_single_source.py.
SUB_SCORE_KEYS: tuple[str, ...] = (
    "title_fit",
    "location_fit",
    "comp_fit",
    "domain_match",
    "seniority_match",
    "skills_match",
)

# Canonical classification verdicts — the universe derive_classification() can
# return. Order is the confusion-matrix / report axis order (do not reorder
# without re-pinning the eval reports). "low_signal" is the honest no-signal
# class, distinct from a confident "reject".
CLASSIFICATIONS: tuple[str, ...] = (
    "apply",
    "consider",
    "skip",
    "reject",
    "low_signal",
)

# Minimum jd_full length required for gold-set labeling and snapshot freezing.
# Rows below this threshold are rejected by the labeler and retired by the
# migration. Single-sourced across migration, labeler, and heal script.
MIN_GOLD_JD_CHARS = 500

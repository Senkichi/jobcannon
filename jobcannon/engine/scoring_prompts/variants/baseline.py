"""Variant 'baseline': aliases v3_scoring_prompt for harness consistency.

The harness loads variants by name; baseline is the production prompt
unchanged. This module exists so variant-loading code is uniform — every
variant is reachable through ``scoring_prompts.variants.<name>``, including
the production reference.

Per the Task 4.1 wiring, _resolve_variant_module("baseline") short-circuits
to v3_scoring_prompt directly (skipping this re-export module). This file
is kept for the test_variants_loadable contract — it makes "baseline" a
discoverable name when listing the variants directory.
"""

from jobcannon.engine.scoring_prompts.v3_scoring_prompt import (
    FEWSHOT_EXAMPLES,
    FIELD_REINFORCEMENT,
    JOB_ASSESSMENT_SCHEMA,
    V3_SCORING_PROMPT,
    V3_SCORING_PROMPT_HEADER,
)

__all__ = [
    "FEWSHOT_EXAMPLES",
    "FIELD_REINFORCEMENT",
    "JOB_ASSESSMENT_SCHEMA",
    "V3_SCORING_PROMPT",
    "V3_SCORING_PROMPT_HEADER",
]

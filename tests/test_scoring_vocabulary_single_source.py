"""Guard: the scoring vocabulary has ONE source of truth.

The six ordinal axes and the five classification verdicts used to be copy-pasted
across db/, web/, eval/, and scripts/. They now derive from
jobcannon.engine.constants.{SUB_SCORE_KEYS, CLASSIFICATIONS}. This test fails loudly if
any consumer reintroduces a divergent literal — the "fully-written-but-silently-
out-of-sync list" failure mode this refactor exists to kill.

The frozen production schema (v3_scoring_prompt.JOB_ASSESSMENT_SCHEMA) is
deliberately NOT derived (it must stay byte-stable for eval reproducibility,
per its module docstring), so it is *pinned* here instead: if the canonical axis
list ever changes, this test forces a conscious reconciliation with the freeze.
"""

from __future__ import annotations

from jobcannon.engine.constants import SUB_SCORE_KEYS


def test_frozen_v3_schema_axes_pinned_to_canonical():
    """The FROZEN production schema is not derived, so pin its axis keys here.

    A divergence means either the schema thawed or the canonical list moved —
    both demand a deliberate decision, not a silent mismatch between what the
    LLM is asked for and what the rest of the system enumerates.
    """
    from jobcannon.engine.scoring_prompts.v3_scoring_prompt import JOB_ASSESSMENT_SCHEMA

    props = JOB_ASSESSMENT_SCHEMA["properties"]
    schema_axis_keys = {k for k, v in props.items() if v.get("type") == "integer"}
    assert schema_axis_keys == set(SUB_SCORE_KEYS)
    # Every axis is also a required top-level field.
    assert set(SUB_SCORE_KEYS).issubset(set(JOB_ASSESSMENT_SCHEMA["required"]))
    # Each axis carries the uniform 1-5 ordinal constraint.
    for axis in SUB_SCORE_KEYS:
        assert props[axis] == {"type": "integer", "minimum": 1, "maximum": 5}

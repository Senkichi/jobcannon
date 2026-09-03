# PORTED from tests/test_v5_domain_skills_anchor.py @ 7b8d57012bdc1283264b5b286b79ec2d44df4211 (private job-cannon). Ledger L-0262.
"""Tests for the v5_domain_skills_anchor variant's prompt structure.

Covers the variant module's own content: domain_match / skills_match
decision-rule replacement, negative few-shots, and schema shape. The
private original (issue #2009) also covered
``_stratified_sample_by_classification``, ``_apply_flip_down_rate``,
``any_axis_disagreement``, and ``--seed`` wiring through
``job_finder.eval.harness.run`` — the private eval/audit harness is not
part of the public engine, so those tests were dropped (see the PR body
for the full list and rationale), along with a variant-discoverability
test that asserted on ``job_finder.web.scoring_prompts.registry``
(also not ported).
"""

from __future__ import annotations


def test_v5_variant_exports_required_symbols():
    """The variant registry requires V3_SCORING_PROMPT, FIELD_REINFORCEMENT,
    FEWSHOT_EXAMPLES, JOB_ASSESSMENT_SCHEMA, and V3_SCORING_PROMPT_HEADER."""
    from jobcannon.engine.scoring_prompts.variants.v5_domain_skills_anchor import (
        FEWSHOT_EXAMPLES,
        FIELD_REINFORCEMENT,
        JOB_ASSESSMENT_SCHEMA,
        V3_SCORING_PROMPT,
        V3_SCORING_PROMPT_HEADER,
    )

    assert V3_SCORING_PROMPT
    assert V3_SCORING_PROMPT_HEADER
    assert FIELD_REINFORCEMENT
    assert FEWSHOT_EXAMPLES
    assert JOB_ASSESSMENT_SCHEMA["type"] == "object"
    assert set(JOB_ASSESSMENT_SCHEMA["required"]) == {
        "title_fit",
        "location_fit",
        "comp_fit",
        "domain_match",
        "seniority_match",
        "skills_match",
        "rationale",
        "legitimacy_note",
    }


def test_v5_domain_match_rule_replaces_baseline_anchor():
    """The v5 header's domain_match section must contain the decision-rule
    anchoring (default 3, named different domain ≤ 2), NOT the baseline's
    generic 'adjacent domain = 3, direct = 5' anchor."""
    from jobcannon.engine.scoring_prompts.variants._persona_corrected import (
        PERSONA_CORRECTED_HEADER as persona_header,
    )
    from jobcannon.engine.scoring_prompts.variants.v5_domain_skills_anchor import (
        V3_SCORING_PROMPT_HEADER as v5_header,
    )

    # The v5 header must contain the decision-rule language.
    assert "decision-rule anchored" in v5_header
    assert "DEFAULT" in v5_header  # "This is the DEFAULT when the JD carries no domain anchor"
    assert "DIFFERENT domain" in v5_header  # named different domain ≤ 2
    assert "NOT" in v5_header  # "Do NOT default to 4"

    # The baseline persona header's domain_match section must NOT carry
    # the v5 decision-rule language (sanity: we actually replaced it).
    domain_section_persona = persona_header[
        persona_header.index("### domain_match") : persona_header.index("### seniority_match")
    ]
    assert "decision-rule anchored" not in domain_section_persona
    assert "DEFAULT" not in domain_section_persona


def test_v5_skills_match_rule_replaces_baseline_anchor():
    """The v5 header's skills_match section must contain the required-only
    decision rule (nice-to-haves excluded, ≥2 absent caps at 3)."""
    from jobcannon.engine.scoring_prompts.variants.v5_domain_skills_anchor import (
        V3_SCORING_PROMPT_HEADER as v5_header,
    )

    assert "required-only decision rule" in v5_header
    assert "Nice-to-have" in v5_header
    assert "CAPPED at 3" in v5_header
    assert "absent" in v5_header.lower()


def test_v5_preserves_other_axes():
    """title_fit, location_fit, comp_fit, seniority_match, rationale structure,
    and legitimacy note must be unchanged from the persona-corrected baseline."""
    from jobcannon.engine.scoring_prompts.variants._persona_corrected import (
        PERSONA_CORRECTED_HEADER as persona_header,
    )
    from jobcannon.engine.scoring_prompts.variants.v5_domain_skills_anchor import (
        V3_SCORING_PROMPT_HEADER as v5_header,
    )

    for section in (
        "### title_fit",
        "### location_fit",
        "### comp_fit",
        "### seniority_match",
        "## Rationale structure",
        "## Legitimacy note",
    ):
        assert section in v5_header, f"v5 header missing {section!r}"

    # title_fit section should be byte-identical to the persona header.
    v5_tf = v5_header[v5_header.index("### title_fit") : v5_header.index("### location_fit")]
    p_tf = persona_header[
        persona_header.index("### title_fit") : persona_header.index("### location_fit")
    ]
    assert v5_tf == p_tf


def test_v5_negative_fewshots_present():
    """Two negative few-shots (domain-mismatch 4→2, skills-mismatch 4→2)
    must be in FEWSHOT_EXAMPLES."""
    from jobcannon.engine.scoring_prompts.variants.v5_domain_skills_anchor import (
        FEWSHOT_EXAMPLES,
    )

    # Domain-mismatch negative few-shot
    assert "Example 8" in FEWSHOT_EXAMPLES
    assert "NEGATIVE" in FEWSHOT_EXAMPLES
    assert "Quantitative Risk Analyst" in FEWSHOT_EXAMPLES
    assert '"domain_match": 2' in FEWSHOT_EXAMPLES

    # Skills-mismatch negative few-shot
    assert "Example 9" in FEWSHOT_EXAMPLES
    assert "Senior Data Engineer" in FEWSHOT_EXAMPLES
    assert '"skills_match": 2' in FEWSHOT_EXAMPLES
    assert "absent" in FEWSHOT_EXAMPLES.lower()


def test_v5_field_reinforcement_has_reminders():
    """FIELD_REINFORCEMENT must carry the v5 domain_match + skills_match
    reminders (default 3 not 4, required-only, ≥2 absent caps at 3)."""
    from jobcannon.engine.scoring_prompts.variants.v5_domain_skills_anchor import (
        FIELD_REINFORCEMENT,
    )

    assert "domain_match reminder (v5)" in FIELD_REINFORCEMENT
    assert "skills_match reminder (v5)" in FIELD_REINFORCEMENT
    assert "NOT 4" in FIELD_REINFORCEMENT
    assert "cap this axis at 3" in FIELD_REINFORCEMENT

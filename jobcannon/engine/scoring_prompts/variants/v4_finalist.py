# PORTED from job_finder/web/scoring_prompts/variants/v4_finalist.py @ b5b955bde5afd7e318be5d5a5b374c31db026a95 (private job-cannon). Ledger L-0261.
"""Variant v4_finalist — combines screening winners B3 + A2.

Deleted in #570 (5938a9c2, 2026-06-23) by a grep-based dead-code pass that
was blind to job_scorer._resolve_variant_module's importlib.import_module
dynamic loader; restored for #1819's auditor-label eval.

Composition (per .planning/eval_results/SCREENING-SUMMARY.md):
    - B3 (v4b3_evidence_quote): per-axis JD quote required; no quote
      forces score ≤ 2. Strongest single-variant signal (apply-FP -0.091,
      coherence -0.125, all 5 gates passed).
    - A2 (v4a2_mean_floor): aggregation note framing the apply-bar as
      "mean ≥ 3.5 AND no axis below 3". Cleanest per-axis MAE profile
      among A-dimension variants (no axis regressed). Anchor lock-in
      failed in isolation (Latent still rated apply because the model
      pushed averages up to compensate for one weak axis), but this
      finalist couples it with B3's evidence-grounding which prevents
      the high scores in the first place.

Hypothesis: dimensions are independent. B3 attacks per-axis manufactured
confidence (RC3) by gating high scores on JD evidence; A2 reframes the
classification rule perception so the model does not assume "all-≥-3 =
apply" and stops flattening scores upward.

Schema: B3's shape — additive 'evidence_quotes' object on rationale.
A2 contributes prompt text only, no schema changes.

Few-shots: persona-corrected baseline + B3's evidence-quote example,
preserving the calibration set the screening winner was tested on.
"""

from __future__ import annotations

from jobcannon.engine.scoring_prompts.variants._persona_corrected import (
    PERSONA_CORRECTED_FEWSHOT_EXAMPLES,
    PERSONA_CORRECTED_FIELD_REINFORCEMENT,
    PERSONA_CORRECTED_HEADER,
)

__all__ = [
    "FEWSHOT_EXAMPLES",
    "FIELD_REINFORCEMENT",
    "JOB_ASSESSMENT_SCHEMA",
    "V3_SCORING_PROMPT",
    "V3_SCORING_PROMPT_HEADER",
]


# B3 schema: 'rationale.evidence_quotes' is an additive optional property.
JOB_ASSESSMENT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title_fit",
        "location_fit",
        "comp_fit",
        "domain_match",
        "seniority_match",
        "skills_match",
        "rationale",
        "legitimacy_note",
    ],
    "properties": {
        "title_fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "location_fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "comp_fit": {"type": "integer", "minimum": 1, "maximum": 5},
        "domain_match": {"type": "integer", "minimum": 1, "maximum": 5},
        "seniority_match": {"type": "integer", "minimum": 1, "maximum": 5},
        "skills_match": {"type": "integer", "minimum": 1, "maximum": 5},
        "rationale": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "strengths",
                "gaps",
                "talking_points",
                "resume_priority_skills",
            ],
            "properties": {
                "strengths": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "talking_points": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "resume_priority_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 6,
                },
                "evidence_quotes": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Per-axis JD quote (or empty string if no quote available). "
                        "Finalist: if no quote, the corresponding axis must be ≤ 2."
                    ),
                },
            },
        },
        "legitimacy_note": {"type": ["string", "null"]},
    },
}


_FINALIST_RULE_BLOCK: str = (
    "## Evidence rule (B3 component)\n\n"
    "For every axis, you MUST be able to point to a short JD quote (≤ 25\n"
    "words) that supports your score. Emit those quotes in\n"
    "rationale.evidence_quotes as `{axis_name: '<quote>'}`. If the JD\n"
    "does not contain a quote you could attach to an axis, then you do\n"
    "not have evidence for a positive score on that axis — score it 2 or\n"
    "lower and emit '' (empty string) for that axis's quote.\n\n"
    "Rationale: this prevents anchoring on the few-shot persona or on\n"
    "your prior beliefs about the company; only what the JD actually\n"
    "says counts.\n\n"
    "## Aggregation note (A2 component)\n\n"
    "The downstream classifier averages your six axis scores AND requires\n"
    "no axis below 3 for a job to count as 'apply'. Score each axis\n"
    "honestly and independently — the system will weight the mean. Do\n"
    "NOT compress your scores toward 4 to lift the average; if an axis\n"
    "is weak, score it 2 or 3 and let the rule decide.\n\n"
)


V3_SCORING_PROMPT_HEADER: str = PERSONA_CORRECTED_HEADER.replace(
    "## Rationale structure\n\n",
    _FINALIST_RULE_BLOCK + "## Rationale structure\n\n",
).replace(
    "  - resume_priority_skills: up to 6 skills (short tokens) to emphasize on the resume\n\n",
    "  - resume_priority_skills: up to 6 skills (short tokens) to emphasize on the resume\n"
    "  - evidence_quotes: object mapping each axis name to a JD quote\n"
    "                     supporting that axis's score, or '' if no quote\n"
    "                     (in which case that axis must be ≤ 2)\n\n",
)


FIELD_REINFORCEMENT: str = PERSONA_CORRECTED_FIELD_REINFORCEMENT + (
    "\n"
    "## Evidence reminder (B3)\n"
    "  - Every axis scored ≥ 3 must have a JD quote in evidence_quotes.\n"
    "  - No quote = score ≤ 2. Do not invent quotes from prior knowledge.\n"
    "\n"
    "## Aggregation reminder (A2)\n"
    "  - Mean of axes will be used downstream — do not flatten scores to\n"
    "    push the average up. Honest 2s and 3s on weak axes are correct.\n"
)


_FINALIST_QUOTE_EXAMPLE: str = """

Example 7 (finalist — evidence quotes shown):
Input: "Director of Product Analytics at HealthCo. Build the experimentation platform serving 10M users. Remote-first. Compensation $220K-$260K base."
Output:
{
  "title_fit": 5,
  "location_fit": 5,
  "comp_fit": 5,
  "domain_match": 4,
  "seniority_match": 5,
  "skills_match": 4,
  "rationale": {
    "strengths": ["Direct title match: 'Director of Product Analytics'", "Remote-first listed", "Comp $220-260K above floor"],
    "gaps": ["JD does not name specific tools (SQL, Python)"],
    "talking_points": ["Experimentation leadership"],
    "resume_priority_skills": ["Analytics leadership", "Experimentation"],
    "evidence_quotes": {
      "title_fit": "Director of Product Analytics",
      "location_fit": "Remote-first",
      "comp_fit": "Compensation $220K-$260K base",
      "domain_match": "HealthCo",
      "seniority_match": "Director",
      "skills_match": "experimentation platform serving 10M users"
    }
  },
  "legitimacy_note": null
}"""


FEWSHOT_EXAMPLES: str = PERSONA_CORRECTED_FEWSHOT_EXAMPLES + _FINALIST_QUOTE_EXAMPLE


V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)

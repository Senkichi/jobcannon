# PORTED from job_finder/web/scoring_prompts/variants/v5_domain_skills_anchor.py @ 7b8d57012bdc1283264b5b286b79ec2d44df4211 (private job-cannon). Ledger L-0262.
"""Variant v5_domain_skills_anchor — decision-rule anchoring for the two
failing axes (issue #2009).

#1819's calibration (n=417 auditor labels, 2026-08-22 → 08-28) showed
qwen2.5:14b over-scores ``domain_match`` (MAE 1.40, bias −1.35, 48% of
rows off by ≥2) and ``skills_match`` (0.99 / −0.91 / 28%), collapsing
67% of fit-axis values to a 4. The #2008 A/B of ``v4_finalist``
(evidence-quote gating + mean-floor framing) did NOT improve
domain/skills MAE — evidence quotes alone do not stop the model from
calling a quant-risk or FP&A role a domain match for a health-tech
product-analytics profile.

This variant changes the *decision rule* for the two failing axes, not
just the evidence requirement:

- ``domain_match``: anchored to the candidate's stated domains from the
  Candidate context (target industries + profile positions). 5 = same
  domain named in the JD, 4 = adjacent domain, 3 = generic analytics
  with no domain signal (the DEFAULT, not 4), ≤2 = a named different
  domain (finance/quant, biotech manufacturing, credit risk, …).

- ``skills_match``: scored from the JD's *required* list only
  (nice-to-haves dropped from the numerator). The model must enumerate
  required skills absent from the profile before scoring. ≥2
  hard-required absences caps the axis at 3.

Few-shots: persona-corrected baseline (from ``_persona_corrected``)
plus two negative few-shots drawn from the audit-dispute failure mode
(domain-mismatch 4→2 and skills-mismatch 4→2), so the calibration set
covers the actual over-scoring pattern.

Schema: unchanged from the persona-corrected baseline (no
evidence_quotes — the decision rule itself is the anchor, not a
per-axis quote gate).
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


# Schema: identical to the persona-corrected baseline. The v5 change is
# prompt-text only (decision-rule anchoring); no additive schema fields.
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
            },
        },
        "legitimacy_note": {"type": ["string", "null"]},
    },
}


# ---------------------------------------------------------------------------
# Decision-rule override block — spliced into the header, replacing the
# baseline domain_match and skills_match anchor sections.
# ---------------------------------------------------------------------------

_V5_DOMAIN_MATCH_RULE: str = (
    "### domain_match — INDUSTRY / VERTICAL (decision-rule anchored)\n"
    "Score against the candidate's stated domains. The candidate's target\n"
    "industries and prior-work domains are listed in the Candidate context\n"
    "above (target industries + position companies). Use THOSE as the\n"
    "reference set, not a generic 'analytics is always a 4' prior.\n"
    "  - score of 5: the JD names the SAME domain as one of the candidate's\n"
    "    target industries or prior-work domains (e.g. JD says 'healthcare\n"
    "    analytics' and the candidate's profile lists healthcare).\n"
    "  - score of 4: an ADJACENT domain — closely related to a stated domain\n"
    "    but a different vertical (e.g. health-tech adjacent to medical\n"
    "    devices, or FinTech adjacent to banking infrastructure).\n"
    "  - score of 3: generic analytics / data role with NO domain signal in\n"
    "    the JD — the JD names no industry, vertical, or product context.\n"
    "    This is the DEFAULT when the JD carries no domain anchor. Do NOT\n"
    "    default to 4 just because the role involves 'analytics' or 'data'.\n"
    "  - score of 2: the JD names a DIFFERENT domain not in the candidate's\n"
    "    stated set (e.g. credit risk / quant finance, biotech manufacturing,\n"
    "    logistics operations for a health-tech product-analytics candidate).\n"
    "    'Analytics' overlap does NOT make a different domain adjacent.\n"
    "  - score of 1: entirely unrelated domain with no transferable context.\n\n"
)

_V5_SKILLS_MATCH_RULE: str = (
    "### skills_match — TECHNICAL SKILLS (required-only decision rule)\n"
    "Score ONLY against the JD's REQUIRED skills. Nice-to-have / preferred /\n"
    "bonus skills do NOT count toward the numerator — they are not required.\n"
    "BEFORE scoring, identify the JD's required-skills list (usually under a\n"
    "'Requirements', 'Qualifications', or 'Must-have' heading) and enumerate\n"
    "the required skills that are ABSENT from the candidate's profile in\n"
    "rationale.gaps.\n"
    "  - score of 5: every required skill is present in the candidate's\n"
    "    profile (direct experience with all required skills).\n"
    "  - score of 4: exactly one required skill is absent, but the\n"
    "    candidate's core competency covers the gap (transferable).\n"
    "  - score of 3: two required skills absent (CAP — see hard rule below).\n"
    "  - score of 2: three or more required skills absent; significant gap.\n"
    "  - score of 1: domain mismatch; the required skills are unrelated to\n"
    "    the candidate's experience.\n"
    "HARD RULE: if ≥ 2 hard-required skills are absent from the candidate's\n"
    "profile, this axis is CAPPED at 3. Do not score 4 or 5 regardless of\n"
    "how transferable the adjacent skills are. The absent-required count is\n"
    "the binding constraint, not the nice-to-have overlap.\n\n"
)


def _replace_axis_section(header: str, axis_name: str, new_section: str) -> str:
    """Replace one axis's ``### axis_name — ...`` block in the header.

    The persona-corrected header has each axis as a ``### axis_name — TITLE``
    block followed by anchor lines, ending at the next ``### `` heading or
    ``## `` section. This swaps one such block for *new_section*.
    """
    marker = f"### {axis_name}"
    start = header.index(marker)
    # Find the next ### or ## heading after this axis block.
    next_heading = header.find("\n### ", start + len(marker))
    next_h2 = header.find("\n## ", start + len(marker))
    candidates = [h for h in (next_heading, next_h2) if h != -1]
    if not candidates:
        # Last axis block — goes to the end of the header's dimension section.
        end = len(header)
    else:
        end = min(candidates)
    return header[:start] + new_section + header[end:]


# Build the v5 header by replacing the two axis sections in the
# persona-corrected header.
_V5_HEADER: str = _replace_axis_section(
    PERSONA_CORRECTED_HEADER, "domain_match", _V5_DOMAIN_MATCH_RULE
)
_V5_HEADER = _replace_axis_section(_V5_HEADER, "skills_match", _V5_SKILLS_MATCH_RULE)

V3_SCORING_PROMPT_HEADER: str = _V5_HEADER


FIELD_REINFORCEMENT: str = PERSONA_CORRECTED_FIELD_REINFORCEMENT + (
    "\n"
    "## domain_match reminder (v5)\n"
    "  - Default is 3 (generic analytics, no domain signal) — NOT 4.\n"
    "  - A named different domain (credit risk, quant, biotech manufacturing)\n"
    "    is ≤ 2, even if the role involves 'analytics' or 'data'.\n"
    "  - Check the candidate's target industries in the context above before\n"
    "    scoring; do not assume a domain match from job-title overlap.\n"
    "\n"
    "## skills_match reminder (v5)\n"
    "  - Score against REQUIRED skills only. Nice-to-haves do not count.\n"
    "  - Enumerate absent required skills in rationale.gaps before scoring.\n"
    "  - ≥ 2 absent hard-required skills → cap this axis at 3.\n"
)


# ---------------------------------------------------------------------------
# Negative few-shots — drawn from the audit-dispute failure mode (#1819
# calibration). These show the model the specific over-scoring pattern to
# correct: a 4 awarded where the decision rule mandates ≤ 2.
# ---------------------------------------------------------------------------

_V5_NEGATIVE_FEWSHOTS: str = """

Example 8 (NEGATIVE — domain mismatch over-scored, correct is 2 not 4):
Input: "Quantitative Risk Analyst at JPMorganChase. Build credit risk models for the trading desk. Required: Python, pandas, risk modeling, Basel III. $180-210K, hybrid NYC." Candidate is health-tech product-analytics; target industries include healthcare, SaaS, FinTech — but NOT credit risk / quant finance. Prior work: product analytics at a health-tech company.
Output:
{
  "title_fit": 3,
  "location_fit": 3,
  "comp_fit": 4,
  "domain_match": 2,
  "seniority_match": 3,
  "skills_match": 3,
  "rationale": {
    "strengths": ["Python overlap", "Comp above floor"],
    "gaps": ["Credit risk / quant finance is a named DIFFERENT domain — not adjacent to health-tech product-analytics", "Basel III absent from profile", "Risk modeling is a different discipline from product analytics"],
    "talking_points": ["Python transferability"],
    "resume_priority_skills": ["Python", "Statistical Analysis"]
  },
  "legitimacy_note": null
}
Note: domain_match = 2 because the JD names credit risk / quant finance, which is NOT in the candidate's stated domains. The 'analytics' overlap does NOT make it adjacent — credit risk is a different vertical. A common error is scoring this 4; the correct score is 2.

Example 9 (NEGATIVE — skills mismatch over-scored, correct is 2 not 4):
Input: "Senior Data Engineer at DataCo. Required: Scala, Spark, Airflow, Kafka, Hadoop. Nice-to-have: Python, SQL. $190-220K, remote." Candidate's profile: Python, SQL, A/B Testing, Statistical Analysis, Tableau, scikit-learn — no Scala, Spark, Airflow, Kafka, or Hadoop.
Output:
{
  "title_fit": 3,
  "location_fit": 5,
  "comp_fit": 5,
  "domain_match": 3,
  "seniority_match": 4,
  "skills_match": 2,
  "rationale": {
    "strengths": ["Senior level appropriate", "Remote match", "Comp above floor"],
    "gaps": ["Scala absent (required)", "Spark absent (required)", "Airflow absent (required)", "Kafka absent (required)", "Hadoop absent (required) — 5 required skills absent; Python/SQL are nice-to-haves only"],
    "talking_points": ["Python transferability to data engineering"],
    "resume_priority_skills": ["Python", "SQL"]
  },
  "legitimacy_note": null
}
Note: skills_match = 2 because 5 REQUIRED skills are absent from the candidate's profile. Python and SQL are listed as nice-to-haves, NOT required — they do not count toward the numerator. A common error is scoring this 4 based on Python/SQL overlap; the correct score is 2 (≥ 2 absent required skills, capped at 3, and 5 absent pushes to 2).
""".strip()


FEWSHOT_EXAMPLES: str = PERSONA_CORRECTED_FEWSHOT_EXAMPLES + "\n\n" + _V5_NEGATIVE_FEWSHOTS


V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)

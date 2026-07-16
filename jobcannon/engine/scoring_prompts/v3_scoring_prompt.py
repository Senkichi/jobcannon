"""v3.0 ordinal-rubric scoring prompt — FROZEN 2026-04-19.

This module is the single source of truth for the v3.0 scoring prompt.
It is imported by:
  - scripts/v3_shootout.py (Phase 33 Plan 2) — the model shootout
  - job_finder/web/job_scorer.py (Phase 34 Plan 1) — the production scorer

DO NOT mutate V3_SCORING_PROMPT, JOB_ASSESSMENT_SCHEMA, FEWSHOT_EXAMPLES,
or FIELD_REINFORCEMENT after Plan 2 begins. Any change invalidates all
previously measured candidates (per Phase 33 CONTEXT §D-26 and
PITFALLS §8 prompt-model co-tuning).
"""

from __future__ import annotations

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


FIELD_REINFORCEMENT: str = (
    "STRICT FIELD NAMES (do not rename or invent synonyms):\n"
    "  - Use 'gaps' for shortcomings. Do NOT use 'weaknesses', 'concerns', or 'issues'.\n"
    "  - Use 'title_fit' for role-function match. Do NOT use 'role_fit' or 'job_fit'.\n"
    "  - Use 'seniority_match' for level match. Do NOT use 'experience_fit' or 'level_fit'.\n"
    "  - Use 'comp_fit' for compensation. Do NOT use 'salary_fit' or 'pay_fit'.\n"
    "  - Use 'domain_match' for industry/vertical. Do NOT use 'industry_fit'.\n"
    "  - Use 'skills_match' for technical skills. Do NOT use 'skills_fit' or 'tech_fit'.\n"
    "  - Emit integers 1-5 for all six dimensions (NOT strings, NOT 0, NOT 6).\n"
)


FEWSHOT_EXAMPLES: str = """
## Fewshot calibration examples — learn the 1-5 scale

Example 1 (strong apply — all 4s and 5s):
Input: Staff ML Engineer at a mid-stage startup, 8+ years, PyTorch/Python/MLOps, $220-260K, remote US. Candidate has 9 years Python, 5 years PyTorch, led a production ML platform team.
Output:
{
  "title_fit": 5,
  "location_fit": 5,
  "comp_fit": 5,
  "domain_match": 4,
  "seniority_match": 5,
  "skills_match": 5,
  "rationale": {
    "strengths": ["Direct PyTorch+MLOps match", "Seniority exact fit", "Comp above target"],
    "gaps": ["Domain adjacent (martech not fintech)"],
    "talking_points": ["Production platform leadership", "PyTorch scale experience"],
    "resume_priority_skills": ["PyTorch", "MLOps", "Python", "Team lead"]
  },
  "legitimacy_note": null
}

Example 2 (consider — mixed 3s and 4s):
Input: Senior Data Analyst, SQL+Tableau, $90-110K, hybrid 2 days/week in a nearby metro. Candidate is ML engineer looking for analyst role as a stepping-stone.
Output:
{
  "title_fit": 3,
  "location_fit": 4,
  "comp_fit": 3,
  "domain_match": 3,
  "seniority_match": 4,
  "skills_match": 3,
  "rationale": {
    "strengths": ["SQL strong", "Seniority acceptable"],
    "gaps": ["Title is analyst, candidate targets engineer", "Tableau not primary skill"],
    "talking_points": ["Transition narrative", "SQL depth"],
    "resume_priority_skills": ["SQL", "Tableau", "Data analysis"]
  },
  "legitimacy_note": null
}

Example 3 (skip — low 2s):
Input: Marketing Analyst at a crypto exchange, 1-3 yrs, $60-80K, on-site Miami. Candidate is senior ML engineer, California-based.
Output:
{
  "title_fit": 2,
  "location_fit": 1,
  "comp_fit": 2,
  "domain_match": 2,
  "seniority_match": 2,
  "skills_match": 2,
  "rationale": {
    "strengths": ["Some analytical overlap"],
    "gaps": ["Title mismatch", "Location mismatch", "Seniority mismatch"],
    "talking_points": [],
    "resume_priority_skills": []
  },
  "legitimacy_note": null
}

Example 4 (reject — any 1 or legitimacy flag):
Input: "Remote work-from-home data entry, $2000/week, no experience needed, apply via Telegram." Candidate is senior ML engineer.
Output:
{
  "title_fit": 1,
  "location_fit": 5,
  "comp_fit": 1,
  "domain_match": 1,
  "seniority_match": 1,
  "skills_match": 1,
  "rationale": {
    "strengths": [],
    "gaps": ["Not a real engineering role", "Compensation signal inconsistent"],
    "talking_points": [],
    "resume_priority_skills": []
  },
  "legitimacy_note": "Telegram contact + unrealistic pay + no experience requirement = scam/MLM pattern"
}

Example 5 (apply — 4s and 5s, different dimension mix):
Input: Principal Applied Scientist, LLM post-training, $350-420K, SF Bay hybrid. Candidate has 10 years ML, 3 years LLM fine-tuning, published at NeurIPS.
Output:
{
  "title_fit": 5,
  "location_fit": 4,
  "comp_fit": 5,
  "domain_match": 5,
  "seniority_match": 5,
  "skills_match": 5,
  "rationale": {
    "strengths": ["NeurIPS publications", "LLM post-training direct match", "Seniority exact"],
    "gaps": ["Hybrid requires partial relocation"],
    "talking_points": ["Publication record", "Fine-tuning pipelines"],
    "resume_priority_skills": ["LLM fine-tuning", "PyTorch", "Research", "ML"]
  },
  "legitimacy_note": null
}
""".strip()


# V3_SCORING_PROMPT_HEADER: the rubric + dimensions + rationale-structure block
# only. Phase 2a sub-fix (D-2.1, supersedes the freeze rule for splice-point
# necessity): exposed as a separate constant so job_scorer can splice
# candidate_context between FIELD_REINFORCEMENT and FEWSHOT_EXAMPLES. The legacy
# V3_SCORING_PROMPT aggregate below remains byte-identical to its pre-refactor
# value, so the no-context code path is unchanged.
V3_SCORING_PROMPT_HEADER: str = (
    # System prompt header
    "You are a job-fit assessor. You read a job description (JD) and a candidate "
    "profile and emit six ordinal ratings plus a structured rationale.\n\n"
    "## Six dimensions — 1-5 integer scale\n\n"
    "Every dimension uses this anchor:\n"
    "  - 1 — strong mismatch / disqualifying\n"
    "  - 2 — weak match, significant gaps\n"
    "  - 3 — neutral or partial fit (missing info: infer neutrally)\n"
    "  - 4 — good fit, minor gaps\n"
    "  - 5 — excellent fit, exceeds requirements\n\n"
    "### title_fit — ROLE FUNCTION\n"
    "Does the role function (what the person does daily) match the candidate's target roles?\n"
    "Analyst? Engineer? Manager? Scientist? Researcher? This measures function, not level.\n"
    "  - score of 1: role function is unrelated (e.g., marketing role for an engineering candidate)\n"
    "  - score of 3: adjacent role function (e.g., data analyst for an ML engineer candidate)\n"
    "  - score of 5: role function is a direct match\n\n"
    "### location_fit — LOCATION / LOGISTICS\n"
    "Does the location policy (remote / hybrid / on-site + geography) match the candidate's constraints?\n"
    "  - score of 1: on-site in a location the candidate cannot or will not relocate to\n"
    "  - score of 3: hybrid with feasible partial-commute\n"
    "  - score of 5: fully remote or on-site in a target geography\n\n"
    "### comp_fit — COMPENSATION\n"
    "Does the compensation (listed or inferred) meet the candidate's floor?\n"
    "  - score of 1: listed and below floor, OR strong below-floor signal (e.g., '$15/hr scrappy startup')\n"
    "  - score of 3: not listed; comparable roles at comparable companies typically meet floor\n"
    "  - score of 5: listed, meets or exceeds floor with margin\n\n"
    "### domain_match — INDUSTRY / VERTICAL\n"
    "Does the company's industry/vertical match the candidate's prior experience?\n"
    "  - score of 1: entirely different domain, no transferable context\n"
    "  - score of 3: adjacent domain, transferable context\n"
    "  - score of 5: direct domain match\n\n"
    "### seniority_match — LEVEL WITHIN FUNCTION\n"
    "Given the role function matches, is the level appropriate for the candidate's years of experience?\n"
    "  - score of 1: level wildly off (intern for a staff candidate, or VP for a junior)\n"
    "  - score of 3: level one step off (senior role for a staff candidate)\n"
    "  - score of 5: level exact match\n\n"
    "### skills_match — TECHNICAL SKILLS\n"
    "Does the candidate have direct experience with the required technical skills?\n"
    "  - score of 1: domain mismatch; skills are unrelated\n"
    "  - score of 3: transferable experience; partial direct match\n"
    "  - score of 5: direct experience with every listed required skill\n\n"
    "## Rationale structure\n\n"
    "Emit four lists under 'rationale':\n"
    "  - strengths: up to 4 short bullets of candidate strengths for this role\n"
    "  - gaps: up to 4 short bullets of shortcomings or missing context\n"
    "  - talking_points: up to 4 short bullets of things to emphasize in an application\n"
    "  - resume_priority_skills: up to 6 skills (short tokens) to emphasize on the resume\n\n"
    "## Legitimacy note\n\n"
    "Emit 'legitimacy_note' as a string ONLY if the JD shows red flags (scam signals, MLM pitch, "
    "unrealistic compensation, non-professional contact channels). Otherwise emit null."
)


# V3_SCORING_PROMPT: legacy aggregate (header + FIELD_REINFORCEMENT +
# FEWSHOT_EXAMPLES) preserved byte-identical for any caller that imports it
# directly. job_scorer._build_system_prompt() now assembles its own ordering
# when candidate_context is provided (header + FIELD_REINFORCEMENT + context +
# FEWSHOT_EXAMPLES per D-2.1) and falls back to this constant + FEWSHOT +
# FIELD_REINFORCEMENT otherwise.
V3_SCORING_PROMPT: str = (
    V3_SCORING_PROMPT_HEADER + "\n\n" + FIELD_REINFORCEMENT + "\n\n" + FEWSHOT_EXAMPLES
)

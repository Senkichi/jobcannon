"""Phase 4 shared helper: persona-corrected few-shot calibration examples.

The baseline v3 prompt's few-shots feature an "ML engineer with 8-9 years
PyTorch" persona, which RC1 identified as the source of false-positive
title_fit scores: the model anchors on the few-shot persona instead of
the candidate.

Per spec D-4.3, every Phase 4 variant uses these persona-corrected
few-shots so the screening signal isolates the per-variant dimension
change (not the persona fix). The candidate is analytics/DS leadership,
$150K floor, Remote/SF target, healthcare/SaaS/FinTech industries.

The leading underscore in this module name prevents the variant-loader
test from picking it up as a variant — it has no V3_SCORING_PROMPT.
"""

from __future__ import annotations

PERSONA_CORRECTED_FEWSHOT_EXAMPLES: str = """
## Fewshot calibration examples — learn the 1-5 scale (analytics/DS persona)

Example 1 (strong apply — all 4s and 5s):
Input: Director of Analytics at a mid-stage health-tech company, 8+ years, SQL/Python/experimentation, $200-260K, fully remote US. Candidate has 9 years analytics, led a 6-person experimentation org, healthcare experience.
Output:
{
  "title_fit": 5,
  "location_fit": 5,
  "comp_fit": 5,
  "domain_match": 5,
  "seniority_match": 5,
  "skills_match": 5,
  "rationale": {
    "strengths": ["Direct analytics-leadership match", "Healthcare domain match", "Comp above floor"],
    "gaps": [],
    "talking_points": ["Experimentation org leadership", "Healthcare analytics scale"],
    "resume_priority_skills": ["SQL", "Python", "Experimentation", "Team lead"]
  },
  "legitimacy_note": null
}

Example 2 (consider — mixed 3s and 4s):
Input: Senior Data Scientist at a B2B SaaS, $170-200K, remote. JD heavy on causal inference and A/B testing; no domain anchor in JD beyond "B2B SaaS". Candidate is analytics-leadership-targeting but ICs are within bounds.
Output:
{
  "title_fit": 4,
  "location_fit": 5,
  "comp_fit": 4,
  "domain_match": 3,
  "seniority_match": 3,
  "skills_match": 4,
  "rationale": {
    "strengths": ["A/B testing core competency", "Comp above floor", "Remote"],
    "gaps": ["IC role for a leadership-targeting candidate", "Domain weak signal"],
    "talking_points": ["Experimentation track record", "Cross-functional partnership"],
    "resume_priority_skills": ["A/B testing", "Causal inference", "SQL", "Python"]
  },
  "legitimacy_note": null
}

Example 3 (skip — multiple 2s):
Input: Marketing Analyst at a regional retailer, 1-3 yrs, $60-80K, on-site Miami. Candidate is senior analytics-leadership, California/remote-targeting.
Output:
{
  "title_fit": 2,
  "location_fit": 1,
  "comp_fit": 2,
  "domain_match": 2,
  "seniority_match": 2,
  "skills_match": 3,
  "rationale": {
    "strengths": ["Some analytics overlap"],
    "gaps": ["Junior role", "Miami on-site is a hard mismatch", "Comp below floor"],
    "talking_points": [],
    "resume_priority_skills": []
  },
  "legitimacy_note": null
}

Example 4 (reject — any 1 or legitimacy flag):
Input: "Remote work-from-home data entry, $2000/week, no experience needed, apply via Telegram." Candidate is senior analytics-leadership.
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
    "gaps": ["Not a real analytics role", "Compensation signal inconsistent"],
    "talking_points": [],
    "resume_priority_skills": []
  },
  "legitimacy_note": "Telegram contact + unrealistic pay + no experience requirement = scam/MLM pattern"
}

Example 5 (apply — all 4s and 5s, different mix):
Input: Head of Product Analytics, FinTech, $230-280K, hybrid SF (2 days/week). Candidate has 10 years analytics, SF-based, strong experimentation portfolio, led analytics at a fintech.
Output:
{
  "title_fit": 5,
  "location_fit": 4,
  "comp_fit": 5,
  "domain_match": 5,
  "seniority_match": 5,
  "skills_match": 5,
  "rationale": {
    "strengths": ["Direct title match", "FinTech domain match", "SF hybrid feasible", "Experimentation portfolio"],
    "gaps": ["Hybrid 2-day commute"],
    "talking_points": ["Analytics leadership", "FinTech experimentation"],
    "resume_priority_skills": ["Analytics leadership", "Experimentation", "SQL", "Python"]
  },
  "legitimacy_note": null
}

Example 6 (low-signal JD — multiple 3s reflecting honest uncertainty):
Input: "Senior Data Scientist at Acme Corp" — JD describes ML modeling at a high level but is silent on industry, salary, remote policy. Candidate is analytics-leadership-targeting.
Output:
{
  "title_fit": 4,
  "location_fit": 3,
  "comp_fit": 3,
  "domain_match": 3,
  "seniority_match": 4,
  "skills_match": 4,
  "rationale": {
    "strengths": ["DS core competency", "Senior level appropriate"],
    "gaps": ["JD silent on remote policy", "JD silent on comp", "JD silent on industry"],
    "talking_points": ["DS/analytics overlap"],
    "resume_priority_skills": ["SQL", "Python", "Stats"]
  },
  "legitimacy_note": null
}

Example 7 (apply — target-geography on-site = 5, near-target title = 4):
Input: "Lead Data Analyst at FinTech Co. SF office, on-site 5 days/week. Owns experimentation and growth analytics for the consumer app. $200K-$240K base. 6+ years required. Required skills: SQL, Python, A/B testing, dashboards." Candidate: analytics-leadership-targeting, target_titles include 'Lead Analyst' (not 'Lead Data Analyst' verbatim), $150K floor, target_locations = ['Remote', 'San Francisco'].
Output:
{
  "title_fit": 4,
  "location_fit": 5,
  "comp_fit": 5,
  "domain_match": 5,
  "seniority_match": 4,
  "skills_match": 4,
  "rationale": {
    "strengths": ["Direct experimentation/growth analytics scope", "FinTech domain match", "SF on-site is a target-geography match", "Comp $200-240K above $150K floor with margin"],
    "gaps": ["'Lead Data Analyst' is a near-variant of target title 'Lead Analyst', not exact"],
    "talking_points": ["Experimentation portfolio", "Lead-level analytics ownership"],
    "resume_priority_skills": ["Experimentation", "A/B testing", "SQL", "Python"]
  },
  "legitimacy_note": null
}

Notes on this example:
  - location_fit = 5 because SF is in target_locations; on-site does NOT downgrade when geography matches.
  - title_fit = 4 because "Lead Data Analyst" is a near-variant of "Lead Analyst" (target list is exemplary, not exhaustive).
  - All sub-scores >= 3, so this row classifies as "apply" downstream.
""".strip()


# ---------------------------------------------------------------------------
# Persona-corrected baseline header — the rubric block all variants share
# unless they override it. Identical to v3_scoring_prompt.V3_SCORING_PROMPT_HEADER
# but with the example anchors retargeted from "ML engineer" to "analytics".
# ---------------------------------------------------------------------------

PERSONA_CORRECTED_HEADER: str = (
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
    "  - score of 1: role function is unrelated (e.g., marketing role for an analytics candidate)\n"
    "  - score of 3: adjacent role function (e.g., generalist data role for an analytics-leadership candidate)\n"
    "  - score of 5: role function is a direct match\n\n"
    "### location_fit — LOCATION / LOGISTICS\n"
    "Does the location policy (remote / hybrid / on-site + geography) match the candidate's constraints?\n"
    "Cross-check the JD's location against the candidate's target_locations list before scoring.\n"
    "  - score of 1: on-site in a location NOT in the candidate's target_locations list\n"
    "  - score of 2: hybrid in a location not in the target list, but commute is feasible\n"
    "  - score of 3: hybrid in a target location with feasible partial-commute, OR remote with caveats (e.g., 'remote-first but in-person required quarterly')\n"
    "  - score of 4: hybrid in a target location with light office days, OR remote in a target country/region\n"
    "  - score of 5: fully remote, OR on-site/hybrid in a location ON the target_locations list (target geography on-site is a 5, not a 2 — geography match overrides on-site penalty)\n\n"
    "### comp_fit — COMPENSATION\n"
    "Does the compensation (listed or inferred) meet the candidate's floor?\n"
    "  - score of 1: listed and clearly below floor, OR strong below-floor signal (e.g., '$15/hr scrappy startup')\n"
    "  - score of 2: listed range straddles or barely reaches the floor (top-end ties the floor; midband below)\n"
    "  - score of 3: not listed; comparable roles at comparable companies typically meet floor\n"
    "  - score of 4: listed, meets floor with modest margin\n"
    "  - score of 5: listed, meets or exceeds floor with clear margin\n\n"
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


PERSONA_CORRECTED_FIELD_REINFORCEMENT: str = (
    "STRICT FIELD NAMES (do not rename or invent synonyms):\n"
    "  - Use 'gaps' for shortcomings. Do NOT use 'weaknesses', 'concerns', or 'issues'.\n"
    "  - Use 'title_fit' for role-function match. Do NOT use 'role_fit' or 'job_fit'.\n"
    "  - Use 'seniority_match' for level match. Do NOT use 'experience_fit' or 'level_fit'.\n"
    "  - Use 'comp_fit' for compensation. Do NOT use 'salary_fit' or 'pay_fit'.\n"
    "  - Use 'domain_match' for industry/vertical. Do NOT use 'industry_fit'.\n"
    "  - Use 'skills_match' for technical skills. Do NOT use 'skills_fit' or 'tech_fit'.\n"
    "  - Emit integers 1-5 for all six dimensions (NOT strings, NOT 0, NOT 6).\n"
)


__all__ = [
    "PERSONA_CORRECTED_FEWSHOT_EXAMPLES",
    "PERSONA_CORRECTED_FIELD_REINFORCEMENT",
    "PERSONA_CORRECTED_HEADER",
]

# PORTED from job_finder/web/jd_display.py @ 65e5ce021068b70a2369ac279c75395a078e1013 (private job-cannon). Ledger L-0190.
"""Content-quality-aware picker for which stored JD text to render.

``jobs.description`` and ``jobs.jd_full`` can both carry text for the same
job; the detail views have always shown whichever field is LONGER on the
theory that more text is more complete. That heuristic has no content-quality
check at all, so a verbose conversational refusal persisted into
``description`` by a pre-``llm_refusal_guard`` reformat pass (see
``jobcannon.engine.llm_refusal_guard``) would beat a short — or missing —
``jd_full`` and get rendered to the user as "the job description".

``pick_jd_display`` keeps the longer-wins heuristic for the common case where
both fields are genuine, but a field that ``looks_like_llm_refusal`` is never
preferred over a clean one regardless of length, and is never shown at all
when it's the only candidate.

Exports:
    pick_jd_display: Choose the best available JD text for display.
"""

from __future__ import annotations

from jobcannon.engine.llm_refusal_guard import looks_like_llm_refusal


def pick_jd_display(job: dict | None) -> str:
    """Return the best available JD text for *job*, or "" if none is usable."""
    if not job:
        return ""

    jd_full = job.get("jd_full") or ""
    description = job.get("description") or ""
    jd_full_ok = bool(jd_full) and not looks_like_llm_refusal(jd_full)
    description_ok = bool(description) and not looks_like_llm_refusal(description)

    if description_ok and jd_full_ok:
        return description if len(description) > len(jd_full) else jd_full
    if description_ok:
        return description
    if jd_full_ok:
        return jd_full
    return ""

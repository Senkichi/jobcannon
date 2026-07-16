"""Shared types for the scoring pipeline.

Provides:
    ScoringResult -- Discriminated return type that lets callers distinguish
              success from budget_exceeded, error, and skipped outcomes.
    format_salary_range -- Human-readable "$min - $max" string from a min/max pair.
    build_comp_context -- Compensation-summary string built from comp_data_json
              (Ashby/Lever ATS payloads). Migrated from the deleted haiku_scorer.py
              (Plan 4 COLLAPSE-01).

Usage:
    from jobcannon.engine.scoring_types import ScoringResult
"""

import json
from typing import Literal, NamedTuple


def format_salary_range(salary_min: int | None, salary_max: int | None) -> str:
    """Format salary_min/salary_max into a human-readable range string.

    Returns:
        e.g. "$80,000 - $120,000", "$80,000+", "up to $120,000", or "Not specified".
    """
    if salary_min is not None and salary_max is not None:
        return f"${salary_min:,} - ${salary_max:,}"
    elif salary_min is not None:
        return f"${salary_min:,}+"
    elif salary_max is not None:
        return f"up to ${salary_max:,}"
    return "Not specified"


class ScoringResult(NamedTuple):
    """Discriminated return type for score_job_haiku and evaluate_job_sonnet.

    Attributes:
        data: The scoring result dict on success, None otherwise.
        status: Why scoring ended -- 'success', 'budget_exceeded', 'error',
                or 'skipped' (precondition not met, e.g. missing jd_full).
    """

    data: dict | None
    status: Literal["success", "budget_exceeded", "error", "skipped"]


# ---------------------------------------------------------------------------
# Scoring-prompt helpers (migrated from haiku_scorer.py per COLLAPSE-01)
# ---------------------------------------------------------------------------


def build_comp_context(job_row: dict) -> str | None:
    """Build a concise compensation-context string from comp_data_json.

    Extracts equity, bonus, and benefits summaries from ATS-sourced
    compensation data (stored as JSON). Returns a short summary suitable
    for inclusion in a scoring prompt, or None if no extra comp data
    is available.
    """
    comp_data_raw = job_row.get("comp_data_json")
    if not comp_data_raw:
        return None
    try:
        comp = json.loads(comp_data_raw) if isinstance(comp_data_raw, str) else comp_data_raw
    except (ValueError, TypeError):
        return None
    if not comp or not isinstance(comp, dict):
        return None

    parts: list[str] = []
    tier_summary = comp.get("compensationTierSummary")
    if tier_summary and isinstance(tier_summary, str):
        parts.append(tier_summary.strip())
    currency = comp.get("currency")
    if currency and not tier_summary:
        comp_min = comp.get("min")
        comp_max = comp.get("max")
        if comp_min and comp_max:
            parts.append(f"{currency} {comp_min:,}-{comp_max:,}")
    return "; ".join(parts) if parts else None

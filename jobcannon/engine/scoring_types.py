# PORTED from job_finder/web/scoring_types.py @ 9d063804c86d76dec470dfb221db44fe7e716be3 (private job-cannon). Ledger L-0038.
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


def has_compensation_signal(job_row: dict) -> bool:
    """Return True iff the row carries a parsed compensation signal the model sees.

    ``comp_fit`` is a comparison against *stated* compensation. With no stated
    compensation there is nothing to compare against, so a maximum score is
    unsupported by construction (issue #1969). This helper is the single
    detection point for that precondition: it mirrors exactly what
    ``job_scorer._build_user_message`` shows the model — a ``Salary:`` line when
    ``salary_min``/``salary_max`` is present, and/or a ``Compensation:`` line
    when ``build_comp_context`` surfaces ATS-sourced comp (equity / bonus / tier
    summary). When both are absent the model is scoring ``comp_fit`` against
    nothing, and ``score_job`` forces the neutral midpoint (3) instead of
    trusting the model's output.

    This consumes only the signals the existing parser already produces — it
    does not re-parse the JD prose (the compensation parser itself is explicitly
    out of scope for #1969).
    """
    if job_row.get("salary_min") or job_row.get("salary_max"):
        return True
    return build_comp_context(job_row) is not None

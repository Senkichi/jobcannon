# PORTED from job_finder/web/enrichment_tiers.py @ 0a4c33c5af7cd4055e539672158cb301b7bdc407 (private job-cannon). Ledger L-0178.
"""Post-fetch structured-field extraction (Phase 2c).

Split out of the private ``enrichment_tiers.py`` (design note PR-4) as the
sole call_model-gated piece of the enrichment-tiers group. Binds to
``ScanServices.parse_structured_fields``.

# PORT-SEAM: call_model is an injected keyword-only parameter (design note
# PR-4 section 1c), not the private module-level
# `from job_finder.web.model_provider import call_model` import. The caller
# (data_enricher.py, L-0174) invokes this via `svc.parse_structured_fields(...)`
# with no call_model argument of its own; the host binds a partial closing
# over call_model in wiring.py (same shape as L-0230's tiebreak_primary_posting).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Minimum jd_full length before parse_structured_fields will spend a quick-tier call.
# Matches data_enricher.MIN_FETCH_JD_CHARS — anything shorter is residual
# auth-wall noise that wouldn't yield reliable salary/location signal.
_MIN_STRUCTURED_PARSE_JD_CHARS = 200

# JSON schema for the post-fetch structured-field extraction call.
# DELIBERATELY EXCLUDES jd_full so the model cannot summarize the description
# back into the description field (the bug the deleted Haiku/Sonnet synthesis
# tiers had — they fabricated short pseudo-JDs from search snippets).
# P1.2: salary_period added so the LLM can signal the posting's pay period;
# the value is routed through normalize_observation for unit math (D-2/D-3).
# #1202: residency_location + has_subcountry_constraint added so a JD-prose
# geographic/residency constraint the structured fields missed is captured.
# residency_location is a country/region/city string routed through
# apply_location_observation (source="llm_extract_residency"); the funnel
# merges it into locations_structured so compute_location_fit's rule table
# picks the right row. has_subcountry_constraint is a boolean gate for
# constraints finer than country/region/city (e.g. a remote role restricted
# to a named subset of US states) that the schema cannot represent as a
# raw_location string — it short-circuits compute_location_fit to None.
_STRUCTURED_FIELDS_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "salary_min": {"type": "integer"},
        "salary_max": {"type": "integer"},
        "salary_period": {"type": "string", "enum": ["annual", "hourly", "monthly"]},
        "location": {"type": "string"},
        "residency_location": {"type": "string"},
        "has_subcountry_constraint": {"type": "boolean"},
    },
}


def parse_structured_fields(
    jd_full: str,
    job_row: dict,
    conn: Any,
    config: dict,
    timeout: float | None = None,
    *,
    call_model: Callable[..., Any],
) -> dict:
    """Extract salary and location from a fully-fetched jd_full.

    Runs ONCE post-cascade, on the actual fetched description (no
    fragment truncation). Schema deliberately excludes jd_full so the
    model cannot summarize the description back into itself — that was
    the bug the deleted Haiku/Sonnet synthesis tiers had.

    Args:
        jd_full: The full job description text (post-fetch).
        job_row: Job record dict; uses 'dedup_key', 'title', 'company'.
        conn: Open DB connection (for cost recording in call_model).
        config: Application config dict (for provider routing).
        timeout: Optional provider-call timeout override (seconds), forwarded
            to ``call_model``. Defaults to None (provider default).
        call_model: REQUIRED keyword-only model-dispatch callable, matching
            the private repo's ``model_provider.call_model`` signature.
            PORT-SEAM: the engine has no provider of its own; the host
            supplies this.

    Returns:
        Dict containing only fields the model populated. None values
        are omitted. Returns {} on short jd_full, missing data, or any
        exception.
    """
    if not jd_full or len(jd_full) < _MIN_STRUCTURED_PARSE_JD_CHARS:
        return {}

    title = job_row.get("title", "")
    company = job_row.get("company", "")
    job_id = job_row.get("dedup_key")

    system_prompt = (
        "You extract structured fields from a job description. "
        "Return ONLY a JSON object with optional fields: "
        "salary_min (integer, in the unit stated by the posting), "
        "salary_max (integer, in the unit stated by the posting), "
        "salary_period (string, one of: annual|hourly|monthly — include only "
        "if the description explicitly states a pay period), "
        "location (string — the primary work location city/region/country), "
        "residency_location (string — a country/region/city residency or "
        "right-to-work constraint stated in the JD prose that restricts where "
        "the candidate must be based, e.g. 'United Kingdom', 'Netherlands', "
        "'Bangalore, India'. Include this ONLY when the JD explicitly restricts "
        "eligibility to a specific geography that the 'location' field alone "
        "does not capture, e.g. 'UK based', 'must be resident in India', or "
        "an onsite location stated in the body but not in the structured "
        "location header), "
        "has_subcountry_constraint (boolean — set to true when the JD carries "
        "a geographic constraint FINER than a single country/region/city that "
        "cannot be expressed as one residency_location string, e.g. a remote "
        "role restricted to a named subset of US states, or a timezone-band "
        "restriction. Set to false otherwise). "
        "Omit fields that cannot be determined. Do not invent data."
    )
    user_prompt = (
        f"Job: {title} at {company}\n\n"
        f"Description:\n{jd_full}\n\n"
        f"Extract structured fields as JSON. Include only fields explicitly mentioned."
    )

    try:
        result = call_model(
            tier="quick",  # cheap; structured-extraction task
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            conn=conn,
            config=config,
            output_schema=_STRUCTURED_FIELDS_SCHEMA,
            job_id=job_id,
            purpose="parse_structured_fields",
            max_tokens=256,
            timeout=timeout,
            privacy_sensitive=False,  # public JD data; honor cloud fallback chain
        )
    except Exception as exc:
        # By the time an exception reaches here the cascade has already
        # aborted internally; from this caller's point of view it's just
        # another failed attempt, so it degrades to {} exactly like any
        # other call_model error, per this function's documented "Returns {}
        # on ... any exception" contract.
        logger.warning("parse_structured_fields: error for %s: %s", job_id, exc)
        return {}

    if not result.data or not result.schema_valid:
        return {}

    # P1.2 (D-2/D-3): route LLM-reported salary through normalize_observation
    # so the single normalizer applies the salvage ladder (hourly → annualize,
    # implausible → drop both, etc.) instead of a bespoke inline bounds check.
    # Both-or-neither semantics are preserved by the normalizer's pair discipline.
    from jobcannon.engine.salary_normalizer import SalaryObservation, normalize_observation

    raw_min = result.data.get("salary_min")
    raw_max = result.data.get("salary_max")
    raw_period = result.data.get("salary_period") or "unknown"

    out: dict = {}

    if raw_min is not None or raw_max is not None:
        obs = SalaryObservation(
            min_value=float(raw_min) if raw_min is not None else None,
            max_value=float(raw_max) if raw_max is not None else None,
            period=raw_period,
            currency="USD",
            provenance="llm_extract",
            raw_text=f"llm: min={raw_min} max={raw_max} period={raw_period}",
        )
        normalized = normalize_observation(obs)
        if normalized.resolution in (
            "ok",
            "salvaged_hourly",
            "salvaged_daily",
            "salvaged_weekly",
            "salvaged_monthly",
        ):
            if normalized.salary_min is not None:
                out["salary_min"] = normalized.salary_min
            if normalized.salary_max is not None:
                out["salary_max"] = normalized.salary_max
            if normalized.period != "unknown":
                out["salary_period"] = normalized.period
        else:
            logger.warning(
                "parse_structured_fields: dropping implausible salary for %s "
                "(min=%s max=%s period=%s, resolution=%s)",
                job_id,
                raw_min,
                raw_max,
                raw_period,
                normalized.resolution,
            )

    location = result.data.get("location")
    if location is not None:
        out["location"] = location

    # #1202: residency constraint extraction. residency_location is a
    # country/region/city string routed through apply_location_observation
    # (source="llm_extract_residency") by _persist, merging into
    # locations_structured so compute_location_fit's rule table picks the
    # correct row. has_subcountry_constraint is a boolean gate for constraints
    # the schema cannot represent (e.g. a state-list restriction); _persist
    # writes it to the has_subcountry_constraint column so compute_location_fit
    # can short-circuit to None. Always emit has_subcountry_constraint (the
    # column defaults to NULL; the LLM call sets it to 0 or 1 so subsequent
    # enrichment passes skip the residency check).
    residency_location = result.data.get("residency_location")
    if residency_location is not None and str(residency_location).strip():
        out["residency_location"] = str(residency_location).strip()

    # Always emit has_subcountry_constraint (default False when the LLM omits
    # the key). The column defaults to NULL; writing a definitive 0 or 1 makes
    # the residency check idempotent so subsequent enrichment passes do not
    # re-extract it.
    raw_subcountry = result.data.get("has_subcountry_constraint")
    out["has_subcountry_constraint"] = bool(raw_subcountry) if raw_subcountry is not None else False

    return out

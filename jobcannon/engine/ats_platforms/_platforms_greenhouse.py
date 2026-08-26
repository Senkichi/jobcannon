"""Greenhouse platform scanner (registry form).

Salary capture (P1.3, D-1/D-2): ``pay_input_ranges`` fields are named
``min_cents``/``max_cents`` but Greenhouse sometimes returns whole-dollar
values at those paths (e.g. hourly $64 stored as ``min_cents=64``). The scanner
performs only the **lossless source-specific decode** of the cents-vs-dollars
question (D-1/D-2 boundary) and wraps the raw per-period values in a
:class:`SalaryObservation`; annualization, the cents salvage ladder, and
plausibility quarantine are the single normalizer's job
(:func:`jobcannon.engine.salary_normalizer.normalize_observation`, via
:func:`build_salary_fields`).

Greenhouse-specific lossless decode by the ``unit``/``interval`` field:
- ``"year"`` AND value > 1_000 → value is cents; pass ``value / 100``, period
  'annual'. (Unit *proves* it is cents — this is decode, not unit math.)
- ``"hour"`` → value is dollars; pass as-is, period 'hourly' (normalizer ×2080).
- Missing/other ``unit``/``interval`` → pass the raw value, period 'unknown',
  and let the normalizer's corroborated cents rung (rung 3) salvage it. This is
  the Northbeam fix: a unit-less ``17_000_000`` raw-cents pair now resolves to
  $170k instead of landing as a $17M canonical salary.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from jobcannon.engine.json_utils import to_naive_utc_iso
from jobcannon.engine._field_alias import resolve_title, resolve_url
from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)
from jobcannon.engine.ats_platforms._salary import build_salary_fields
from jobcannon.engine.description_formatter import html_to_plain_text

logger = logging.getLogger(__name__)


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    # max_pages is unused (non-paginated platform) but kept for signature compatibility.
    data = _http_get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&pay_transparency=true",
        log_label="scan_greenhouse",
        slug=slug,
    )
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs", [])
    return jobs if isinstance(jobs, list) else []


def _decode_greenhouse_value(value: float | None, period: str) -> float | None:
    """Lossless cents-vs-dollars decode of one ``pay_input_ranges`` value (D-1/D-2).

    Greenhouse names the field ``*_cents`` but the unit lies. Only when the
    posting's interval is annual AND the value exceeds $1,000 is it provably
    cents (a real annual salary < $1,000 does not exist) — divide by 100. Every
    other case passes the raw value through to the normalizer untouched:

    - annual & value > 1_000 → cents; ``value / 100``.
    - annual & value ≤ 1_000 → ambiguous small value; pass as-is (normalizer
      quarantines a sub-floor annual figure).
    - hourly/monthly/unknown  → pass the raw per-period value; the normalizer
      annualizes (hourly/monthly) or runs the corroborated cents rung (unknown).

    This is lossless decode (cents → dollars when the unit proves it), NOT unit
    math; annualization belongs to the single normalizer (D-2).
    """
    if value is None:
        return None
    if period == "annual" and value > 1_000:
        return value / 100
    return value


# Salary-period mapping (Phase 49.02). Maps Greenhouse's unit/interval value to
# the m081 salary_period CHECK allowlist; anything unrecognized → 'unknown'.
_INTERVAL_TO_PERIOD: dict[str, str] = {
    "year": "annual",
    "annual": "annual",
    "yearly": "annual",
    "hour": "hourly",
    "hourly": "hourly",
    "month": "monthly",
    "monthly": "monthly",
}

# m081 salary_currency CHECK allowlist.
_CURRENCY_ALLOWLIST: frozenset[str] = frozenset(
    {"USD", "GBP", "EUR", "CAD", "AUD", "INR", "SGD", "UNKNOWN"}
)


def _interval_to_period(interval: str | None) -> str:
    """Map a Greenhouse unit/interval to the salary_period allowlist."""
    if not interval:
        return "unknown"
    return _INTERVAL_TO_PERIOD.get(interval.strip().lower(), "unknown")


def _normalize_currency(currency: str | None) -> str:
    """Map a Greenhouse currency code to the salary_currency allowlist (default USD)."""
    if not currency:
        return "USD"
    code = currency.strip().upper()
    return code if code in _CURRENCY_ALLOWLIST else "USD"


def _to_canonical(posting: dict) -> list:
    """Layer-1 mapping for Greenhouse posting → list[JobLocation].

    Greenhouse exposes only a freeform ``location.name`` string (no
    structured city/region/country fields). We run it through
    ``parse_locations()`` (Layer 2/3) at ingest time so structured-location
    data is available immediately rather than waiting for the m067 backfill.

    Returns ``[]`` when the location field is absent or blank.
    """
    from jobcannon.engine.location_parser import parse_locations

    location_obj = posting.get("location")
    if not isinstance(location_obj, dict):
        return []
    location_name = (location_obj.get("name") or "").strip()
    if not location_name:
        return []
    try:
        return parse_locations(location_name)
    except Exception as exc:
        logger.debug(
            "greenhouse _to_canonical: parse_locations failed for %r: %s", location_name, exc
        )
        return []


def _posting_to_job(posting: dict, _slug: str) -> dict:
    # ── Salary (P1.3 capture: lossless decode → single normalizer) ───────────
    comp_json = None
    salary_fields = build_salary_fields(None, None)
    pay_ranges = posting.get("pay_input_ranges") or []
    if pay_ranges:
        first_range = pay_ranges[0]
        # Greenhouse may use "unit" or "interval" for the pay period field.
        interval = first_range.get("unit") or first_range.get("interval") or None
        period = _interval_to_period(interval)
        currency = _normalize_currency(
            first_range.get("currency_type") or first_range.get("currency")
        )
        # Lossless cents/dollars decode at capture (D-1/D-2); annualization +
        # cents salvage + quarantine are the normalizer's job (build_salary_fields).
        min_val = _decode_greenhouse_value(first_range.get("min_cents"), period)
        max_val = _decode_greenhouse_value(first_range.get("max_cents"), period)
        salary_fields = build_salary_fields(
            min_val,
            max_val,
            period=period,
            currency=currency,
            raw_text=json.dumps(first_range),
        )
        comp_json = json.dumps(pay_ranges)

    # ── Location (flat string + structured Layer-1 emission) ─────────────────
    location_obj = posting.get("location") or {}
    location = location_obj.get("name") or "" if isinstance(location_obj, dict) else ""

    # ── source_id (F-04: was missing on 98% of rows) ─────────────────────────
    posting_id = posting.get("id")
    source_id = str(posting_id) if posting_id is not None else None

    # ── posted_date (F-02: was missing on 100% of rows) ──────────────────────
    # Greenhouse returns ISO-8601 strings (e.g. "2024-01-15T10:30:00Z").
    # ``first_published`` is the only first-posted field the Job Board API
    # exposes: ``updated_at`` is last-modified (edits/reposts bump it,
    # making stale jobs look fresh) and ``created_at`` does not exist in the
    # payload at all. A missing first_published stays NULL — a wrong date is
    # worse than no date (D-08).
    posted_date: str | None = posting.get("first_published") or None

    # ── ats_refreshed_at (mutable refresh timestamp) ─────────────────────────
    # ``updated_at`` is the mutable last-modified field (bumps on edits/reposts).
    # Captured raw-as-provided and normalized to naive-UTC ISO for repost detection
    # (divergence from posted_date). NULL when absent — no synthesis.
    ats_refreshed_at: str | None = None
    refreshed_raw = posting.get("updated_at")
    if refreshed_raw:
        try:
            dt = datetime.fromisoformat(refreshed_raw.replace("Z", "+00:00"))
            ats_refreshed_at = to_naive_utc_iso(dt)
        except (ValueError, TypeError):
            # Malformed timestamp — leave NULL rather than storing garbage
            pass

    # ── Description (JD Layer 2 step 2a) ─────────────────────────────────────
    # Greenhouse `content` is entity-escaped HTML (&lt;p&gt;…). Stored verbatim
    # it would send raw HTML to the scorer. Convert losslessly to plain text —
    # no tags, no entities, no dropped sections.
    description = html_to_plain_text(posting.get("content") or "")

    # ── Title / URL via override-aware resolvers (Phase C field-rename heal) ──
    # resolve_* searches the canonical alias list first, then any healed
    # extras from an adopted ats:greenhouse override recipe. With no override
    # file present this is identical to the Phase B extract_field behaviour.
    title = resolve_title(posting, "greenhouse") or ""
    source_url = resolve_url(posting, "greenhouse") or ""

    # ── Structured-field CAPTURE — raw-as-provided, no synthesis ──────────────
    # The Greenhouse Job Board API exposes no remote / employment-type field;
    # both stay None. It does carry a ``departments`` array of {id, name} —
    # capture the first entry's name verbatim.
    department: str | None = None
    departments = posting.get("departments")
    if isinstance(departments, list):
        for dept in departments:
            if isinstance(dept, dict):
                name = (dept.get("name") or "").strip()
                if name:
                    department = name
                    break

    return {
        "title": title,
        "company_source": "Greenhouse",
        "location": location,
        "locations_structured": _to_canonical(posting),
        "description": description,
        "source_url": source_url,
        "salary_min": salary_fields["salary_min"],
        "salary_max": salary_fields["salary_max"],
        "salary_currency": salary_fields["salary_currency"],
        "salary_period": salary_fields["salary_period"],
        "salary_provenance": salary_fields["salary_provenance"],
        "salary_observation": salary_fields["salary_observation"],
        "comp_json": comp_json,
        "source_id": source_id,
        "posted_date": posted_date,
        "ats_refreshed_at": ats_refreshed_at,
        "is_remote": None,
        "employment_type": None,
        "department": department,
    }


SCANNER = PlatformScanner(
    name="greenhouse",
    company_source="Greenhouse",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)

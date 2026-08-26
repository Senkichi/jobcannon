"""Lever platform scanner (registry form)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from jobcannon.engine._field_alias import resolve_title, resolve_url
from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
    label_or_str,
)
from jobcannon.engine.ats_platforms._salary import build_salary_fields, period_from_interval
from jobcannon.engine.location_canonical import (
    JobLocation,
    normalize_workplace_type,
)


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    # max_pages is unused (non-paginated platform) but kept for signature compatibility.
    data = _http_get_json(
        f"https://api.lever.co/v0/postings/{slug}?mode=json",
        log_label="scan_lever",
        slug=slug,
    )
    return data if isinstance(data, list) else []


def _to_canonical(posting: dict) -> list[JobLocation]:
    """Layer-1 mapping for Lever posting → list[JobLocation].

    The only structured field Lever emits is the top-level ``workplaceType``
    enum (kebab-case: ``unspecified``/``on-site``/``remote``/``hybrid``).
    Location strings (``categories.location`` + ``categories.allLocations[]``)
    are freeform, so each entry is emitted as ``unresolved=True`` with the
    trustworthy ``workplace_type`` already filled in. The m067 backfill
    re-parses ``raw`` through Layer 2 to populate city/region/country.
    """
    wt = normalize_workplace_type(posting.get("workplaceType"))
    categories = posting.get("categories") or {}
    raw_locations: list[str] = []
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list):
        raw_locations.extend(
            str(s).strip() for s in all_locations if isinstance(s, str) and s.strip()
        )
    primary = categories.get("location")
    if isinstance(primary, str) and primary.strip() and primary.strip() not in raw_locations:
        raw_locations.append(primary.strip())
    if not raw_locations and wt == "REMOTE":
        # Pure-remote posting with no location string — synthesize one entry.
        return [JobLocation.unresolved_from_raw("Remote", workplace_type="REMOTE")]
    # Dedup by raw string — every entry is unresolved=True so the canonical
    # (country_code, region_code, city, workplace_type) dedup tuple is
    # identical for every entry and would collapse "Berlin" into "Stockholm".
    # Backfill (m067) re-keys them via Layer 2; pre-backfill, raw uniqueness
    # is the only safe signal.
    return [
        JobLocation.unresolved_from_raw(raw, workplace_type=wt)
        for raw in dict.fromkeys(raw_locations)
    ]


def _posting_to_job(posting: dict, slug: str) -> dict:
    # ── Salary (P1.3 capture, D-1/D-2): wrap raw Lever values in an observation
    # and delegate annualization/salvage to the single normalizer. Lever's
    # salaryRange exposes ``interval`` (e.g. "per-year-salary") and ``currency``.
    salary_range = posting.get("salaryRange") or {}
    comp_json = json.dumps(salary_range) if salary_range else None
    salary_fields = build_salary_fields(
        salary_range.get("min") if salary_range else None,
        salary_range.get("max") if salary_range else None,
        period=period_from_interval(salary_range.get("interval")),
        currency=salary_range.get("currency"),
        raw_text=comp_json,
    )

    categories = posting.get("categories") or {}
    location = categories.get("location") or categories.get("team") or ""

    # ── source_id (F-04: was missing on 98.6% of Lever rows) ─────────────────
    posting_id = posting.get("id")
    source_id: str | None = str(posting_id) if posting_id is not None else None

    # ── posted_date (from createdAt — Lever emits epoch-ms integer) ───────────
    created_at_ms = posting.get("createdAt")
    posted_date: str | None = None
    if created_at_ms is not None:
        posted_date = datetime.fromtimestamp(created_at_ms / 1000, tz=UTC).isoformat()

    # ── Title / URL via override-aware resolvers (Phase C field-rename heal) ──
    # resolve_* searches the canonical alias list first, then any healed
    # extras from an adopted ats:lever override recipe. With no override
    # file present this is identical to the Phase B extract_field behaviour.
    title = resolve_title(posting, "lever") or ""
    source_url = resolve_url(posting, "lever") or ""

    # ── Structured-field CAPTURE — raw-as-provided, no synthesis ──────────────
    # Lever's only remote signal is the top-level ``workplaceType`` enum
    # (kebab-case); derive the tri-state bool from it (REMOTE → True,
    # ONSITE/HYBRID → False, unspecified/absent → None). Employment type rides
    # in ``categories.commitment`` (e.g. "Full-time"); department/team in
    # ``categories.department`` / ``categories.team``.
    norm_wt = normalize_workplace_type(posting.get("workplaceType"))
    if norm_wt == "REMOTE":
        is_remote: bool | None = True
    elif norm_wt in ("ONSITE", "HYBRID"):
        is_remote = False
    else:
        is_remote = None
    employment_type = label_or_str(categories.get("commitment"))
    department = label_or_str(categories.get("department")) or label_or_str(categories.get("team"))

    return {
        "title": title,
        "company_source": "Lever",
        "location": location,
        "locations_structured": _to_canonical(posting),
        "description": posting.get("descriptionPlain") or "",
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
        "is_remote": is_remote,
        "employment_type": employment_type,
        "department": department,
    }


SCANNER = PlatformScanner(
    name="lever",
    company_source="Lever",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("text", ""),
    posting_to_job=_posting_to_job,
)

"""Pinpoint platform scanner (registry form).

Public JSON at ``https://{slug}.pinpointhq.com/postings.json`` → ``{"data": [...]}``.
Single-shot — Pinpoint returns every active posting in one response with
no pagination. Each item carries title, url, location dict
({city, name, province}), compensation_minimum/maximum, employment_type,
workplace_type, and a job.department.name nested under "job".
"""

from __future__ import annotations

import json

from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)
from jobcannon.engine.ats_platforms._salary import build_salary_fields, period_from_interval
from jobcannon.engine.description_formatter import strip_html_to_text
from jobcannon.engine.location_canonical import JobLocation, normalize_workplace_type


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    payload = _http_get_json(
        f"https://{slug}.pinpointhq.com/postings.json",
        log_label="scan_pinpoint",
        slug=slug,
    )
    if not isinstance(payload, dict):
        return []
    postings = payload.get("data")
    if not isinstance(postings, list):
        return []
    # Filter out non-dict items defensively (matches historical scan_pinpoint).
    return [p for p in postings if isinstance(p, dict)]


def _to_canonical(posting: dict) -> list[JobLocation]:
    """Layer-1 mapping for Pinpoint posting → list[JobLocation].

    Pinpoint exposes a structured ``location`` dict with ``city``, ``province``
    (region), and ``name`` (country name). We construct a ``JobLocation``
    directly from these fields — no Layer-2 parser required.

    Returns ``[]`` when the location field is absent or all sub-fields are blank.
    """
    loc_obj = posting.get("location")
    if not isinstance(loc_obj, dict):
        return []

    city = (loc_obj.get("city") or "").strip() or None
    region = (loc_obj.get("province") or "").strip() or None
    country = (loc_obj.get("name") or "").strip() or None

    # Require at least one geographic field to emit a structured location.
    if not any([city, region, country]):
        return []

    # Reconstruct a raw string from the available parts for audit/display.
    raw_parts = [p for p in [city, region, country] if p]
    raw = ", ".join(raw_parts)

    # Pinpoint exposes a workplace_type field at the posting level.
    workplace_type = normalize_workplace_type(posting.get("workplace_type"))

    return [
        JobLocation(
            city=city,
            region=region,
            region_code=None,  # Pinpoint does not expose ISO 3166-2 codes
            country=country,
            country_code=None,  # Pinpoint does not expose ISO 3166-1 codes
            workplace_type=workplace_type,
            raw=raw,
            unresolved=False,
        )
    ]


def _posting_to_job(posting: dict, _slug: str) -> dict:
    # _fetch_postings already filters out non-dicts; this function is only
    # called by run_platform_scan via that filter.
    loc_obj = posting.get("location") or {}
    if isinstance(loc_obj, dict):
        parts = [
            loc_obj.get("city") or "",
            loc_obj.get("province") or loc_obj.get("name") or "",
        ]
        location = ", ".join(p for p in parts if p)
    else:
        location = ""

    description_raw = posting.get("description") or posting.get("description_html") or ""
    description = strip_html_to_text(description_raw) if "<" in description_raw else description_raw

    source_url = posting.get("url") or posting.get("apply_url") or ""

    # ── Salary (P1.3 capture, D-1/D-2): wrap the raw Pinpoint compensation in an
    # observation and delegate annualization/salvage to the single normalizer.
    raw_min = posting.get("compensation_minimum")
    raw_max = posting.get("compensation_maximum")
    salary_fields = build_salary_fields(
        raw_min if isinstance(raw_min, (int, float)) else None,
        raw_max if isinstance(raw_max, (int, float)) else None,
        period=period_from_interval(
            posting.get("compensation_interval") or posting.get("salary_interval")
        ),
        currency=posting.get("compensation_currency") or posting.get("salary_currency"),
        raw_text=json.dumps(
            {
                "compensation_minimum": raw_min,
                "compensation_maximum": raw_max,
                "compensation_interval": posting.get("compensation_interval"),
                "compensation_currency": posting.get("compensation_currency"),
            }
        ),
    )

    # ── source_id (Layer-1, Phase 48.04) ────────────────────────────────────
    posting_id = posting.get("id")
    source_id = str(posting_id) if posting_id is not None else None

    # ── locations_structured (Layer-1, Phase 48.04) ──────────────────────────
    locations_structured = _to_canonical(posting)

    return {
        "title": posting.get("title") or "",
        "company_source": "Pinpoint",
        "location": location,
        "locations_structured": locations_structured,
        "description": description,
        "source_url": source_url,
        "salary_min": salary_fields["salary_min"],
        "salary_max": salary_fields["salary_max"],
        "salary_currency": salary_fields["salary_currency"],
        "salary_period": salary_fields["salary_period"],
        "salary_provenance": salary_fields["salary_provenance"],
        "salary_observation": salary_fields["salary_observation"],
        "comp_json": None,
        "source_id": source_id,
    }


SCANNER = PlatformScanner(
    name="pinpoint",
    company_source="Pinpoint",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title") or "",
    posting_to_job=_posting_to_job,
)

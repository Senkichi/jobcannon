"""Rippling ATS platform scanner (registry form).

Rippling exposes a public job board API at
``https://ats.rippling.com/api/v2/board/{slug}/jobs`` returning paginated
JSON: ``{"items": [...], "page": N, "pageSize": N, "totalItems": N,
"totalPages": N}``.

Each item carries the fields needed for the canonical job dict:

- ``id`` (UUID)
- ``name`` (title)
- ``url`` (application URL)
- ``department.name``
- ``locations``: list of objects with ``name``, ``city``, ``state``,
  ``country``, ``workplaceType``

The list endpoint does NOT include the job description — fetching it
requires a per-job detail call. We skip that to keep the scan fast and
let the existing enrichment_tier pipeline fill jd_full asynchronously
(same pattern as Recruitee, which also leaves description empty).

Salary is not exposed by the public Rippling API; comp_json stays None.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
)
from jobcannon.engine.location_canonical import (
    JobLocation,
    dedupe_locations,
    normalize_workplace_type,
)


def _str(value) -> str:
    """Coerce to stripped str, or empty if value is not a string.

    Guards against ATS responses that nest a dict under fields we expect
    to be flat strings (observed in Maleda Tech via Breezy).
    """
    return value.strip() if isinstance(value, str) else ""


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    """Fetch one page of Rippling postings. Pagination is rare for active
    tenants (typical totalPages is 1-3); we walk pages until exhausted.

    Args:
        slug: Rippling board slug.
        max_pages: Kept for signature compatibility; not applicable —
            Rippling paginates until ``totalPages`` is exhausted, gated by a
            hardcoded ``page > 20`` defensive cap (a >20-page tenant is
            unprecedented; the cap only guards against malformed pagination
            metadata causing a runaway loop) instead of a caller-supplied
            budget.
    """
    items: list[dict] = []
    page = 1
    while True:
        data = _http_get_json(
            f"https://ats.rippling.com/api/v2/board/{slug}/jobs",
            log_label="scan_rippling",
            slug=slug,
            params={"page": page, "pageSize": 100},
        )
        if not isinstance(data, dict):
            break
        batch = data.get("items")
        if not isinstance(batch, list) or not batch:
            break
        items.extend(p for p in batch if isinstance(p, dict))
        total_pages = data.get("totalPages")
        if not isinstance(total_pages, int) or page >= total_pages:
            break
        page += 1
        # Defensive cap: a tenant with >20 pages of 100 jobs is unprecedented;
        # break to avoid runaway HTTP if pagination metadata is malformed.
        if page > 20:
            break
    return items


def _to_canonical(item: dict) -> list[JobLocation]:
    """Layer-1 mapping for Rippling item → list[JobLocation].

    Rippling emits ``locations[].{name, city, state, country, workplaceType}``
    — workplace_type is per-location, not posting-level. ``stateCode`` /
    ``countryCode`` may be present; fall back to extracting from full names
    only when the 2-letter shape is obvious (defensive ISO-alpha-2 heuristic).
    """
    locs = item.get("locations")
    if not isinstance(locs, list):
        return []
    out: list[JobLocation] = []
    for loc in locs:
        if not isinstance(loc, dict):
            continue
        wt = normalize_workplace_type(loc.get("workplaceType"))
        city = _str(loc.get("city")) or None
        state = _str(loc.get("state")) or None
        state_code_raw = _str(loc.get("stateCode")).upper()
        state_code = state_code_raw or (state.upper() if state and len(state) == 2 else None)
        country = _str(loc.get("country")) or None
        country_code_raw = _str(loc.get("countryCode")).upper()
        country_code = country_code_raw or (
            country.upper() if country and len(country) == 2 else None
        )
        # Don't double-store: if state/country was actually a 2-letter code,
        # leave the long name None.
        region = state if state and len(state) != 2 else None
        country_name = country if country and len(country) != 2 else None
        raw = _str(loc.get("name")) or ", ".join(p for p in [city, state, country] if p)
        if not any((city, region, state_code, country_name, country_code)) and wt == "UNSPECIFIED":
            if raw:
                out.append(JobLocation.unresolved_from_raw(raw, workplace_type="UNSPECIFIED"))
            continue
        out.append(
            JobLocation(
                city=city,
                region=region,
                region_code=state_code,
                country=country_name,
                country_code=country_code,
                workplace_type=wt,
                raw=raw,
                unresolved=False,
            )
        )
    return dedupe_locations(out)


def _location_string(item: dict) -> str:
    """Pick a human-readable location from a Rippling item's `locations` list.

    Prefers the first location's display ``name`` field (already formatted
    by Rippling, e.g. "Remote (United States)" or "San Francisco, CA").
    Falls back to a city/state/country composite when ``name`` is absent.
    """
    locs = item.get("locations")
    if not isinstance(locs, list) or not locs:
        return ""
    first = locs[0]
    if not isinstance(first, dict):
        return ""
    name = first.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    parts = [
        first.get("city") or "",
        first.get("state") or first.get("stateCode") or "",
        first.get("country") or first.get("countryCode") or "",
    ]
    return ", ".join(p for p in parts if isinstance(p, str) and p)


def _posting_to_job(item: dict, slug: str) -> dict:
    source_url = item.get("url") or (
        f"https://ats.rippling.com/{slug}/jobs/{item.get('id')}" if item.get("id") else ""
    )
    return {
        "title": item.get("name") or "",
        "company_source": "Rippling",
        "location": _location_string(item),
        "locations_structured": _to_canonical(item),
        "description": "",  # list endpoint omits description; enrichment fills jd_full
        "source_url": source_url,
        "salary_min": None,
        "salary_max": None,
        "comp_json": None,
    }


SCANNER = PlatformScanner(
    name="rippling",
    company_source="Rippling",
    fetch_postings=_fetch_postings,
    title_of=lambda item: item.get("name") or "",
    posting_to_job=_posting_to_job,
)

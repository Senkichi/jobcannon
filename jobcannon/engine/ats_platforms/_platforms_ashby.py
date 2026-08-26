"""Ashby platform scanner (registry form).

Ashby slugs are CASE-SENSITIVE (Research Pitfall 3): jobs.ashbyhq.com/OpenAI
is not jobs.ashbyhq.com/openai. The slug is forwarded verbatim.

A single timeout retry handles the Ashby-side intermittency seen on
2026-05-26 07:41-07:50 when ~20 tenants in sequence returned Read
timeouts inside a 9-minute window. Capped at one retry so a sustained
Ashby outage cannot double the run time of the whole ATS scan.
"""

from __future__ import annotations

import json

from jobcannon.engine.ats_platforms._registry import (
    PlatformScanner,
    _http_get_json,
    coerce_remote_bool,
    label_or_str,
)
from jobcannon.engine.ats_platforms._salary import build_salary_fields, period_from_interval
from jobcannon.engine.description_formatter import html_to_plain_text
from jobcannon.engine.location_canonical import (
    JobLocation,
    WorkplaceType,
    dedupe_locations,
    normalize_workplace_type,
)


def _fetch_postings(slug: str, max_pages: int | None = None) -> list[dict]:
    # NOTE: No lowercasing — Ashby slugs are case-sensitive.
    # max_pages is unused (non-paginated platform) but kept for signature compatibility.
    data = _http_get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
        log_label="scan_ashby",
        slug=slug,
        retry_on_timeout=True,
    )
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs", [])
    return jobs if isinstance(jobs, list) else []


def _address_to_loc(
    addr: dict | None, workplace_type: WorkplaceType, fallback_raw: str
) -> JobLocation | None:
    """Map an Ashby address (``postalAddress.*`` shape) to a JobLocation.

    Returns None when the address has no usable fields and the caller has
    no fallback raw string. The caller's ``workplace_type`` is the
    structured posting-level signal — applied uniformly to primary +
    secondary entries per SPEC §Layer-1.
    """
    if not isinstance(addr, dict):
        if fallback_raw:
            return JobLocation.unresolved_from_raw(fallback_raw, workplace_type=workplace_type)
        return None
    postal = addr.get("postalAddress") if isinstance(addr.get("postalAddress"), dict) else addr
    if not isinstance(postal, dict):
        postal = {}
    city = (postal.get("addressLocality") or "").strip() or None
    region = (postal.get("addressRegion") or "").strip() or None
    country = (postal.get("addressCountry") or "").strip() or None
    raw = (
        ", ".join(
            p
            for p in [
                postal.get("addressLocality"),
                postal.get("addressRegion"),
                postal.get("addressCountry"),
            ]
            if p
        )
        or fallback_raw
    )
    if not any((city, region, country)):
        if not raw:
            return None
        return JobLocation.unresolved_from_raw(raw, workplace_type=workplace_type)
    # Two-letter addressCountry → ISO 3166-1 alpha-2 (defensive uppercase).
    country_code = country.upper() if (country and len(country) == 2) else None
    return JobLocation(
        city=city,
        region=region,
        region_code=None,  # Ashby does not emit ISO 3166-2; parser/backfill resolves.
        country=country if country and len(country) != 2 else None,
        country_code=country_code,
        workplace_type=workplace_type,
        raw=raw,
        unresolved=False,
    )


def _to_canonical(posting: dict) -> list[JobLocation]:
    """Layer-1 mapping for Ashby posting → list[JobLocation].

    Sources: ``address.postalAddress.{addressLocality, addressRegion,
    addressCountry}`` (primary), ``secondaryLocations[].address.postalAddress``
    (multi-loc), ``workplaceType`` enum (PascalCase). Falls back to the flat
    ``location`` string + ``isRemote`` bool when the structured address is
    absent — matches existing scanner behavior so legacy postings don't
    regress to empty.
    """
    wt = normalize_workplace_type(posting.get("workplaceType"))
    if wt == "UNSPECIFIED" and posting.get("isRemote"):
        wt = "REMOTE"
    out: list[JobLocation] = []
    primary = _address_to_loc(
        posting.get("address"),
        wt,
        fallback_raw=(posting.get("location") or "").strip(),
    )
    if primary is not None:
        out.append(primary)
    for sec in posting.get("secondaryLocations") or []:
        if not isinstance(sec, dict):
            continue
        loc = _address_to_loc(sec.get("address"), wt, fallback_raw="")
        if loc is not None:
            out.append(loc)
    # Fallback path: pure-remote posting with no structured address.
    if not out and wt == "REMOTE":
        out.append(
            JobLocation.unresolved_from_raw(
                posting.get("location") or "Remote", workplace_type="REMOTE"
            )
        )
    return dedupe_locations(out)


def _posting_to_job(posting: dict, _slug: str) -> dict:
    # ── Salary (P1.3 capture, D-1/D-2): wrap the raw base_salary component in an
    # observation and delegate annualization/salvage to the single normalizer.
    # Ashby components carry ``interval`` (e.g. "1 YEAR") and ``currencyCode``.
    comp_json = None
    salary_fields = build_salary_fields(None, None)
    compensation = posting.get("compensation")
    if compensation:
        comp_json = json.dumps(compensation)
        summary_components = compensation.get("summaryComponents") or []
        for component in summary_components:
            if component.get("compensationType") == "base_salary":
                salary_fields = build_salary_fields(
                    component.get("minValue"),
                    component.get("maxValue"),
                    period=period_from_interval(component.get("interval")),
                    currency=component.get("currencyCode") or component.get("currency"),
                    raw_text=json.dumps(component),
                )
                break

    location = posting.get("location") or ""
    if not location and posting.get("isRemote"):
        location = "Remote"

    # descriptionPlain is already clean; only the descriptionHtml fallback needs
    # lossless HTML→text conversion (JD Layer 2 step 2b).
    description = posting.get("descriptionPlain") or ""
    if not description:
        description = html_to_plain_text(posting.get("descriptionHtml") or "")

    # ── source_id (F-04: was missing on 98.4% of Ashby rows) ─────────────────
    posting_id = posting.get("id")
    source_id: str | None = str(posting_id) if posting_id is not None else None

    # ── posted_date (from publishedAt ISO-8601 string) ────────────────────────
    posted_date: str | None = posting.get("publishedAt") or None

    # ── Structured-field CAPTURE — raw-as-provided, no synthesis ──────────────
    # Ashby emits ``isRemote`` (bool), ``employmentType`` (e.g. "FullTime"),
    # and ``department`` / ``team`` strings.
    is_remote = coerce_remote_bool(posting.get("isRemote"))
    employment_type = label_or_str(posting.get("employmentType"))
    department = label_or_str(posting.get("department")) or label_or_str(posting.get("team"))

    return {
        "title": posting.get("title", ""),
        "company_source": "Ashby",
        "location": location,
        "locations_structured": _to_canonical(posting),
        "description": description,
        "source_url": posting.get("jobUrl") or "",
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
    name="ashby",
    company_source="Ashby",
    fetch_postings=_fetch_postings,
    title_of=lambda posting: posting.get("title", ""),
    posting_to_job=_posting_to_job,
)

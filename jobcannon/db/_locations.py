"""apply_location_observation -- Postgres port of the private repo's
single-writer funnel for the canonical location columns (ledger L-0072).
Private source: ``job_finder/db/_locations.py`` @
b1f69f3e10a452cc498527f830959b852108f5e9.

Landed as a new sibling module rather than a hunk in ``jobcannon/db/_jobs.py``
-- ``_jobs.py`` is L-0070's manifest-gap file (already on public main
pre-audit; this branch changes no code there), and fidelity-diff is
file-to-file so a real port needs its own file anyway.
# PORT-SEAM: private's D-5 rule ("no module outside upsert_job/this funnel
# writes any location column") is enforced there by a CI grep gate
# (tests/test_location_writers_routed.py); no equivalent gate exists here.
# Recorded, not fixed: a future PR adding one should allowlist exactly
# jobcannon/db/_jobs.py::upsert_job and this module.

Design rules this module enforces (cite by ID -- see issue #393 / #386):

- **D-5 (Single writer for canonical location).** ``locations_structured`` is
  the canonical location. ``location`` (display string) and
  ``workplace_type`` / ``primary_country_code`` (denormalized filter columns)
  are *derived* from it in exactly one code path. No module outside that path
  writes any location column. Enrichment contributes *observations* through the
  same funnel rather than side-door-writing the ``location`` column (the S4
  wipe: an enrichment-written ``location`` with an empty ``locations_raw`` was
  reverted to ``''`` the next time the crawler re-sighted the job, because the
  upsert UPDATE branch rebuilds ``location`` from ``locations_raw``).

# PORT-SEAM: private's sixth column, ``is_location_unresolved`` (a
# materialized issue-#1336 flag computed alongside the write), has no
# equivalent column on this host -- the migration that would add it hasn't
# landed. Dropped from both the SELECT and UPDATE; nothing here computes or
# writes it. This module therefore owns FOUR columns together, not private's
# five: ``locations_raw, locations_structured, location, workplace_type,
# primary_country_code``, written atomically so a re-sighting can never erase
# a subset of them. Revisit if/when the column lands.

This mirrors the existing single-writer patterns in the codebase:
``set_jd_full`` is the sole sanctioned ``jd_full`` writer; the assessment
writer (L-0064) is the sole scoring-column writer. ``apply_location_observation``
is the analogous funnel for location. (# PORT-SEAM: private cites two CI
grep-gate tests here, ``tests/test_jd_full_writers_routed.py`` and
``tests/test_location_writers_routed.py`` -- neither exists on this host,
see the module's own D-5 PORT-SEAM note above.)

Exports
-------
merge_locations_raw(existing, incoming) -> list[str]
    Pure helper: Remote/Hybrid-first set-union of two raw-location lists.
    Shared by ``upsert_job`` (UPDATE branch) and ``apply_location_observation``
    so the merge semantics are defined in exactly one place.

merge_locations_structured(existing, incoming) -> list[JobLocation]
    Pure helper: Union by ``(country_code, region_code, city)`` with workplace_type
    specificity upgrade. Shared by ``upsert_job`` (UPDATE branch) so the merge
    semantics are defined in exactly one place. Mirrors the design of
    ``merge_locations_raw``.
    # PORT-SEAM: this host's upsert_job (jobcannon/db/_jobs.py,
    # manifest-gap/frozen) does its own simpler fill-if-null merge instead
    # and does NOT call this helper -- wiring it in would edit a
    # manifest-gap file, out of scope here. Ported anyway (cheap, pure,
    # part of the module's public surface) so a future PR can wire it
    # without another port pass.

apply_location_observation(conn, dedup_key, raw_location, *, source) -> bool
    The single funnel. Merges one observed location string into a job's
    canonical location columns and rewrites all four together in one UPDATE.
    (# PORT-SEAM: "four", not private's "five" -- is_location_unresolved
    dropped, see module docstring.) Idempotent, never raises on parse
    failure. Unwired here (no in-tree caller yet), matching the
    L-0077/L-0078 unwired-writer precedent.
"""

from __future__ import annotations

import logging  # PORT-SEAM: private's `import json` / `import sqlite3` dropped -- no sqlite3 dialect and no manual JSON round-trip on this host, see below
import re

import psycopg  # PORT-SEAM: replaces private's sqlite3 import -- no sqlite3 dialect on this host
from psycopg.types.json import Jsonb

from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine.location_canonical import (
    JobLocation,
    dedupe_locations,
)  # PORT-SEAM: private's second import (to_json as _locations_to_json) dropped -- Jsonb(list[dict]) via _loc_dict replaces text-column JSON serialization, see apply_location_observation

_logger = logging.getLogger(__name__)

# Remote/Hybrid raw entries float to the front of locations_raw so the merged
# display string and the dropdown lead with the workplace signal. Mirrors the
# historical inline behavior in upsert_job's UPDATE branch.
_REMOTE_HYBRID_RE = re.compile(r"\b(remote|hybrid)\b", re.IGNORECASE)


def merge_locations_raw(existing: list[str], incoming: list[str]) -> list[str]:
    """Remote/Hybrid-first set-union of two raw-location lists (pure).

    Single source of truth for the ``locations_raw`` merge semantics shared by
    ``upsert_job`` and ``apply_location_observation`` (D-5). Case-insensitive
    de-dup against the existing list; first-seen casing is preserved. A new
    entry containing a standalone ``remote`` / ``hybrid`` token is inserted at
    the front (so the workplace signal leads the display join); everything else
    is appended in arrival order.

    Args:
        existing: The raw-location list already stored on the row.
        incoming: Newly observed raw-location strings to merge in.

    Returns:
        A new list — neither input is mutated (immutability).
    """
    merged: list[str] = [loc for loc in existing if loc]
    seen_keys = {loc.lower() for loc in merged}
    for normalized in incoming:
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if _REMOTE_HYBRID_RE.search(normalized):
            merged.insert(0, normalized)
        else:
            merged.append(normalized)
    return merged


def merge_locations_structured(
    existing: list[JobLocation], incoming: list[JobLocation]
) -> list[JobLocation]:
    """Union by ``(country_code, region_code, city)`` with workplace_type specificity upgrade (pure).

    Single source of truth for the ``locations_structured`` merge semantics
    shared by ``upsert_job`` (UPDATE branch). Mirrors the design of
    ``merge_locations_raw`` — pure, immutable, single source of truth.

    Deduplication groups by ``(country_code, region_code, city)`` only —
    ``workplace_type`` is a refinable payload field, not part of the identity.
    Within each geographic group, the most specific ``workplace_type`` wins:
    ``UNSPECIFIED`` is upgraded to any other value (``REMOTE``/``HYBRID``/``ONSITE``).
    Conflicts between non-UNSPECIFIED values keep the first-seen (no downgrade).

    This ensures that a later sighting with a resolved workplace_type (e.g.
    ``#LI-Hybrid`` parsed from the full JD) upgrades an earlier ``UNSPECIFIED``
    entry for the same city, fixing the denormalized column freeze bug (issue #639).

    Preserves first-seen order: existing entries first (in their stored order),
    then genuinely-new incoming entries appended in arrival order.

    Args:
        existing: The structured-location list already stored on the row.
        incoming: Newly observed structured locations to merge in.

    Returns:
        A new list — neither input is mutated (immutability).
    """
    # Group by geographic identity (country_code, region_code, city) only —
    # workplace_type is refinable, not part of the dedup key. Exception: when
    # all three geo fields are unresolved (None), there is no geographic
    # identity to upgrade in place, so fall back to the full 4-tuple key
    # (matching dedupe_locations) — otherwise distinct unresolved-geo entries
    # with different specific workplace_type values would silently collapse
    # (issue #639 remediation-pass finding: all-None geo-key collision).
    geo_groups: dict[
        tuple[str | None, str | None, str | None] | tuple[str | None, str | None, str | None, str],
        list[JobLocation],
    ] = {}
    for loc in existing + incoming:
        if loc.country_code is None and loc.region_code is None and loc.city is None:
            geo_key = (loc.country_code, loc.region_code, loc.city, loc.workplace_type)
        else:
            geo_key = (loc.country_code, loc.region_code, loc.city)
        if geo_key not in geo_groups:
            geo_groups[geo_key] = []
        geo_groups[geo_key].append(loc)

    # For each geographic group, pick the best workplace_type entry.
    # UNSPECIFIED is upgraded to any other value; conflicts keep first-seen.
    merged: list[JobLocation] = []
    for _geo_key, group in geo_groups.items():
        # Find the most specific workplace_type in this group.
        # Priority: REMOTE/HYBRID/ONSITE > UNSPECIFIED.
        best = group[0]  # Default to first-seen
        for loc in group[1:]:
            if best.workplace_type == "UNSPECIFIED" and loc.workplace_type != "UNSPECIFIED":
                # Upgrade: UNSPECIFIED -> more specific
                best = loc
            # else: keep first-seen (no downgrade from specific to UNSPECIFIED,
            # and no change between REMOTE/HYBRID/ONSITE conflicts)
        merged.append(best)

    # Final dedupe by full key (including workplace_type) to handle the case
    # where the same geographic+workplace_type entry appears in both lists.
    return dedupe_locations(merged)


def _loc_dict(loc: JobLocation) -> dict:
    # PORT-SEAM: new helper, no private equivalent at module scope --
    # mirrors jobcannon/db/_jobs.py's own private _loc_dict (manifest-gap,
    # can't import a "_"-prefixed name across modules) so Jsonb() gets a
    # plain dict rather than a JobLocation dataclass instance.
    from dataclasses import asdict

    return asdict(loc)


def apply_location_observation(
    conn: object,  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only) -- private said "sqlite3.Connection"
    dedup_key: str,
    raw_location: str,
    *,
    source: str,
) -> bool:
    """Merge a location observation into a job's canonical location columns.

    The single sanctioned write path for the four canonical location columns
    outside ``upsert_job`` (D-5). (# PORT-SEAM: "four", not private's "five"
    -- see module docstring.) Pipeline:

        normalize/split incoming string -> split_multi_locations
        -> merge into locations_raw (Remote/Hybrid first, via merge_locations_raw)
        -> parse_locations(merged_raw, jd_full=row.jd_full) -> rewrite
           locations_structured, location (derived join), workplace_type,
           primary_country_code in ONE UPDATE.

    All four columns move together (# PORT-SEAM: private's "All five
    columns") — the invariant that kills the S4 wipe class (an
    enrichment-written ``location`` with empty ``locations_raw`` survives a
    subsequent crawler re-sighting because ``locations_raw`` now carries it too).

    Idempotent: re-applying the same observation is a no-op (the
    case-insensitive de-dup in ``merge_locations_raw`` plus the no-change guard
    below). Never raises on parse failure — logs at WARNING and returns False.

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching ``_jobs.py``'s dispatch. (# PORT-SEAM:
            private said "Open sqlite3 connection".)
        dedup_key: The job's primary key.
        raw_location: An observed location string (e.g. an LLM-extracted city).
        source: Provenance tag for logging (e.g. ``"llm_extract"``).

    Returns:
        True when at least one canonical location column changed; False on a
        no-op (idempotent re-apply, missing row, empty/unparseable input, or
        parse failure).
    """
    if not dedup_key or not raw_location or not raw_location.strip():
        return False

    # PORT-SEAM: paths changed from job_finder.web.* to jobcannon.engine.*
    # Lazy imports — keep db/ free of a module-load-time db/ -> engine/ cycle
    # (same pattern as private's db/ -> web/ avoidance, and _jd_full.normalize_jd's
    # deferred import on this host).
    from jobcannon.engine.location_normalizer import split_multi_locations
    from jobcannon.engine.location_parser import parse_locations

    raw = (
        conn.raw if hasattr(conn, "raw") else conn
    )  # PORT-SEAM: pooled-connection unwrap, no equivalent needed for private's bare sqlite3 connection

    try:
        row = raw.execute(
            "SELECT locations_raw, jd_full FROM postings WHERE dedup_key = %s",  # PORT-SEAM: postings table + %s placeholder replace private's jobs table + sqlite3 ?
            (dedup_key,),
        ).fetchone()
    except psycopg.Error as exc:  # PORT-SEAM: replaces sqlite3.Error
        # Contract: the funnel never raises — a side-door write must not abort
        # the surrounding enrichment persist (mirrors set_jd_full's soft-fail).
        _logger.warning(
            "apply_location_observation: read failed [source=%s key=%s]: %s",
            source,
            dedup_key,
            exc,
        )
        return False
    if row is None:
        return False

    existing_raw = list(
        row["locations_raw"] or []
    )  # PORT-SEAM: locations_raw is native jsonb here -- no json.loads text-column round-trip needed (private's original wrapped this in a try/except json.JSONDecodeError)
    existing_raw = [loc for loc in existing_raw if loc]

    incoming_raw = split_multi_locations(raw_location)
    if not incoming_raw:
        return False

    merged_raw = merge_locations_raw(existing_raw, incoming_raw)
    if merged_raw == existing_raw:
        # No new raw segment — idempotent re-apply. Nothing to rewrite.
        return False

    try:
        # jd_full is passed as the workplace-type fallback source (#LI-Remote /
        # #LI-Hybrid / #LI-Onsite body hashtags) — same proxy upsert_job uses.
        structured = parse_locations(merged_raw, jd_full=row["jd_full"])
    except Exception as exc:  # funnel must never raise (contract)
        _logger.warning(
            "apply_location_observation: parse failed [source=%s key=%s]: %s",
            source,
            dedup_key,
            exc,
        )
        return False

    locations_structured_payload = (
        Jsonb([_loc_dict(loc) for loc in structured]) if structured else None
    )  # PORT-SEAM: Jsonb(list[dict]) replaces private's _locations_to_json() text-column serialization
    location_col = ", ".join(dict.fromkeys(merged_raw))
    workplace_type = structured[0].workplace_type if structured else "UNSPECIFIED"
    primary_country_code = structured[0].country_code if structured else None
    # PORT-SEAM: private also computes is_location_unresolved (issue #1336)
    # here for the fifth column -- dropped, no equivalent column on this
    # host, see module docstring.

    # Four columns rewritten together (D-5). workplace_type uses the same
    # COALESCE/NULLIF guard as upsert_job so an UNSPECIFIED observation never
    # downgrades a previously-determined workplace type.
    try:
        with raw.transaction():  # PORT-SEAM: SAVEPOINT-based recovery replaces private's conn.execute()/conn.rollback() pair -- matching _companies.py/_jobs.py's transaction discipline
            raw.execute(
                "UPDATE postings SET "
                "locations_raw = %s, "
                "location = %s, "
                "locations_structured = %s, "
                "workplace_type = COALESCE(NULLIF(%s, 'UNSPECIFIED'), workplace_type, 'UNSPECIFIED'), "
                "primary_country_code = COALESCE(%s, primary_country_code) "
                "WHERE dedup_key = %s",
                (
                    Jsonb(merged_raw),
                    location_col,
                    locations_structured_payload,
                    workplace_type,
                    primary_country_code,
                    dedup_key,
                ),
            )
    except psycopg.Error as exc:
        # Contract: the funnel never raises. A constraint rejection or
        # transient DB error rolls back the location write (SAVEPOINT) but
        # leaves the caller's other persists (jd_full, salary, enrichment_tier)
        # intact.
        _logger.warning(
            "apply_location_observation: write failed [source=%s key=%s]: %s",
            source,
            dedup_key,
            exc,
        )
        return False
    commit_unless_nested(
        raw
    )  # PORT-SEAM: replaces private's conn.commit() -- no-op inside an ambient transaction (tests/host/conftest.py), a real commit otherwise
    _logger.info(
        "apply_location_observation: merged %r [source=%s key=%s] -> %d raw segments",
        raw_location,
        source,
        dedup_key,
        len(merged_raw),
    )
    return True

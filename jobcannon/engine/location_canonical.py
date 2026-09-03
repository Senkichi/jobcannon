# PORTED from job_finder/web/location_canonical.py @ 6c0ba1bc42d11053e09e1b27da642aac4af53f0c (private job-cannon). Ledger L-0455.
"""Canonical structured location for job postings.

`JobLocation` is the value object the location parser (Layer 2/3) and the
4 Layer-1 scanners (SmartRecruiters, Ashby, Lever, Rippling) both produce.
Field shape mirrors schema.org PostalAddress + LinkedIn workplaceType so
JSON-LD export comes for free and our internal enum casing matches the
most-quoted industry source.

# PORT-SEAM: dropped a reference to a private-only planning doc
# (.planning/SPEC-location-parsing.md), not carried into this port.
Companion modules:
  - `location_parser.py` — Layer 2 (gazetteer) + Layer 3 (heuristic).
  - `location_normalizer.py` — pre-existing whitespace/placeholder cleanup
    used as the first step of Layer 2.

Note: this module is the JSON shape that lands in
`jobs.locations_structured` (m066, separate commit). Adding fields here
needs a matching read-side update in any consumer that round-trips
through JSON — the encode/decode helpers below tolerate unknown fields
on read for forward-compat.
"""

from __future__ import annotations

import json
# PORT-SEAM: is_dataclass added for from_list's default-serializer helper below
# (an earlier port-wave addition, not in the private source's import list).
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Literal

WorkplaceType = Literal["REMOTE", "HYBRID", "ONSITE", "UNSPECIFIED"]

_VALID_WORKPLACE_TYPES: frozenset[str] = frozenset({"REMOTE", "HYBRID", "ONSITE", "UNSPECIFIED"})


@dataclass(frozen=True, slots=True)
class JobLocation:
    """Canonical structured location for a single job posting.

    Multi-location postings carry ``list[JobLocation]`` rather than a single
    instance. Always frozen — locations are value objects, mutation is a bug.

    Fields:
        city: City name in Title Case (``"San Francisco"``), or ``None`` when
            unknown / ambiguous / not applicable (pure-remote postings).
        region: Full subdivision name (``"California"``, ``"Ontario"``), or
            ``None``.
        region_code: ISO 3166-2 subdivision code without the country prefix
            (``"CA"``, ``"ON"``), uppercase, or ``None``.
        country: Country name (``"United States"``), or ``None``.
        country_code: ISO 3166-1 alpha-2 country code (``"US"``, ``"IN"``),
            uppercase, or ``None``.
        workplace_type: One of ``REMOTE`` / ``HYBRID`` / ``ONSITE`` /
            ``UNSPECIFIED``. Matches LinkedIn's workplaceType enum casing.
        raw: The original parser-captured string, preserved for audit and
            for display fallback when ``unresolved`` is true.
        unresolved: True iff structuring failed (the parser saw input but
            couldn't extract city/region/country). Consumers should fall
            back to ``raw`` for display.
    """

    city: str | None
    region: str | None
    region_code: str | None
    country: str | None
    country_code: str | None
    workplace_type: WorkplaceType
    raw: str
    unresolved: bool

    def __post_init__(self) -> None:
        if self.workplace_type not in _VALID_WORKPLACE_TYPES:
            raise ValueError(
                f"invalid workplace_type {self.workplace_type!r}; "
                f"must be one of {sorted(_VALID_WORKPLACE_TYPES)}"
            )

    @classmethod
    def unresolved_from_raw(
        cls,
        raw: str,
        *,
        workplace_type: WorkplaceType = "UNSPECIFIED",
    ) -> JobLocation:
        """Construct an ``unresolved=True`` location preserving ``raw``.

        Use this when Layer 2 cannot structure the input. The caller has
        already detected workplace_type from inline tokens (``Remote`` /
        ``Hybrid`` / ``#LI-Remote`` etc.) and passes it through; the rest
        is ``None``.
        """
        return cls(
            city=None,
            region=None,
            region_code=None,
            country=None,
            country_code=None,
            workplace_type=workplace_type,
            raw=raw,
            unresolved=True,
        )


def dedupe_locations(locations: list[JobLocation]) -> list[JobLocation]:
    """Deduplicate by ``(country_code, region_code, city, workplace_type)``.

    Preserves first-seen order. Matches the SPEC dedup contract — two
    locations that differ only in ``raw`` collapse to the first occurrence.
    """
    seen: set[tuple[str | None, str | None, str | None, str]] = set()
    out: list[JobLocation] = []
    for loc in locations:
        key = (loc.country_code, loc.region_code, loc.city, loc.workplace_type)
        if key in seen:
            continue
        seen.add(key)
        out.append(loc)
    return out


_WORKPLACE_TYPE_ALIASES: dict[str, WorkplaceType] = {
    "ONSITE": "ONSITE",
    "ON-SITE": "ONSITE",
    "ON_SITE": "ONSITE",
    "ON SITE": "ONSITE",
    "REMOTE": "REMOTE",
    "HYBRID": "HYBRID",
    "UNSPECIFIED": "UNSPECIFIED",
}


def normalize_workplace_type(value: str | None) -> WorkplaceType:
    """Canonicalize a vendor-emitted workplace-type string to the SPEC enum.

    Handles Ashby (PascalCase), Lever (kebab-case), Rippling (unspecified
    casing) in one helper. Unknown / empty values fall back to
    ``"UNSPECIFIED"`` — the parser layer can still override later.
    """
    if not value:
        return "UNSPECIFIED"
    return _WORKPLACE_TYPE_ALIASES.get(str(value).strip().upper(), "UNSPECIFIED")


def to_json(locations: list[JobLocation]) -> str:
    """Serialize a list of locations to a JSON string for ``locations_structured``.

    Empty list serializes to ``"[]"``. Use ``from_json`` for the reverse.
    """
    return json.dumps([asdict(loc) for loc in locations], ensure_ascii=False)


def from_json(payload: str | None) -> list[JobLocation]:
    """Deserialize ``locations_structured`` JSON to a list of locations.

    Tolerates ``None`` / empty string / ``"[]"`` (returns ``[]``). Tolerates
    unknown fields on read for forward-compat — extra keys are silently
    dropped. Raises ``ValueError`` only when JSON is malformed or when a
    required field is missing.
    """
    if not payload:
        return []
    data: Any = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError(f"expected JSON array, got {type(data).__name__}")
    out: list[JobLocation] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"expected JSON object, got {type(item).__name__}")
        # Forward-compat: ignore unknown keys, take only what JobLocation declares.
        out.append(
            JobLocation(
                city=item.get("city"),
                region=item.get("region"),
                region_code=item.get("region_code"),
                country=item.get("country"),
                country_code=item.get("country_code"),
                workplace_type=item.get("workplace_type", "UNSPECIFIED"),
                raw=item.get("raw", ""),
                unresolved=bool(item.get("unresolved", False)),
            )
        )
    return out


# PORT-SEAM: _location_json_default and from_list below are an engine-side
# addition from an earlier port wave (not in the private source at this row's
# declared SHA) — no host-layer dependency.
def _location_json_default(obj: object) -> object:
    """json.dumps default for dataclass values (e.g. ``JobLocation``).

    Mirrors the db/_jobs.py and ats_scanner cache write path: serialize frozen
    dataclass instances via ``asdict()``; anything else is a programming error.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def from_list(locations: list[Any] | None) -> list[JobLocation]:
    """Deserialize a list of location dicts or ``JobLocation`` objects.

    This is the list-shaped counterpart of ``from_json``. It is used when
    ``locations_structured`` has round-tripped through JSON as Python objects
    (e.g. the ATS scan cache in ``enrichment_tiers.query_ats_api``) and
    re-enters the live data flow. Already-instantiated ``JobLocation`` objects
    are returned unchanged.

    Tolerates ``None`` / empty list (returns ``[]``). Tolerates unknown fields
    on dict items for forward-compat. Raises ``ValueError`` for malformed data
    or an invalid ``workplace_type``.
    """
    if not locations:
        return []
    if all(isinstance(loc, JobLocation) for loc in locations):
        return list(locations)
    return from_json(json.dumps(locations, default=_location_json_default, ensure_ascii=False))

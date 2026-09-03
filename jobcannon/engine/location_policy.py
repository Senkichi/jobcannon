# PORTED from job_finder/web/location_policy.py @ 80f7668ed61d9da522cba64bd79c1232bb80f36f (private job-cannon). Ledger L-0196.
"""Deterministic location policy engine (Issue #1210).

Computes a versioned, fingerprinted ``LocationPolicy`` verdict from structured
location facts, candidate config, and target-market geography.  This module is
a pure-computation foundation; downstream issues wire it into scoring and
persistence. (# PORT-SEAM: "target-market" genericizes the private repo's
"Bay Area" phrasing throughout this module -- see the paragraph below.)

Genericized at port time (Ledger L-0196): the private repo's owner-specific
target market (hardcoded ``region_code == "CA"``, a 291-city Bay Area seed
list, and a ``"San Francisco"`` primary-city fallback) does NOT carry — that
seed list is Ledger L-0149 (DIES, ``job_finder/web/bay_area_geography.py``,
"owner-specific data that doesn't generalize to hosted multi-tenant users").
Every tenant supplies its own target country/region/metro-city set via
``config["profile"]["location_policy"]``; the classification LOGIC (primary
vs. metro vs. outside-target tiering) is unchanged and fully portable. A
config with no ``location_policy`` block degrades permissively: no region
gate, no metro/primary tiering (everything in the target country resolves
``outside_target`` at the city-tier step, same as an empty extra_cities set
did in the original), never a crash.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from jobcannon.engine.location_canonical import normalize_workplace_type

# PORT-SEAM: config helpers below replace job_finder.config (hop1 DIES).
# Genuinely host-agnostic pure functions (_normalize_target_locations,
# _drop_key_recursive, get_remote_eligible_countries) are carried verbatim;
# get_location_policy_config/_hash are extended with the target-market keys
# documented in the module docstring above so the owner-specific gate becomes
# tenant-configurable instead of hardcoded.

DEFAULT_REMOTE_ELIGIBLE_COUNTRIES: frozenset[str] = frozenset({"US"})
DEFAULT_LOCATION_POLICY_MAX_RADIUS_MILES = 50
DEFAULT_LOCATION_POLICY_GEOCODING_ENABLED = False
# Generic default target country; unlike the private repo's hardcoded
# region_code == "CA" gate, the region is left unset (None) by default so a
# tenant with no location_policy config gets no region-level rejection.
DEFAULT_TARGET_COUNTRY_CODE = "US"


def _normalize_target_locations(values: Any) -> list[str]:
    """Normalize profile.target_locations for hashing; preserves order."""
    if not isinstance(values, (list, tuple)):
        return []
    return [str(v).strip().lower() for v in values if v and str(v).strip()]


def _drop_key_recursive(obj: object, key: str) -> object:
    """Recursively drop all occurrences of ``key`` from dicts in a JSON-ish tree."""
    if isinstance(obj, dict):
        return {k: _drop_key_recursive(v, key) for k, v in obj.items() if k != key}
    if isinstance(obj, list):
        return [_drop_key_recursive(item, key) for item in obj]
    return obj


def get_remote_eligible_countries(config: dict) -> frozenset[str]:
    """Return the set of countries where remote work is eligible.

    Defaults to ``{"US"}``.  Values are uppercased and stripped.
    """
    if not isinstance(config, dict):
        return DEFAULT_REMOTE_ELIGIBLE_COUNTRIES
    profile = config.get("profile") or {}
    countries = profile.get("remote_eligible_countries")
    if not isinstance(countries, (list, tuple, set, frozenset)):
        return DEFAULT_REMOTE_ELIGIBLE_COUNTRIES
    result = {str(c).strip().upper() for c in countries if c and str(c).strip()}
    return frozenset(result) if result else DEFAULT_REMOTE_ELIGIBLE_COUNTRIES


def normalize_profile_location_policy(cfg: dict) -> dict:
    """Merge location-policy defaults, strip invalid entries, return a new config dict.

    User-supplied ``profile.location_policy.bay_area_cities`` (the tenant's
    own target-metro city list; the key name is kept for config-shape
    stability) is normalized to lowercased tokens. ``target_region_code``/
    ``target_country_code``/``primary_city_fallback`` are passed through
    verbatim (``None`` when absent — no private-repo hardcoded default).
    ``max_radius_miles`` and ``geocoding_enabled`` get code-level defaults
    when absent or unparseable. The input dict is never mutated.
    """
    if not isinstance(cfg, dict):
        cfg = {}
    profile = dict(cfg.get("profile") or {})
    user_block = profile.get("location_policy")
    if not isinstance(user_block, dict):
        user_block = {}

    extras = user_block.get("bay_area_cities")
    normalized_extras: frozenset[str] = frozenset()
    if isinstance(extras, (list, tuple, set, frozenset)):
        normalized_extras = frozenset(
            norm
            for c in extras
            if c is not None and str(c).strip()
            for norm in [normalize_city(str(c))]
            if norm and any(ch.isalpha() for ch in norm)
        )

    max_radius = user_block.get("max_radius_miles", DEFAULT_LOCATION_POLICY_MAX_RADIUS_MILES)
    if not isinstance(max_radius, int):
        try:
            max_radius = int(max_radius)
        except (TypeError, ValueError):
            max_radius = DEFAULT_LOCATION_POLICY_MAX_RADIUS_MILES

    geo_enabled = user_block.get("geocoding_enabled", DEFAULT_LOCATION_POLICY_GEOCODING_ENABLED)
    if not isinstance(geo_enabled, bool):
        if isinstance(geo_enabled, str):
            geo_enabled = geo_enabled.lower() in ("true", "1", "yes", "on")
        else:
            geo_enabled = bool(geo_enabled)

    target_region = user_block.get("target_region_code")
    target_region = str(target_region).strip().upper() if target_region else None
    target_country = user_block.get("target_country_code")
    target_country = (
        str(target_country).strip().upper() if target_country else DEFAULT_TARGET_COUNTRY_CODE
    )
    primary_fallback = user_block.get("primary_city_fallback")
    primary_fallback = str(primary_fallback).strip() if primary_fallback else None

    new_block = {
        "bay_area_cities": normalized_extras,
        "max_radius_miles": max_radius,
        "geocoding_enabled": geo_enabled,
        "target_region_code": target_region,
        "target_country_code": target_country,
        "primary_city_fallback": primary_fallback,
    }
    new_profile = {**profile, "location_policy": new_block}
    return {**cfg, "profile": new_profile}


def get_location_policy_config(config: dict) -> dict:
    """Return a fully defaulted ``profile.location_policy`` block.

    The returned dict contains ``bay_area_cities`` (tenant metro-city set),
    ``max_radius_miles``, ``geocoding_enabled``, ``target_region_code``
    (nullable — no region gate when unset), ``target_country_code``
    (defaults ``"US"``), and ``primary_city_fallback`` (nullable).
    """
    normalized = normalize_profile_location_policy(config or {})
    block = normalized.get("profile", {}).get("location_policy") or {}
    return {
        "bay_area_cities": block.get("bay_area_cities", frozenset()),
        "max_radius_miles": int(
            block.get("max_radius_miles", DEFAULT_LOCATION_POLICY_MAX_RADIUS_MILES)
        ),
        "geocoding_enabled": bool(
            block.get("geocoding_enabled", DEFAULT_LOCATION_POLICY_GEOCODING_ENABLED)
        ),
        "target_region_code": block.get("target_region_code"),
        "target_country_code": block.get("target_country_code", DEFAULT_TARGET_COUNTRY_CODE),
        "primary_city_fallback": block.get("primary_city_fallback"),
    }


def get_location_policy_config_hash(config: dict) -> str:
    """Return a SHA-256 hash of the policy-relevant profile subset.

    The subset includes ``home_country``, ``target_locations`` (order
    matters), ``work_arrangement``, ``remote_eligible_countries`` (sorted),
    and the normalized ``location_policy`` block (including the
    target-market keys). Any ``last_rescored_config_hash`` key anywhere in
    the subset is excluded.
    """
    profile = (config or {}).get("profile") or {}
    lp_config = get_location_policy_config(config or {})
    subset = {
        "home_country": _norm_code(profile.get("home_country")),
        "target_locations": _normalize_target_locations(profile.get("target_locations")),
        "work_arrangement": _norm_str(profile.get("work_arrangement")),
        "remote_eligible_countries": sorted(get_remote_eligible_countries(config or {})),
        "location_policy": {
            "bay_area_cities": sorted(lp_config["bay_area_cities"]),
            "max_radius_miles": lp_config["max_radius_miles"],
            "geocoding_enabled": lp_config["geocoding_enabled"],
            "target_region_code": lp_config["target_region_code"],
            "target_country_code": lp_config["target_country_code"],
            "primary_city_fallback": lp_config["primary_city_fallback"],
        },
    }
    subset = _drop_key_recursive(subset, "last_rescored_config_hash")
    canonical = json.dumps(subset, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# PORT-SEAM: generic geography-tier matching, factored out of Ledger
# L-0149 (job_finder/web/bay_area_geography.py, DIES). The 291-city Bay Area
# seed list does NOT carry (owner-specific); the matching LOGIC below is
# host-agnostic and fully portable — ``BAY_AREA_CITIES`` ships empty, so
# tiering is driven entirely by each tenant's own ``extra_cities`` config.

_ZIP_RE = re.compile(r"\b\d{5}(-\d{4})?\b\s*$")
# US state/Canadian province full names and 2-letter abbreviations, for
# stripping a trailing state/province from a free-text city string. Fully
# generic (no owner-specific data) — carried verbatim from Ledger L-0149.
_STATE_NAME_ALIASES: frozenset[str] = frozenset(
    {
        "california",
        "new york",
        "new jersey",
        "florida",
        "texas",
        "illinois",
        "pennsylvania",
        "ohio",
        "georgia",
        "north carolina",
        "michigan",
        "arizona",
        "massachusetts",
        "tennessee",
        "indiana",
        "missouri",
        "maryland",
        "wisconsin",
        "colorado",
        "minnesota",
        "south carolina",
        "alabama",
        "louisiana",
        "kentucky",
        "oregon",
        "oklahoma",
        "connecticut",
        "utah",
        "iowa",
        "nevada",
        "arkansas",
        "mississippi",
        "kansas",
        "new mexico",
        "nebraska",
        "west virginia",
        "idaho",
        "hawaii",
        "new hampshire",
        "maine",
        "montana",
        "rhode island",
        "delaware",
        "south dakota",
        "north dakota",
        "alaska",
        "vermont",
        "wyoming",
        "ontario",
        "quebec",
        "british columbia",
        "alberta",
        "manitoba",
        "saskatchewan",
        "nova scotia",
        "new brunswick",
        "newfoundland and labrador",
        "prince edward island",
        "northwest territories",
        "nunavut",
        "yukon",
    }
)
_STATE_ABBREVIATIONS: frozenset[str] = frozenset(  # PORT-SEAM: host-agnostic, carried verbatim
    {
        "al",
        "ak",
        "az",
        "ar",
        "ca",
        "co",
        "ct",
        "de",
        "fl",
        "ga",
        "hi",
        "id",
        "il",
        "in",
        "ia",
        "ks",
        "ky",
        "la",
        "me",
        "md",
        "ma",
        "mi",
        "mn",
        "ms",
        "mo",
        "mt",
        "ne",
        "nv",
        "nh",
        "nj",
        "nm",
        "ny",
        "nc",
        "nd",
        "oh",
        "ok",
        "or",
        "pa",
        "ri",
        "sc",
        "sd",
        "tn",
        "tx",
        "ut",
        "vt",
        "va",
        "wa",
        "wv",
        "wi",
        "wy",
        "dc",
        "pr",
        "gu",
        "vi",
        "as",
        "mp",
        "ab",
        "bc",
        "mb",
        "nb",
        "nl",
        "ns",
        "nt",
        "nu",
        "on",
        "pe",
        "qc",
        "sk",
        "yt",
    }
)
# Empty by design (Ledger L-0149, DIES): the private repo's 291-city Bay Area
# seed does not carry. Tenants supply their own target-metro cities via
# ``config["profile"]["location_policy"]["bay_area_cities"]`` (extra_cities).
BAY_AREA_CITIES: frozenset[str] = frozenset()  # PORT-SEAM: empty by design, Ledger L-0149 DIES


def normalize_city(s: str | None) -> str:
    """Strip state/zip, trim, and lower-case a city string.

    The returned token is suitable for case-insensitive membership tests
    against a tenant's configured target-metro city set.
    """
    if not s:
        return ""
    value = str(s).strip()

    # Take only the part before the first comma; this drops state/ZIP suffixes.
    value = value.split(",")[0].strip()

    # Remove a trailing ZIP code.
    value = _ZIP_RE.sub("", value).strip()

    # Remove a trailing 2-letter state/province abbreviation or full state name.
    lowered = value.lower()
    for alias in _STATE_NAME_ALIASES:
        if lowered.endswith(f" {alias}"):
            value = lowered[: -len(alias) - 1].strip()
            break
    else:
        parts = value.rsplit(None, 1)
        if len(parts) == 2 and parts[1].lower() in _STATE_ABBREVIATIONS:
            value = parts[0].strip()

    return value.lower()


def is_bay_area_city(city: str, extra_cities: frozenset[str]) -> bool:
    """Return True iff ``city`` is in the tenant-configured target-metro set.

    ``extra_cities`` must already be lowercased/normalized. This helper does
    not interpret region or country; callers must ensure the target
    country/region match first.
    """
    norm = normalize_city(city)
    if not norm:
        return False
    return norm in BAY_AREA_CITIES or norm in extra_cities


def classify_geography(city: str, primary_city: str | None, extra_cities: frozenset[str]) -> str:
    """Classify ``city`` relative to ``primary_city`` and the target-metro set.

    Returns one of ``primary`` | ``metro`` | ``outside_target``.
    """
    city_norm = normalize_city(city)
    primary_norm = normalize_city(primary_city) if primary_city else ""

    if city_norm and city_norm == primary_norm:
        return "primary"
    if city_norm and (city_norm in BAY_AREA_CITIES or city_norm in extra_cities):
        return "metro"
    return "outside_target"


# Country name / 2-letter code -> ISO-3166-1 alpha-2.
_COUNTRY_NAME_TO_CODE: dict[str, str] = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u s a": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "canada": "CA",
    "united kingdom": "GB",
    "uk": "GB",
    "u.k.": "GB",
    "great britain": "GB",
    "india": "IN",
    "germany": "DE",
    "france": "FR",
    "australia": "AU",
    "china": "CN",
    "japan": "JP",
    "netherlands": "NL",
    "spain": "ES",
    "italy": "IT",
    "switzerland": "CH",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "ireland": "IE",
    "belgium": "BE",
    "austria": "AT",
    "portugal": "PT",
    "poland": "PL",
    "israel": "IL",
    "singapore": "SG",
    "hong kong": "HK",
    "new zealand": "NZ",
    "brazil": "BR",
    "mexico": "MX",
    "south korea": "KR",
    "korea": "KR",
    "russia": "RU",
    "turkey": "TR",
    "united arab emirates": "AE",
    "uae": "AE",
    "u.a.e.": "AE",
}

# Known-good ISO-3166-1 alpha-2 country codes accepted from ambiguous inputs
# (e.g. a parenthetical inside a free-text location string). The bare 2-letter
# country field path is a direct user/config input and stays permissive, but a
# parenthetical capture like "Remote (NY)" must be validated before it is
# promoted to a country code — otherwise a US-state or arbitrary 2-letter token
# flows into ``_classify_remote`` and produces a false active ``ineligible``
# verdict (#1576). This set is the closed world ``normalize_country_code`` can
# reason about without config; codes outside it fall through to ``None`` (the
# unresolved branch) rather than a graded ineligible.
_KNOWN_COUNTRY_CODES: frozenset[str] = frozenset(_COUNTRY_NAME_TO_CODE.values())

# Region (US state / Canadian province/territory) names and 2-letter codes -> ISO-3166-2.
_REGION_NAME_TO_CODE: dict[str, str] = {
    # US states
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
    "puerto rico": "PR",
    "guam": "GU",
    "virgin islands": "VI",
    "american samoa": "AS",
    "northern mariana islands": "MP",
    # US state abbreviations
    "al": "AL",
    "ak": "AK",
    "az": "AZ",
    "ar": "AR",
    "ca": "CA",
    "co": "CO",
    "ct": "CT",
    "de": "DE",
    "fl": "FL",
    "ga": "GA",
    "hi": "HI",
    "id": "ID",
    "il": "IL",
    "in": "IN",
    "ia": "IA",
    "ks": "KS",
    "ky": "KY",
    "la": "LA",
    "me": "ME",
    "md": "MD",
    "ma": "MA",
    "mi": "MI",
    "mn": "MN",
    "ms": "MS",
    "mo": "MO",
    "mt": "MT",
    "ne": "NE",
    "nv": "NV",
    "nh": "NH",
    "nj": "NJ",
    "nm": "NM",
    "ny": "NY",
    "nc": "NC",
    "nd": "ND",
    "oh": "OH",
    "ok": "OK",
    "or": "OR",
    "pa": "PA",
    "ri": "RI",
    "sc": "SC",
    "sd": "SD",
    "tn": "TN",
    "tx": "TX",
    "ut": "UT",
    "vt": "VT",
    "va": "VA",
    "wa": "WA",
    "wv": "WV",
    "wi": "WI",
    "wy": "WY",
    "dc": "DC",
    # Canadian provinces / territories
    "ontario": "ON",
    "quebec": "QC",
    "british columbia": "BC",
    "alberta": "AB",
    "manitoba": "MB",
    "saskatchewan": "SK",
    "nova scotia": "NS",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "prince edward island": "PE",
    "northwest territories": "NT",
    "nunavut": "NU",
    "yukon": "YT",
    # Canadian abbreviations
    "on": "ON",
    "qc": "QC",
    "bc": "BC",
    "ab": "AB",
    "mb": "MB",
    "sk": "SK",
    "ns": "NS",
    "nb": "NB",
    "nl": "NL",
    "pe": "PE",
    "nt": "NT",
    "nu": "NU",
    "yt": "YT",
}


@dataclasses.dataclass(frozen=True)
class LocationPolicy:
    """Deterministic location verdict for a job or posting.

    ``posting_policies`` is populated only on the top-level job verdict when the
    input contains a ``postings`` list; per-posting policies have ``None`` there.
    """

    eligibility: str
    workplace_class: str
    geography_tier: str
    primary_city: str | None
    rank: int
    sort_order: int
    effective_location_fit: int | None
    reason: str
    evidence: list[dict]
    policy_version: str
    input_fingerprint: str
    computed_at: str
    posting_policies: list[LocationPolicy] | None = None


def normalize_country_code(value: str | None) -> str | None:
    """Best-effort mapping of a country string to an ISO-3166-1 alpha-2 code.

    A bare ``"CA"`` in a country field means Canada.  A two-letter code inside
    parentheses (e.g. ``"Remote (US)"``) is extracted, so the parentheses do not
    prevent normalization.  Returns ``None`` when the string cannot be normalized.
    """
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    upper = v.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper
    # Parenthetical country code, e.g. "Remote (US)" or "New York, NY/Remote (US)".
    # Validate the capture against the known-good country code set: a free-text
    # parenthetical can carry a US-state code ("NY"), an arbitrary abbreviation
    # ("AI" from "Engineer (AI)"), or other non-country tokens. Promoting any of
    # those to a country code would route through ``_classify_remote`` and yield
    # a false active ``ineligible`` verdict. Unknown captures fall through to the
    # name-based lookup and ultimately ``None`` (the unresolved branch). (#1576)
    m = re.search(r"\(([A-Z]{2})\)", upper)
    if m and m.group(1) in _KNOWN_COUNTRY_CODES:
        return m.group(1)
    name_key = v.lower()
    if name_key in _COUNTRY_NAME_TO_CODE:
        return _COUNTRY_NAME_TO_CODE[name_key]
    simplified = name_key.replace(".", "").replace(",", "")
    if simplified in _COUNTRY_NAME_TO_CODE:
        return _COUNTRY_NAME_TO_CODE[simplified]
    return None


def normalize_region_code(value: str | None) -> str | None:
    """Best-effort mapping of a region string to an ISO-3166-2 subdivision code.

    A bare ``"CA"`` in a region field means California.  Returns ``None`` when
    the string cannot be normalized.
    """
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    upper = v.upper()
    if len(upper) == 2 and upper.isalpha() and upper in _REGION_NAME_TO_CODE.values():
        return upper
    name_key = v.lower()
    if name_key in _REGION_NAME_TO_CODE:
        return _REGION_NAME_TO_CODE[name_key]
    simplified = name_key.replace(".", "")
    if simplified in _REGION_NAME_TO_CODE:
        return _REGION_NAME_TO_CODE[simplified]
    return None


def _norm_code(value: str | None) -> str | None:
    """Strip and uppercase an ISO code, or None."""
    if value:
        v = str(value).strip().upper()
        if v:
            return v
    return None


def _norm_str(value: str | None) -> str | None:
    """Strip and lowercase a free-text value, or None."""
    if value:
        v = str(value).strip().lower()
        if v:
            return v
    return None


def _title_case_city(name: str) -> str:
    """Best-effort title-casing for a normalized city token."""
    return " ".join(part.capitalize() for part in name.split())


def _detect_primary_city(  # PORT-SEAM: ``fallback`` replaces the private
    target_locations: list, extra_cities: frozenset[str], fallback: str | None = None
) -> str | None:
    """Return the first target_location matching the tenant's target-metro set.

    Strips state/zip, drops the ``"Remote"`` sentinel, and falls back to
    ``fallback`` when no target matches.  # PORT-SEAM: ``fallback`` (config
    ``primary_city_fallback``) replaces the private repo's hardcoded
    ``"San Francisco"`` default -- None when the tenant leaves it unset.
    """
    for target in target_locations or []:
        if not target:
            continue
        candidate = normalize_city(str(target))
        if candidate == "remote" or not candidate:
            continue
        if is_bay_area_city(candidate, extra_cities):
            return _title_case_city(candidate)
    return fallback  # PORT-SEAM: None default (no owner-specific city fallback)


def _normalize_location(loc: Any, fallback_workplace_type: str | None = None) -> dict[str, Any]:
    """Convert a JobLocation-like object to a normalized dict.

    Fills missing ``country_code`` / ``region_code`` from ``country`` /
    ``region`` strings and normalizes the ``workplace_type``.  Unresolved
    locations are NOT filtered here; callers skip them explicitly.
    """
    if isinstance(loc, dict):
        out = dict(loc)
    elif dataclasses.is_dataclass(loc) and not isinstance(loc, type):
        out = dataclasses.asdict(loc)
    else:
        out = dict(loc)

    cc = out.get("country_code")
    if not cc:
        cc = normalize_country_code(out.get("country"))
    else:
        cc = str(cc).strip().upper() if cc else None
    out["country_code"] = cc

    rc = out.get("region_code")
    if not rc:
        rc = normalize_region_code(out.get("region"))
    else:
        rc = str(rc).strip().upper() if rc else None
    out["region_code"] = rc

    wt = normalize_workplace_type(out.get("workplace_type"))
    if wt not in ("REMOTE", "HYBRID", "ONSITE") and fallback_workplace_type:
        wt = normalize_workplace_type(fallback_workplace_type)
    out["workplace_type"] = wt

    return out


def _evidence(loc: dict[str, Any], workplace_class: str, geography_tier: str) -> dict[str, Any]:
    """Build a single evidence dict from a normalized location."""
    return {
        "city": loc.get("city"),
        "region": loc.get("region"),
        "region_code": loc.get("region_code"),
        "country_code": loc.get("country_code"),
        "workplace_class": workplace_class,
        "geography_tier": geography_tier,
    }


def _verdict_fields(
    *,
    workplace_class: str,
    geography_tier: str,
    eligibility: str,
    primary_city: str | None,
    reason: str,
    evidence: list[dict],
    effective_fit: int | None,
    rank: int = 0,
    sort_order: int = 1,
) -> dict[str, Any]:
    """Bundle the common LocationPolicy fields (minus version/fingerprint/time)."""
    return {
        "eligibility": eligibility,
        "workplace_class": workplace_class,
        "geography_tier": geography_tier,
        "primary_city": primary_city,
        "rank": rank,
        "sort_order": sort_order,
        "effective_location_fit": effective_fit,
        "reason": reason,
        "evidence": evidence,
    }


def _unknown_verdict(
    primary_city: str | None, reason: str, evidence: list[dict] | None = None
) -> dict[str, Any]:
    return _verdict_fields(
        workplace_class="unknown",
        geography_tier="unknown",
        eligibility="unknown",
        primary_city=primary_city,
        reason=reason,
        evidence=evidence or [],
        effective_fit=None,
    )


def _classify_remote(
    loc: dict[str, Any],
    remote_eligible: frozenset[str],
    home_country: str | None,
    primary_city: str | None,
) -> dict[str, Any]:
    """Classify a single REMOTE location."""
    country_code = loc.get("country_code")

    if country_code is None:
        if loc.get("workplace_type") != "REMOTE":
            return _unknown_verdict(primary_city, "location with no country information")
        return _verdict_fields(
            workplace_class="remote",
            geography_tier="unknown",
            eligibility="unknown",
            primary_city=primary_city,
            reason="remote with no country information",
            evidence=[_evidence(loc, "remote", "unknown")],
            effective_fit=None,
        )

    if country_code in remote_eligible or (home_country and country_code == home_country):
        return _verdict_fields(
            workplace_class="remote",
            geography_tier="remote",
            eligibility="eligible",
            primary_city=primary_city,
            rank=5,
            sort_order=2,
            effective_fit=5,
            reason=f"remote in {country_code} (eligible)",
            evidence=[_evidence(loc, "remote", "remote")],
        )

    if home_country is None:
        return _verdict_fields(
            workplace_class="remote",
            geography_tier="unknown",
            eligibility="unknown",
            primary_city=primary_city,
            reason=f"remote in {country_code} (home country unknown)",
            evidence=[_evidence(loc, "remote", "unknown")],
            effective_fit=None,
        )

    return _verdict_fields(
        workplace_class="remote",
        geography_tier="outside_target",
        eligibility="ineligible",
        primary_city=primary_city,
        rank=0,
        sort_order=0,
        effective_fit=1,
        reason=f"remote in {country_code} (not eligible)",
        evidence=[_evidence(loc, "remote", "outside_target")],
    )


def _classify_presence(
    loc: dict[str, Any],
    workplace_class: str,
    primary_city: str | None,
    extra_cities: frozenset[str],
    target_country_code: str = DEFAULT_TARGET_COUNTRY_CODE,  # PORT-SEAM: see docstring below
    target_region_code: str | None = None,
) -> dict[str, Any]:
    """Classify a single HYBRID or ONSITE location.

    # PORT-SEAM: target_country_code/target_region_code (config-driven, see
    # get_location_policy_config) replace the private repo's hardcoded
    # ``!= "US"`` / ``!= "CA"`` gate. target_region_code=None (the tenant
    # default) skips the region-level gate entirely — falls through to
    # city-tier classification for any region in the target country.
    """
    country_code = loc.get("country_code")
    region_code = loc.get("region_code")
    city = loc.get("city")

    if country_code is not None and country_code != target_country_code:  # PORT-SEAM:
        return _verdict_fields(
            workplace_class=workplace_class,
            geography_tier="outside_target",
            eligibility="ineligible",
            primary_city=primary_city,
            rank=0,
            sort_order=0,
            effective_fit=1,
            reason=f"{workplace_class} in {country_code} (outside target)",
            evidence=[_evidence(loc, workplace_class, "outside_target")],
        )

    if (  # PORT-SEAM: None target_region_code skips the region gate entirely
        target_region_code is not None
        and region_code is not None
        and region_code != target_region_code
    ):
        return _verdict_fields(
            workplace_class=workplace_class,
            geography_tier="outside_target",
            eligibility="ineligible",
            primary_city=primary_city,
            rank=0,
            sort_order=0,
            effective_fit=1,
            reason=f"{workplace_class} in {region_code} (outside target)",
            evidence=[_evidence(loc, workplace_class, "outside_target")],
        )

    if country_code is None or region_code is None:
        return _unknown_verdict(
            primary_city,
            f"{workplace_class} with insufficient location data",
            evidence=[_evidence(loc, workplace_class, "unknown")],
        )

    if not city:
        return _unknown_verdict(
            primary_city,
            f"{workplace_class} with missing city",
            evidence=[_evidence(loc, workplace_class, "unknown")],
        )

    tier = classify_geography(city, primary_city, extra_cities)

    if tier == "primary":
        rank, effective_fit = (4, 4) if workplace_class == "hybrid" else (2, 2)
        return _verdict_fields(
            workplace_class=workplace_class,
            geography_tier="primary",
            eligibility="eligible",
            primary_city=primary_city,
            rank=rank,
            sort_order=2,
            effective_fit=effective_fit,
            reason=f"{workplace_class} in {city}, {region_code}, {country_code} (primary target)",
            evidence=[_evidence(loc, workplace_class, "primary")],
        )

    if tier == "metro":
        rank, effective_fit = (3, 3) if workplace_class == "hybrid" else (1, 2)
        return _verdict_fields(
            workplace_class=workplace_class,
            geography_tier="metro",
            eligibility="eligible",
            primary_city=primary_city,
            rank=rank,
            sort_order=2,
            effective_fit=effective_fit,
            reason=f"{workplace_class} in {city}, {region_code}, {country_code} (metro)",
            evidence=[_evidence(loc, workplace_class, "metro")],
        )

    return _verdict_fields(
        workplace_class=workplace_class,
        geography_tier="outside_target",
        eligibility="ineligible",
        primary_city=primary_city,
        rank=0,
        sort_order=0,
        effective_fit=1,
        reason=f"{workplace_class} in {city}, {region_code}, {country_code} (outside target)",
        evidence=[_evidence(loc, workplace_class, "outside_target")],
    )


def _evaluate_single(
    locations_structured: list | None,
    workplace_type: str | None,
    primary_country_code: str | None,
    remote_eligible: frozenset[str],
    extra_cities: frozenset[str],
    home_country: str | None,
    primary_city: str | None,
    target_country_code: str = DEFAULT_TARGET_COUNTRY_CODE,  # PORT-SEAM: threaded through
    target_region_code: str | None = None,
) -> dict[str, Any]:
    """Evaluate one location set (a posting or the row-level set)."""
    resolved: list[dict[str, Any]] = []
    for loc in locations_structured or []:
        if isinstance(loc, dict) and loc.get("unresolved"):
            continue
        if getattr(loc, "unresolved", False):
            continue
        resolved.append(_normalize_location(loc, fallback_workplace_type=workplace_type))

    if not resolved and (workplace_type or primary_country_code):
        resolved = [
            {
                "workplace_type": normalize_workplace_type(workplace_type),
                "country_code": _norm_code(primary_country_code),
                "city": None,
                "region": None,
                "country": None,
                "region_code": None,
                "unresolved": False,
                "raw": "",
            }
        ]

    if not resolved:
        return _unknown_verdict(primary_city, "no location data available")

    candidates: list[dict[str, Any]] = []
    for loc in resolved:
        wt = loc.get("workplace_type")
        if wt == "REMOTE":
            candidate = _classify_remote(loc, remote_eligible, home_country, primary_city)
        elif wt in ("HYBRID", "ONSITE"):
            candidate = _classify_presence(  # PORT-SEAM: pass-through, see def above
                loc, wt.lower(), primary_city, extra_cities, target_country_code, target_region_code
            )
        else:
            candidate = _unknown_verdict(
                primary_city,
                f"unspecified workplace type for {loc.get('city') or 'location'}",
                evidence=[_evidence(loc, "unknown", "unknown")],
            )
        candidates.append(candidate)

    return max(candidates, key=lambda c: (c["sort_order"], c["rank"]))


def _canonicalize_location(loc: Any) -> dict[str, Any]:
    """Convert a location value to a stable, serializable dict for hashing."""
    if isinstance(loc, dict):
        src = loc
    elif dataclasses.is_dataclass(loc) and not isinstance(loc, type):
        src = dataclasses.asdict(loc)
    else:
        src = dict(loc)

    def _val(key: str, value: Any) -> Any:
        if value is None:
            return None
        if key == "unresolved":
            return bool(value)
        if key in ("region_code", "country_code"):
            return str(value).strip().upper()
        if key == "workplace_type":
            return str(value).strip().upper()
        return str(value).strip().lower()

    return {
        "city": _val("city", src.get("city")),
        "region": _val("region", src.get("region")),
        "region_code": _val("region_code", src.get("region_code")),
        "country": _val("country", src.get("country")),
        "country_code": _val("country_code", src.get("country_code")),
        "workplace_type": _val("workplace_type", src.get("workplace_type")),
        "unresolved": _val("unresolved", src.get("unresolved")),
    }


def _canonicalize_posting(posting: Any) -> dict[str, Any]:
    """Convert a posting value to a stable, serializable dict for hashing."""
    if isinstance(posting, dict):
        src = posting
    elif dataclasses.is_dataclass(posting) and not isinstance(posting, type):
        src = dataclasses.asdict(posting)
    else:
        src = dict(posting)
    out: dict[str, Any] = {}
    for key in sorted(src.keys()):
        value = src[key]
        if key == "locations_structured":
            out[key] = [_canonicalize_location(loc) for loc in (value or [])]
        elif key in ("workplace_type", "primary_country_code"):
            out[key] = str(value).strip().upper() if value else None
        else:
            out[key] = value
    return out


def compute_input_fingerprint(
    locations_structured: list | None = None,
    workplace_type: str | None = None,
    primary_country_code: str | None = None,
    postings: list | None = None,
    config: dict | None = None,
) -> str:
    """Return a SHA-256 fingerprint of job location inputs + policy-relevant config."""
    payload = {
        "locations_structured": [
            _canonicalize_location(loc) for loc in (locations_structured or [])
        ],
        "workplace_type": _norm_code(workplace_type),
        "primary_country_code": _norm_code(primary_country_code),
        "postings": [_canonicalize_posting(p) for p in (postings or [])],
        "policy_config_hash": get_location_policy_config_hash(config or {}),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _policy_to_dict(policy: LocationPolicy) -> dict[str, Any]:
    """Recursively convert a LocationPolicy to a JSON-serializable dict.

    The ``effective_location_fit`` field is always emitted, including as
    ``null`` when the policy could not resolve a location.  That sentinel is
    part of the public data format and must not be silently dropped.
    """
    result: dict[str, Any] = {}
    for field in dataclasses.fields(policy):
        value = getattr(policy, field.name)
        if value is None and field.name != "effective_location_fit":
            continue
        if isinstance(value, LocationPolicy):
            result[field.name] = _policy_to_dict(value)
        elif isinstance(value, list):
            result[field.name] = [
                _policy_to_dict(item) if isinstance(item, LocationPolicy) else item
                for item in value
            ]
        else:
            result[field.name] = value
    return result


def verdict_to_json(policy: LocationPolicy) -> str:
    """Serialize a LocationPolicy to JSON."""
    return json.dumps(_policy_to_dict(policy), ensure_ascii=False, sort_keys=True)


def json_to_verdict(s: str) -> dict[str, Any]:
    """Parse a LocationPolicy JSON string back to a plain dict."""
    return json.loads(s)


def location_policy_from_verdict(verdict_json: str | None) -> LocationPolicy | None:
    """Reconstruct a ``LocationPolicy`` from a stored verdict JSON string.

    The inverse of ``verdict_to_json``: round-trips a verdict produced by
    ``_policy_to_dict`` back into a ``LocationPolicy`` so callers that need the
    typed object (e.g. ``persist_job_assessment``'s ``location_policy=`` kwarg)
    can reuse a STORED verdict without recomputing the policy from location
    facts. Used by ``scripts/redrive_classification.py``'s ``remediate`` to
    thread the stored verdict into the sanctioned writer (#1703), keeping the
    redrive's write path coherent with the stored-verdict read path
    ``find_divergences`` now uses via ``effective_sub_scores``.

    Returns ``None`` for a missing/empty/malformed verdict (the no-policy case
    — callers fall back to recomputing or to ``location_policy=None``).
    """
    if not verdict_json:
        return None
    try:
        data = json.loads(verdict_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _policy_from_dict(data)


def _policy_from_dict(data: dict[str, Any]) -> LocationPolicy:
    """Recursive core of ``location_policy_from_verdict``."""
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(LocationPolicy):
        if field.name in data:
            value = data[field.name]
            if field.name == "posting_policies" and isinstance(value, list):
                value = [_policy_from_dict(pp) if isinstance(pp, dict) else pp for pp in value]
            kwargs[field.name] = value
        elif field.name == "posting_policies":
            # _policy_to_dict skips None posting_policies; default is None.
            kwargs[field.name] = None
    # effective_location_fit is always emitted by _policy_to_dict (even as
    # null); every other field is present iff non-None, so missing required
    # fields default to None — matching the original policy's value.
    for field in dataclasses.fields(LocationPolicy):
        kwargs.setdefault(field.name, None)
    return LocationPolicy(**kwargs)


def is_unresolved_location_policy(verdict_json: str | None) -> bool:
    """Return True when a stored verdict reflects an unresolved location.

    The policy is unresolved when it could not make a graded judgment.  New
    verdicts store ``effective_location_fit: null`` and old verdicts store the
    legacy integer 2, so the canonical signal is the combination of
    ``eligibility='unknown'`` and ``geography_tier='unknown'``.
    """
    if not verdict_json:
        return False
    try:
        data = json.loads(verdict_json)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("eligibility") == "unknown" and data.get("geography_tier") == "unknown"


_LOCATION_FIT_COLORS: dict[str, str] = {
    "eligible": "bg-emerald-500",
    "unknown": "bg-amber-500",
    "ineligible": "bg-red-500",
}


def apply_location_policy_to_postings(
    postings: list,
    location_policy: LocationPolicy,
) -> list:
    """Return a new postings list enriched with per-posting policy verdicts.

    Each posting dict receives:
      - ``location_fit`` = the per-posting policy ``rank``
      - ``location_fit_color`` based on the per-posting ``eligibility``
      - ``location_policy_verdict`` = the per-posting verdict dict

    The input list and posting dicts are not mutated.  The helper is reused by
    the scoring orchestrator and by the upcoming backfill issue.
    """
    if not postings or not location_policy.posting_policies:
        return list(postings)

    enriched: list = []
    for posting, policy in zip(postings, location_policy.posting_policies, strict=True):
        if isinstance(posting, dict):
            new_posting = dict(posting)
        else:
            new_posting = dataclasses.asdict(posting)
        verdict_dict = _policy_to_dict(policy)
        new_posting["location_fit"] = policy.rank
        new_posting["location_fit_color"] = _LOCATION_FIT_COLORS.get(
            policy.eligibility, "bg-amber-500"
        )
        new_posting["location_policy_verdict"] = verdict_dict
        enriched.append(new_posting)
    return enriched


def compute_location_policy(
    locations_structured: list | None = None,
    workplace_type: str | None = None,
    primary_country_code: str | None = None,
    postings: list | None = None,
    config: dict | None = None,
    *,
    now: datetime | None = None,
    has_subcountry_constraint: bool = False,
) -> LocationPolicy | None:
    """Compute the deterministic LocationPolicy for a job or posting.

    Args:
        locations_structured: Row-level list of JobLocation dicts.
        workplace_type: Row-level fallback workplace type.
        primary_country_code: Row-level fallback country code.
        postings: Optional list of posting dicts, each with ``locations_structured``.
        config: Full config dict (uses ``profile.*`` keys).
        now: Optional datetime for the ``computed_at`` timestamp; defaults to naive UTC now.
        has_subcountry_constraint: #1202 — when True, the JD carries a
            geographic/residency constraint FINER than country/region/city
            (e.g. a remote role restricted to a named subset of US states)
            that the ``locations_structured`` schema cannot represent. The
            rule table would mis-fire (e.g. ``_classify_remote`` returning
            ``rank=5`` for a REMOTE-in-home-country posting the candidate
            cannot actually take). Return ``None`` so the caller skips the
            policy override and the LLM's own judgment is authoritative —
            the same semantics as ``compute_location_fit`` returning ``None``.
            This is a gate on the existing override, not a parallel scoring
            path.
    """
    # #1202: sub-country constraint gate. Short-circuit to None so the caller
    # (scoring_orchestrator / location_policy_rescore) skips the policy
    # override and the LLM's own location_fit sub-score drives classification.
    if has_subcountry_constraint:
        return None

    cfg = config or {}
    profile = cfg.get("profile") or {}

    remote_eligible = get_remote_eligible_countries(cfg)
    lp_config = get_location_policy_config(cfg)
    extra_cities = lp_config["bay_area_cities"]
    target_country_code = lp_config["target_country_code"]  # PORT-SEAM: tenant config, see above
    target_region_code = lp_config["target_region_code"]
    home_country = _norm_code(profile.get("home_country"))
    target_locations = profile.get("target_locations") or []
    primary_city = _detect_primary_city(  # PORT-SEAM: primary_city_fallback, see def above
        target_locations, extra_cities, lp_config["primary_city_fallback"]
    )

    computed_at = (now or datetime.now(UTC).replace(tzinfo=None)).isoformat()

    posting_policies: list[LocationPolicy] | None = None
    best_fields: dict[str, Any]

    if postings:
        posting_policies = []
        candidates: list[dict[str, Any]] = []
        for posting in postings:
            if isinstance(posting, dict):
                p_locs = posting.get("locations_structured")
                p_wt = posting.get("workplace_type", workplace_type)
                p_pcc = posting.get("primary_country_code", primary_country_code)
            else:
                p_locs = getattr(posting, "locations_structured", None)
                p_wt = getattr(posting, "workplace_type", workplace_type)
                p_pcc = getattr(posting, "primary_country_code", primary_country_code)

            fields = _evaluate_single(  # PORT-SEAM: target_country_code/region_code pass-through
                p_locs,
                p_wt,
                p_pcc,
                remote_eligible,
                extra_cities,
                home_country,
                primary_city,
                target_country_code,  # PORT-SEAM: tenant target country/region
                target_region_code,
            )
            candidates.append(fields)
            posting_policies.append(
                LocationPolicy(
                    **fields,
                    policy_version="location-policy-v1",
                    input_fingerprint=compute_input_fingerprint(
                        locations_structured=p_locs,
                        workplace_type=p_wt,
                        primary_country_code=p_pcc,
                        config=cfg,
                    ),
                    computed_at=computed_at,
                    posting_policies=None,
                )
            )
        best_fields = max(candidates, key=lambda c: (c["sort_order"], c["rank"]))
    else:
        best_fields = _evaluate_single(  # PORT-SEAM: target_country_code/region_code pass-through
            locations_structured,
            workplace_type,
            primary_country_code,
            remote_eligible,
            extra_cities,
            home_country,
            primary_city,
            target_country_code,  # PORT-SEAM: tenant target country/region
            target_region_code,
        )

    input_fingerprint = compute_input_fingerprint(
        locations_structured=locations_structured,
        workplace_type=workplace_type,
        primary_country_code=primary_country_code,
        postings=postings,
        config=cfg,
    )

    return LocationPolicy(
        **best_fields,
        policy_version="location-policy-v1",
        input_fingerprint=input_fingerprint,
        computed_at=computed_at,
        posting_policies=posting_policies,
    )

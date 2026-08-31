"""Location parsing: freeform string → ``list[JobLocation]``.

This is the Layer 2 (gazetteer) + Layer 3 (heuristic) path. Layer 1 lives in
the individual ATS scanners (`_platforms_smartrecruiters.py`,
`_platforms_ashby.py`, `_platforms_lever.py`, `_platforms_rippling.py`) —
scanners that emit structured data write `JobLocation` directly, bypassing
this parser entirely.

Top-level entry point is ``parse_locations``. Callers pass either a single
freeform string or a list of pre-split strings (as `locations_raw` carries).
The parser:

  1. Strips and splits each input on the existing
     `location_normalizer.split_multi_locations` separators, which is
     already battle-tested in the dropdown path.
  2. Detects ``workplace_type`` from inline tokens (``Remote``, ``Hybrid``,
     ``On-Site``, plus LinkedIn ``#LI-Remote`` / ``#LI-Hybrid`` / ``#LI-Onsite``
     tags) and strips the token from the remaining location string.
  3. Anchors a country from the trailing segment via ``pycountry`` (alpha-2,
     alpha-3, full name, plus a small alias map for ``UK`` → ``GB``,
     ``USA`` → ``US``).
  4. Matches a region (subdivision) via ``pycountry.subdivisions`` once
     anchored — the input segment is the source of truth, the gazetteer is
     used only for city existence + tiebreaks.
  5. Matches a city via ``geonamescache``, scoped by country/region when
     known, falling back to population-weighted tiebreak when ambiguous.
  6. On total parse failure, emits ``JobLocation.unresolved_from_raw`` so
     ingestion never blocks.

Never raises. Empty input → ``[]``. Pure-placeholder input
(``"Multiple Locations"``, ``"TBD"``, ``"Unknown"``) → ``[]`` per the
existing normalizer's placeholder set, EXCEPT when the normalizer would
have dropped a workplace token alongside the placeholder (e.g.
``"Remote - TBD"`` → ``[JobLocation(workplace_type=REMOTE, unresolved=True)]``).

Module-level gazetteer caches (``_GC_CITIES``, ``_PYCOUNTRY_*``) are built
lazily on first call. Re-instantiating geonamescache costs ~150ms; the
cache makes repeat parses cheap.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any, cast

import geonamescache
import pycountry

from jobcannon.engine.location_canonical import (
    JobLocation,
    WorkplaceType,
    dedupe_locations,
)
from jobcannon.engine.location_normalizer import (
    normalize_location,
    split_multi_locations,
)

logger = logging.getLogger(__name__)

# ─── Workplace-type detection ────────────────────────────────────────
#
# Match word-boundaried tokens, case-insensitive. The detected token is
# STRIPPED from the location string so it doesn't pollute the city/region
# segment match downstream. LinkedIn hashtag forms appear when callers
# pass a JD body excerpt; JD-body fallback beyond that is left as a
# separate parameter for now — `jd_full` plumbing comes later.

_REMOTE_TOKEN_RE = re.compile(
    r"\b(?:remote(?:[\s\-]*only)?|#LI[\-]Remote|fully\s+remote)\b",
    re.IGNORECASE,
)
_HYBRID_TOKEN_RE = re.compile(r"\b(?:hybrid|#LI[\-]Hybrid)\b", re.IGNORECASE)
_ONSITE_TOKEN_RE = re.compile(
    r"\b(?:on[\s\-]?site|in[\s\-]?office|#LI[\-]Onsite)\b",
    re.IGNORECASE,
)

# JD-body LinkedIn workplace tags (SPEC Q3). Only the high-signal hashtag
# forms — bare ``remote`` / ``hybrid`` / ``onsite`` in JD prose are too
# false-positive-prone ("remote possibility", "hybrid model") to use as a
# workplace_type signal. The hashtag forms are LinkedIn-specific and
# almost never appear in body prose for any other reason.
_LI_REMOTE_BODY_RE = re.compile(r"#LI[\-\s]?Remote\b", re.IGNORECASE)
_LI_HYBRID_BODY_RE = re.compile(r"#LI[\-\s]?Hybrid\b", re.IGNORECASE)
_LI_ONSITE_BODY_RE = re.compile(r"#LI[\-\s]?Onsite\b", re.IGNORECASE)

# ─── Country aliasing ────────────────────────────────────────────────
#
# pycountry's alpha_2 lookup for "UK" maps to Uganda (UG), which is the
# documented behavior — UK is an ISO 3166-1 *reserved* code, not an
# *assigned* alpha-2. Same for a few other community-standard aliases:
# we hand-map them to the right country before pycountry sees the token.
# Order matters only in that all keys here take precedence over the
# pycountry lookup.

_COUNTRY_ALIASES: dict[str, str] = {
    "UK": "GB",
    "U.K.": "GB",
    "USA": "US",
    "U.S.A.": "US",
    "U.S.": "US",
    "AMERICA": "US",
    # "Europe" + "EU" and "EMEA" intentionally NOT mapped — they're regions,
    # not countries. The SPEC's anchor "Remote, EMEA" expects unresolved=True
    # with country_code=None. Leaving them out makes the parser correctly
    # decline to anchor a country.
}

# ─── Ambiguous codes ─────────────────────────────────────────────────
#
# A handful of two-letter strings are both a US state code AND a ISO
# 3166-1 country alpha-2. The most-common collisions:
#   "CA" — California vs. Canada
#   "GA" — Georgia (US) vs. Gabon (country code GA)
#   "LA" — Louisiana vs. Lao PDR (LA)
#   "AL" — Alabama vs. Albania (AL)
#   "AR" — Arkansas vs. Argentina (AR)
#   "MO" — Missouri vs. Macao (MO)
#   "MS" — Mississippi vs. Montserrat (MS)
#   "DE" — Delaware vs. Germany (DE)
#   "ID" — Idaho vs. Indonesia (ID)
#   "IN" — Indiana vs. India (IN)
#   "MT" — Montana vs. Malta (MT)
#   "NE" — Nebraska vs. Niger (NE)
#   "PA" — Pennsylvania vs. Panama (PA)
#   "SC" — South Carolina vs. Seychelles (SC)
#   "SD" — South Dakota vs. South Sudan (SS)... actually SD = Sudan.
#
# Decision: when a 2-letter segment in the COUNTRY-ANCHOR position (last
# segment) is also a US state code, prefer the country only when there
# is no other US signal in the string. If there's already a city that
# disambiguates (San Francisco → US), or the preceding segment looks
# like "City, ST" → "ST" is the state code, we keep US.
#
# In practice the SPEC anchor corpus only exercises "CA". The set below
# is defensive — if the last segment is one of these AND the preceding
# segments don't already pin a different country, we treat it as a US
# state (anchor=US).

_US_STATE_CODES: frozenset[str] = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    }
)

# Heuristic-only mapping for Layer 3. "Multiple Locations" is already in
# the existing normalizer's placeholder set and gets dropped before it
# reaches us — no work here. The "Remote - <country>" shape is the
# common LinkedIn-emitted form for fully-remote postings.

_REMOTE_DASH_COUNTRY_RE = re.compile(
    r"^\s*remote\s*[\-–—]\s*(.+?)\s*$",
    re.IGNORECASE,
)

# Trailing "/ Remote" / "/ Hybrid" / "/ On-Site" workplace-promotion pattern.
# Distinct from the other multi-location separators: `/` followed by a bare
# workplace token at the end of the string means "this posting is in <location>
# with <workplace> mode", not "this posting is in <location> OR <workplace>".
# The SPEC anchor `"San Francisco, CA / Remote"` expects one row with workplace
# promoted, while `"London, UK or Remote"` expects two distinct rows — the
# difference is the separator. Only the trailing `/` form triggers promotion.
_TRAILING_SLASH_WORKPLACE_RE = re.compile(
    r"^(.*?)\s+/\s+(remote|hybrid|on[\s\-]?site|in[\s\-]?office|fully\s+remote)\s*$",
    re.IGNORECASE,
)


# ─── Gazetteer caches ────────────────────────────────────────────────
#
# geonamescache.GeonamesCache() loads ~26k cities into a dict — ~150ms
# at first instantiation. The module-level lazy build + lru_cache on the
# index lookups keeps subsequent parses to microseconds.


@lru_cache(maxsize=1)
def _gc_cities() -> dict[str, dict[str, Any]]:
    """Return geonamescache's city dict keyed by GeoNames ID.

    Each value is a dict with keys (per geonamescache 2.x):
        name, latitude, longitude, countrycode, population, timezone,
        admin1code, alternatenames, ...
    """
    gc = geonamescache.GeonamesCache()
    return cast("dict[str, dict[str, Any]]", gc.get_cities())


@lru_cache(maxsize=1)
def _cities_by_name() -> dict[str, list[dict[str, Any]]]:
    """Build a name→[city...] index from geonamescache.

    Lowercase-keyed for case-insensitive lookup. A single name may map to
    many cities (Springfield) — disambiguation happens in the matcher.
    Also indexes ``alternatenames`` so common short forms work:
      - "New York" → "New York City" (NYC's alt list)
      - "Bangalore" → "Bengaluru" (legacy name in IN's alt list)

    geonamescache 2.x stores ``alternatenames`` as a Python list of
    strings (NOT comma-separated). Earlier versions used a string —
    handled defensively below so a version bump doesn't silently break
    the index.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    # Track (name_key, geonames_id) pairs already added so the same city
    # doesn't appear twice under one name when its primary AND an
    # alternatename happen to share casing/whitespace variants.
    seen: dict[str, set[Any]] = {}

    def _add(name_key: str, city_dict: dict[str, Any]) -> None:
        gid = city_dict.get("geonameid") or id(city_dict)
        if seen.setdefault(name_key, set()).__contains__(gid):
            return
        seen[name_key].add(gid)
        index.setdefault(name_key, []).append(city_dict)

    for city in _gc_cities().values():
        primary = (city.get("name") or "").strip()
        if primary:
            _add(primary.lower(), city)
        alts = city.get("alternatenames", []) or []
        alt_iter: list[str]
        if isinstance(alts, list):
            alt_iter = [a for a in alts if isinstance(a, str)]
        elif isinstance(alts, str):
            alt_iter = alts.split(",")
        else:
            alt_iter = []
        for alt in alt_iter:
            alt = alt.strip()
            if alt:
                _add(alt.lower(), city)
    return index


@lru_cache(maxsize=1)
def _subdivisions_by_country() -> dict[str, list[Any]]:
    """Build a country_code → [Subdivision...] index from pycountry."""
    out: dict[str, list[Any]] = {}
    for sub in cast("list[Any]", pycountry.subdivisions):
        out.setdefault(sub.country_code, []).append(sub)
    return out


# ─── Country anchor ──────────────────────────────────────────────────


def _lookup_country(token: str) -> tuple[str, str] | None:
    """Return ``(country_code, country_name)`` for a country token, else None.

    Handles alpha-2, alpha-3, full names, and the small alias map. Never
    falls back to ``search_fuzzy`` — false matches on three-letter strings
    are too easy and the cost of a wrong country anchor is high.
    """
    if not token:
        return None
    upper = token.strip().upper()
    if upper in _COUNTRY_ALIASES:
        upper = _COUNTRY_ALIASES[upper]
    if len(upper) == 2:
        c = pycountry.countries.get(alpha_2=upper)
        if c is not None:
            return (c.alpha_2, c.name)  # pyright: ignore[reportAttributeAccessIssue]
    if len(upper) == 3:
        c = pycountry.countries.get(alpha_3=upper)
        if c is not None:
            return (c.alpha_2, c.name)  # pyright: ignore[reportAttributeAccessIssue]
    # Full-name lookup — case-insensitive via the .name attr.
    for country in cast("list[Any]", pycountry.countries):
        if country.name.lower() == token.strip().lower():
            return (country.alpha_2, country.name)
        official = getattr(country, "official_name", None)
        if official and official.lower() == token.strip().lower():
            return (country.alpha_2, country.name)
    return None


# ─── Region (subdivision) match ──────────────────────────────────────


def _lookup_region(token: str, country_code: str) -> tuple[str, str] | None:
    """Return ``(region_code_short, region_name)`` for a token within a country.

    ``region_code_short`` is the subdivision code WITHOUT the country prefix
    (``"CA"`` for ``"US-CA"``, ``"KA"`` for ``"IN-KA"``). Tries:

      1. Exact match on the short code (case-insensitive).
      2. Exact match on the subdivision name (case-insensitive).

    No fuzzy matching — subdivision names are stable and short, and
    fuzzy is what got us "UK → Uganda" elsewhere.
    """
    if not token or not country_code:
        return None
    subs = _subdivisions_by_country().get(country_code, [])
    upper = token.strip().upper()
    lower = token.strip().lower()
    for sub in subs:
        # sub.code looks like "US-CA"; strip the country prefix
        short_code = sub.code.split("-", 1)[1] if "-" in sub.code else sub.code
        if short_code.upper() == upper:
            return (short_code.upper(), sub.name)
    for sub in subs:
        if sub.name.lower() == lower:
            short_code = sub.code.split("-", 1)[1] if "-" in sub.code else sub.code
            return (short_code.upper(), sub.name)
    return None


# ─── City match ──────────────────────────────────────────────────────


def _lookup_city(
    token: str,
    country_code: str | None,
    region_code: str | None,
) -> dict[str, Any] | None:
    """Find a city in geonamescache scoped by country (and optionally region).

    Disambiguation rules (SPEC Q1, resolved 2026-05-27 — country-anchored
    fallback + population-weighted within that country):
      - If ``country_code`` is set, restrict to cities with that countrycode.
      - If ``country_code`` is None and 2+ global candidates exist, default
        the country anchor to ``US`` and restrict to US matches. When no US
        matches exist, fall through to global population-weighted (rare —
        ``Paris`` alone resolves to Paris, TX under this policy because
        the corpus is US-dominant; non-US callers must supply a country
        anchor in the location string).
      - If ``region_code`` is set AND ``country_code == "US"``, also restrict
        to cities with admin1code == region_code (US admin1codes are ISO
        3166-2, so this is reliable). For non-US countries, geonamescache
        admin1code is FIPS/numeric — skip the region filter rather than
        risk a wrong scope.
      - Always pick the highest-population candidate that survives scoping
        (matches what humans usually mean by ambiguous names within a
        country: largest Springfield in US is Springfield, MO).
      - When zero matches survive scoping, return None.

    Previous behavior (pre-Q1): returned None when 2+ candidates remained
    without a ``region_code`` scope, leaving ``city=None``. The user
    explicitly rejected that default at session-end 2026-05-27 because
    it discarded usable signal — see SPEC Q1 resolution.
    """
    if not token:
        return None
    candidates = _cities_by_name().get(token.strip().lower(), [])
    if not candidates:
        return None
    if country_code:
        candidates = [c for c in candidates if c.get("countrycode") == country_code]
    elif len(candidates) >= 2:
        # SPEC Q1: no country anchor + ambiguous → default to US.
        us_only = [c for c in candidates if c.get("countrycode") == "US"]
        if us_only:
            candidates = us_only
        # else: keep global candidates, fall through to population tiebreak.
    if not candidates:
        return None
    if region_code and country_code == "US":
        scoped = [c for c in candidates if c.get("admin1code") == region_code]
        if scoped:
            candidates = scoped
    candidates.sort(key=lambda c: c.get("population", 0) or 0, reverse=True)
    return candidates[0]


# ─── Workplace-type detection ────────────────────────────────────────


def _detect_workplace_from_body(jd_full: str | None) -> WorkplaceType:
    """Scan a JD body for LinkedIn workplace hashtags.

    Precedence matches ``_detect_workplace_type``: REMOTE > HYBRID > ONSITE.
    Returns UNSPECIFIED when no tag is found or input is empty. Only the
    ``#LI-Remote`` / ``#LI-Hybrid`` / ``#LI-Onsite`` forms are matched —
    bare workplace tokens in JD prose are too false-positive-prone.
    """
    if not jd_full:
        return "UNSPECIFIED"
    if _LI_REMOTE_BODY_RE.search(jd_full):
        return "REMOTE"
    if _LI_HYBRID_BODY_RE.search(jd_full):
        return "HYBRID"
    if _LI_ONSITE_BODY_RE.search(jd_full):
        return "ONSITE"
    return "UNSPECIFIED"


def _detect_workplace_type(text: str) -> tuple[WorkplaceType, str]:
    """Return ``(workplace_type, text_with_token_stripped)``.

    Detection precedence: REMOTE > HYBRID > ONSITE. If multiple tokens
    appear in the same string the most-prominent claim wins — a remote
    posting that mentions "Toronto on-site office available" is still
    REMOTE. UNSPECIFIED is returned when no token matches.
    """
    stripped = text
    detected: WorkplaceType = "UNSPECIFIED"
    if _REMOTE_TOKEN_RE.search(stripped):
        detected = "REMOTE"
        stripped = _REMOTE_TOKEN_RE.sub(" ", stripped)
    elif _HYBRID_TOKEN_RE.search(stripped):
        detected = "HYBRID"
        stripped = _HYBRID_TOKEN_RE.sub(" ", stripped)
    elif _ONSITE_TOKEN_RE.search(stripped):
        detected = "ONSITE"
        stripped = _ONSITE_TOKEN_RE.sub(" ", stripped)
    # Tidy up the separator debris the substitution leaves behind:
    # collapse whitespace, strip leading/trailing punctuation that the
    # removed token was sitting next to.
    stripped = re.sub(r"\s+", " ", stripped).strip()
    stripped = re.sub(r"^[\s\-–—,/;|]+|[\s\-–—,/;|]+$", "", stripped)
    return (detected, stripped)


def strip_workplace_tokens(text: str) -> str | None:
    """Strip an inline workplace-type token from a flat ``location`` string.

    Ingest-boundary normalizer for issue #264: scraped ``location`` values
    can carry an embedded workplace token ("Remote — Austin, TX") that
    duplicates the structured ``workplace_type`` column and, pre-fix, leaked
    straight through to the display string (``dedupe_location`` in
    ``web/feed_entries.py`` can only suppress the redundant badge, not clean
    the stored string itself). Reuses ``_detect_workplace_type``'s
    token-detect regexes — the same ones ``parse_locations`` uses — rather
    than re-deriving detection logic, so there is one definition of "what
    counts as a workplace token" for both the structured and flat paths.

    This helper only strips; it discards the detected ``workplace_type``
    classification. Populating ``workplace_type`` from a flat string is
    ``parse_locations``'s job when a source supplies ``locations_raw``.

    Returns:
      - The input unchanged when it is empty/falsy — a stripping normalizer
        should not invent a value for an already-empty location.
      - The cleaned string when content survives the strip
        ("Remote — Austin, TX" -> "Austin, TX").
      - ``None`` when the input was ONLY a workplace token ("Remote" ->
        None), matching ``db/_jobs.py``'s ``parsed.location or None`` intent
        rather than leaving a semantically-empty string in the column.

    Known limitation (inherited from ``_detect_workplace_type``): detection
    is if/elif (REMOTE > HYBRID > ONSITE precedence), so only the
    first-matched token TYPE is stripped — "Remote/Hybrid — Austin" only
    strips "Remote", leaving "Hybrid — Austin". Acceptable for #264:
    workplace tokens are rarely combined in one scraped location string.
    """
    if not text:
        return text
    _workplace, stripped = _detect_workplace_type(text)
    return stripped or None


# ─── Layer 3 — heuristics for tightly-scoped shapes ──────────────────


def _try_remote_dash_country(raw: str) -> JobLocation | None:
    """Match ``"Remote - <country>"`` and return a structured location.

    Returns None when the input doesn't match the shape OR when the
    trailing token isn't a recognized country.
    """
    m = _REMOTE_DASH_COUNTRY_RE.match(raw)
    if not m:
        return None
    country = _lookup_country(m.group(1))
    if country is None:
        return None
    code, name = country
    return JobLocation(
        city=None,
        region=None,
        region_code=None,
        country=name,
        country_code=code,
        workplace_type="REMOTE",
        raw=raw,
        unresolved=False,
    )


# ─── Core single-segment parser ──────────────────────────────────────


def _parse_one(raw: str) -> JobLocation | None:
    """Parse a single location string into one JobLocation, or None to drop.

    Returns None when the string normalizes to nothing (placeholder /
    whitespace only). Otherwise always returns a JobLocation — may be
    ``unresolved=True`` when structuring failed.
    """
    normalized = normalize_location(raw)
    if normalized is None:
        return None

    # Layer 3: "Remote - US" tight shape (LinkedIn workplace+country combo).
    layer3 = _try_remote_dash_country(normalized)
    if layer3 is not None:
        return layer3

    workplace, remainder = _detect_workplace_type(normalized)

    if not remainder:
        # Pure workplace token ("Remote", "Hybrid", ...) — return that
        # signal alone, no country/region/city.
        return JobLocation(
            city=None,
            region=None,
            region_code=None,
            country=None,
            country_code=None,
            workplace_type=workplace,
            raw=raw,
            unresolved=True,
        )

    # Tokenize on commas; trim each.
    segments = [s.strip() for s in remainder.split(",") if s.strip()]
    if not segments:
        return JobLocation.unresolved_from_raw(raw, workplace_type=workplace)

    # Anchor a country from the LAST segment when it looks like a country.
    country_code: str | None = None
    country_name: str | None = None
    if segments:
        last = segments[-1]
        # Special-case: 2-letter trailing tokens that are US state codes
        # default to "state, country=US inferred" UNLESS the token is
        # also an unambiguous country and the preceding segments don't
        # already nominate a US city. The common SPEC anchor
        # "San Francisco, CA" wants country_code=US, region_code=CA.
        last_upper = last.upper()
        if len(segments) >= 2 and last_upper in _US_STATE_CODES:
            # Treat as US state — anchor country to US, region to last.
            country_code = "US"
            country_name = "United States"
        else:
            anchored = _lookup_country(last)
            if anchored is not None:
                country_code, country_name = anchored
                segments = segments[:-1]  # consume the country segment
            elif len(segments) >= 2:
                # If country wasn't found but last looks like a 2-letter
                # state code, try the US fallback (unusual, but covers
                # "Springfield, IL" without explicit US).
                if last_upper in _US_STATE_CODES:
                    country_code = "US"
                    country_name = "United States"

    # If we anchored country via state-code path, the state IS the last
    # segment and we want to keep it for the region match. If we anchored
    # via country-name path, we already popped it.

    # Anchor a region from the (now possibly-trailing) state/region segment.
    # Only consume as region when ≥2 segments remain (the city slot is
    # safe). With a single remaining segment, treat it as city — many
    # GB / IN cities share names with their containing administrative
    # regions (Manchester city vs Manchester metropolitan county; Goa
    # city vs Goa state), and we want the more-specific city anchor.
    region_code: str | None = None
    region_name: str | None = None
    if country_code and len(segments) >= 2:
        candidate = segments[-1]
        region = _lookup_region(candidate, country_code)
        if region is not None:
            region_code, region_name = region
            segments = segments[:-1]  # consume the region segment

    # Match a city from what's left. SPEC says "first-segment-is-city is
    # the default ordering," so we take the first remaining segment.
    city_name: str | None = None
    if segments:
        candidate = segments[0]
        match = _lookup_city(candidate, country_code, region_code)
        if match is not None:
            city_name = match.get("name")
            # If we didn't get country/region from the input but the city
            # is unambiguous in the gazetteer, backfill from it.
            if not country_code:
                cc = match.get("countrycode")
                if cc:
                    c = pycountry.countries.get(alpha_2=cc)
                    if c is not None:
                        country_code = c.alpha_2  # pyright: ignore[reportAttributeAccessIssue]
                        country_name = c.name  # pyright: ignore[reportAttributeAccessIssue]
            if not region_code and country_code == "US":
                # US-only: admin1code is ISO 3166-2 in geonamescache.
                ac = match.get("admin1code")
                if ac:
                    region = _lookup_region(ac, "US")
                    if region is not None:
                        region_code, region_name = region

    # Decide unresolved: structurally unresolved iff we have neither
    # city NOR country (region alone with no country is too weak to
    # count as resolved).
    unresolved = city_name is None and country_code is None

    return JobLocation(
        city=city_name,
        region=region_name,
        region_code=region_code,
        country=country_name,
        country_code=country_code,
        workplace_type=workplace,
        raw=raw,
        unresolved=unresolved,
    )


# ─── Public entry point ──────────────────────────────────────────────


def parse_locations(
    raw: str | list[str] | None,
    *,
    jd_full: str | None = None,
) -> list[JobLocation]:
    """Parse a location string (or pre-split list) into ``list[JobLocation]``.

    Entry point for everything downstream of the ATS scanner Layer-1
    boundary. Greenhouse / Workday / Gmail parsers / SerpAPI /
    DataForSEO / Thordata / portal-search all reach here.

    Behavior:
      - None / "" / placeholder-only input → ``[]``.
      - Multi-location strings split via the existing normalizer's
        unambiguous-separator set (``|``, ``;``, ``/``, ``&``, ``or``).
      - List input is processed segment-by-segment; the existing
        normalizer is reused for splitting & placeholder pruning.
      - Output is deduped by ``(country_code, region_code, city,
        workplace_type)`` preserving first-seen order.

    ``jd_full`` (SPEC Q3): when provided, scan the JD body for
    ``#LI-Remote`` / ``#LI-Hybrid`` / ``#LI-Onsite`` hashtags as a
    last-resort workplace_type signal. Promotes UNSPECIFIED entries
    only — per-segment tokens in ``raw`` and trailing-slash promotion
    both take precedence. Empty/missing ``raw`` still returns ``[]``
    (the body tag is enrichment for known locations, not a location
    source on its own).

    Never raises. On failure, emits ``unresolved=True`` entries with the
    raw input preserved so callers can still display something useful.
    """
    if raw is None:
        return []

    # Pre-pass: detect trailing "/ <workplace>" and remember the workplace.
    # When present, strip it off the input before split + promote the workplace
    # onto every parsed entry that lacks an explicit token of its own. This
    # honors the SPEC contract that "San Francisco, CA / Remote" → one row.
    promoted_workplace: WorkplaceType | None = None
    pre_strings: list[str] = [raw] if isinstance(raw, str) else list(raw)
    stripped_strings: list[str] = []
    for entry in pre_strings:
        if not isinstance(entry, str):
            continue
        match = _TRAILING_SLASH_WORKPLACE_RE.match(entry)
        if match:
            base = match.group(1).strip()
            token = match.group(2).lower()
            if token.replace("-", "").replace(" ", "") in {"remote", "fullyremote"}:
                promoted_workplace = "REMOTE"
            elif token == "hybrid":  # noqa: S105 — workplace type token, not a credential
                promoted_workplace = "HYBRID"
            else:
                promoted_workplace = "ONSITE"
            stripped_strings.append(base if base else "")
        else:
            stripped_strings.append(entry)

    segments: list[str] = []
    for entry in stripped_strings:
        if not entry:
            continue
        segments.extend(split_multi_locations(entry))
    if not segments and promoted_workplace is None:
        return []
    if not segments and promoted_workplace is not None:
        # Edge case: the entire input was just "/ Remote" — emit a bare
        # workplace entry rather than dropping the signal entirely.
        return [
            JobLocation(
                city=None,
                region=None,
                region_code=None,
                country=None,
                country_code=None,
                workplace_type=promoted_workplace,
                raw=pre_strings[0] if pre_strings else "",
                unresolved=True,
            )
        ]

    out: list[JobLocation] = []
    for segment in segments:
        parsed = _parse_one(segment)
        if parsed is None:
            continue
        # Apply trailing-slash workplace promotion: only fires when the
        # entry's own detection came back UNSPECIFIED (an explicit
        # per-segment token wins over the promoted one).
        if promoted_workplace is not None and parsed.workplace_type == "UNSPECIFIED":
            parsed = JobLocation(
                city=parsed.city,
                region=parsed.region,
                region_code=parsed.region_code,
                country=parsed.country,
                country_code=parsed.country_code,
                workplace_type=promoted_workplace,
                raw=parsed.raw,
                unresolved=parsed.unresolved,
            )
        logger.debug("parse_locations: %r → %r", segment, parsed)
        out.append(parsed)

    # SPEC Q3: JD-body LinkedIn hashtag fallback. Applies ONLY to entries
    # whose workplace_type is still UNSPECIFIED after raw-token detection
    # and trailing-slash promotion — both of those signals come from the
    # location string itself and outrank the body-tag fallback.
    if out and jd_full:
        body_workplace = _detect_workplace_from_body(jd_full)
        if body_workplace != "UNSPECIFIED":
            promoted: list[JobLocation] = []
            for loc in out:
                if loc.workplace_type == "UNSPECIFIED":
                    promoted.append(
                        JobLocation(
                            city=loc.city,
                            region=loc.region,
                            region_code=loc.region_code,
                            country=loc.country,
                            country_code=loc.country_code,
                            workplace_type=body_workplace,
                            raw=loc.raw,
                            unresolved=loc.unresolved,
                        )
                    )
                else:
                    promoted.append(loc)
            out = promoted

    return dedupe_locations(out)

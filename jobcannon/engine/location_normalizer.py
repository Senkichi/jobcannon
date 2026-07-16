"""Location normalization for job listings.

Pure functions. No I/O, no shared state. Used at two boundaries:

  - **Ingestion (write):** ``upsert_job`` calls ``normalize_location`` on
    each entry before storing in ``jobs.locations_raw`` so subsequent
    ingestions of the same job don't accumulate whitespace / casing /
    placeholder variants as distinct entries.

  - **Filter dropdown (read):** ``get_distinct_locations`` iterates each
    job's ``locations_raw`` JSON array (rather than the merged
    ``location`` string), normalizes every entry, and lower-case-dedupes
    so the dropdown shows a manageable set of canonical location values.

Conservative on purpose: trim + collapse whitespace, strip surrounding
punctuation, drop placeholder-only values ("Unknown", "TBD", ...). NO
aggressive case rewriting and NO multi-location splitting on plain
commas — both have ambiguous failure modes (mangling "CA" → "Ca",
splitting "Suite 100, San Francisco, CA" into garbage). Future work
can tighten this if dropdown pollution remains.

Parsers that produced multi-location strings via UNAMBIGUOUS separators
(`|`, ` / `, `;`, ` & `) ARE split here — those separators don't appear
inside real city/state pairs and produce clean per-location entries.
"""

from __future__ import annotations

import re

# Placeholder values that mean "no real location" — drop entirely.
# Match must be the entire normalized value (case-insensitive). Conservative:
# only includes unambiguous junk. "Anywhere", "Worldwide", "Global",
# "US" etc. are NOT placeholders — they're meaningful filter values.
_PLACEHOLDER_VALUES = frozenset(
    {
        "n/a",
        "na",
        "tbd",
        "tba",
        "unknown",
        "various",
        "varies",
        "multiple locations",
        "see job description",
        "see jd",
        "see description",
        "not specified",
        "none",
        "-",
        "--",
    }
)

# Collapse runs of whitespace to a single space.
_WS_RE = re.compile(r"\s+")

# Strip surrounding whitespace + punctuation. Includes hyphens and dashes
# because some parsers emit bare "- " or "—" as a location placeholder.
_TRAILING_PUNCT_RE = re.compile(r"^[\s\-–—.,;:|]+|[\s\-–—.,;:|]+$")

# Multi-location separators that NEVER appear inside a real "City, State"
# fragment. Splitting on these is safe; splitting on plain "," is not
# (would break city/state pairs).
_MULTI_LOC_SEP_RE = re.compile(r"\s*[|;]\s*|\s+/\s+|\s+&\s+|\s+or\s+", re.IGNORECASE)


def normalize_location(raw: str | None) -> str | None:
    """Canonicalize a single location string.

    Args:
        raw: The location text as captured by a parser.

    Returns:
        The normalized location, or ``None`` when the input is empty or a
        recognized placeholder ("Unknown", "N/A", "TBD", ...). The
        returned value is whitespace-trimmed, whitespace-collapsed, and
        stripped of leading / trailing punctuation. Case is preserved
        (no aggressive title-casing — see module docstring).
    """
    if not raw:
        return None
    cleaned = _TRAILING_PUNCT_RE.sub("", raw)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return None
    if cleaned.lower() in _PLACEHOLDER_VALUES:
        return None
    return cleaned


####################################################################
# Display-side normalization (filter dropdown polish, read-only)    #
####################################################################
#
# Display normalization is MORE aggressive than write-side
# ``normalize_location``: it strips annotations / trailing countries /
# ZIPs, converts ALLCAPS to title-case, and folds full US state names
# to their 2-letter postal codes. The purpose is to collapse "San Jose,
# CA", "San Jose, California", "San Jose, CA, United States", "SAN
# JOSE, CALIFORNIA", "San Jose, CA (+1 other)", "San Jose, CA 95131"
# into a single dropdown entry. Source data is NOT mutated — this is
# strictly for what the user sees in the filter list.

# "(+N other)" / "(+N others)" annotations emitted by some parsers.
_ANNOTATION_RE = re.compile(r"\s*\(\+\d+\s+(?:other|others)\)\s*", re.IGNORECASE)

# 5-digit US ZIP, optionally followed by -4.
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")

# Terminal country tokens to drop ("San Jose, CA, United States" -> "San
# Jose, CA"). Matched after the final comma; the leading comma + whole
# segment is consumed.
_TRAILING_COUNTRY_RE = re.compile(
    r",\s*(?:united states|u\.s\.a\.|usa|u\.s\.|us)\s*$",
    re.IGNORECASE,
)

# Full US state name -> 2-letter postal code. Used so "San Jose,
# California" collapses with "San Jose, CA". DC included; US territories
# excluded (Puerto Rico / Guam appear rarely; can be added if needed).
_US_STATE_NAME_TO_CODE = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
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
}


def _is_allcaps(s: str) -> bool:
    """True iff s contains at least one letter and every letter is upper case."""
    has_letter = False
    for c in s:
        if c.isalpha():
            has_letter = True
            if not c.isupper():
                return False
    return has_letter


def _unshout(s: str) -> str:
    """Title-case ALLCAPS *multi-segment* strings; leave others unchanged.

    Two guards:
      - Mixed-case strings are returned as-is (avoids the common bug
        where ``.title()`` mangles existing state codes — "San Francisco,
        CA" must not become "San Francisco, Ca").
      - Single-token ALLCAPS strings are also untouched (preserves
        well-known abbreviations like "NYC", "SF", "LA", "USA", "UK"
        — these should appear in the dropdown exactly as the parser
        captured them).

    Only fires when the string contains a comma, which is the signal of
    a multi-segment "City, State" pattern. That's the only ALLCAPS
    shape the dataset surfaces (e.g. "SAN JOSE, CALIFORNIA" from a
    couple of parsers).
    """
    if "," not in s:
        return s
    if _is_allcaps(s):
        return s.title()
    return s


def _swap_state_names(s: str) -> str:
    """Replace full US state names with their 2-letter codes.

    Operates on comma-delimited segments because state names sit between
    commas in "City, State, Country" patterns. Multi-word state names
    ("New York") are handled because the segment is the entire content
    between commas, not the result of a word-split.
    """
    parts = s.split(",")
    out: list[str] = []
    for part in parts:
        stripped = part.strip()
        lower = stripped.lower()
        if lower in _US_STATE_NAME_TO_CODE:
            out.append(_US_STATE_NAME_TO_CODE[lower])
        else:
            out.append(stripped)
    return ", ".join(out)


def normalize_for_display(raw: str | None) -> str | None:
    """Display-side canonicalization for the filter dropdown.

    Layered on TOP of ``normalize_location`` (this fn does not handle
    placeholder dropping or whitespace collapse — assume those happened
    already). Returns ``None`` when the result is empty after stripping.

    Transformations:
      - Strip "(+N other)" / "(+N others)" annotation.
      - Strip ZIP codes (5 digits + optional -4).
      - Strip a trailing US country segment (",US", ",USA", ",United States", "us").
      - Title-case ALLCAPS strings ("SAN JOSE, CALIFORNIA" -> "San Jose, California").
      - Map full US state names to their 2-letter postal codes
        ("San Jose, California" -> "San Jose, CA").

    All transformations are conservative and avoid mangling mixed-case
    "City, State" pairs. The function never splits on plain commas
    except as the segment boundary for state-name swapping.
    """
    if not raw:
        return None
    s = _ANNOTATION_RE.sub(" ", raw)
    s = _ZIP_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    s = _TRAILING_COUNTRY_RE.sub("", s).strip()
    s = _unshout(s)
    s = _swap_state_names(s)
    # Final tidy: re-collapse whitespace, strip trailing punctuation that
    # may have been exposed by segment removal (", " left behind by ZIP
    # strip mid-string, for example).
    s = _WS_RE.sub(" ", s).strip()
    s = _TRAILING_PUNCT_RE.sub("", s)
    return s or None


def split_multi_locations(raw: str | None) -> list[str]:
    """Split a parser-captured location on unambiguous multi-location
    separators (`|`, `;`, ` / `, ` & `, ` or `), then normalize each part.

    Returns the de-duplicated list of normalized entries (preserving
    first-seen order). Returns an empty list when the input is empty or
    a pure placeholder.

    Plain commas are NOT a separator — they're part of "City, State"
    fragments. Comma-separated lists of cities will appear as a single
    entry; that's an acceptable trade-off (rare in practice; ambiguous
    to split safely).
    """
    if not raw:
        return []
    parts = _MULTI_LOC_SEP_RE.split(raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = normalize_location(part)
        if normalized is None:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out

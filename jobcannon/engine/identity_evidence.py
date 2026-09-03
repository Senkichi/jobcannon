# PORTED from job_finder/web/identity_evidence.py @ b3a892fcba95531152bbc660293225795aae8020 (private job-cannon). Ledger L-0049.
"""Identity-evidence helpers shared across homepage discovery and the careers crawler.

These helpers are deliberately dependency-light (only ``re`` and the standard
library) so they can be imported by modules that must not pull in heavy
web/database dependencies at import time.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Bounded slice of a homepage's HTML searched for identity evidence (<title>,
# og:site_name, copyright footer). Kept small and deterministic — no full-DOM
# parsing dependency beyond what the repo already uses elsewhere.
_IDENTITY_EVIDENCE_SLICE = 10_000

_COMPANY_SUFFIXES = frozenset(
    [
        "inc",
        "llc",
        "corp",
        "co",
        "ltd",
        "group",
        "inc.",
        "llc.",
        "corp.",
        "co.",
        "ltd.",
    ]
)

# Common <title> separator glyphs. A real page title is usually
# "<Brand> <sep> <Page>" or "<Page> <sep> <Brand>", so the brand token sits
# at a LEADING or TRAILING separator-delimited edge — never buried in an
# interior segment. We anchor identity on edge segments only; interior
# segments (and the whole-title text) are treated as loose evidence usable by
# multi-token names alone.
_TITLE_SEPARATORS = ("|", "–", "—", "-", "::", "·", "»", "•", ":")


# ---------------------------------------------------------------------------
# Name normalization helpers
# ---------------------------------------------------------------------------


def _strip_company_suffixes(name: str) -> str:
    """Lowercase name, strip trailing suffix tokens (Inc, LLC, Corp, etc.)."""
    tokens = name.lower().split()
    while tokens and tokens[-1].rstrip(".") in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _name_to_slug(name: str) -> str:
    """Convert a company/display name to a hyphenated slug.

    Lowercases, strips suffixes, and collapses all non-alphanumeric runs to
    hyphens. Used for identity comparisons across homepage discovery and
    employer-identity verification in the careers crawler.
    """
    stripped = _strip_company_suffixes(name)
    slug = re.sub(r"[^a-z0-9]+", "-", stripped).strip("-")
    return slug


# ---------------------------------------------------------------------------
# Token-sequence matching
# ---------------------------------------------------------------------------


def _slug_has_token_sequence(haystack_slug: str, needle_slug: str) -> bool:
    """True if ``needle_slug``'s hyphen-separated tokens appear as a
    CONTIGUOUS token subsequence within ``haystack_slug``'s tokens.

    Token-boundary match (not a raw substring): "acme" must not match inside
    "pinnacme" or "acmecorp-inc" as a bare ``in`` check would allow, but DOES
    match "acme" within "widget-makers-home" style haystacks when the needle's
    own tokens ("widget", "makers") appear back-to-back. Both sides are
    expected to already be lowercased/hyphenated via `_name_to_slug`.
    """
    if not needle_slug or not haystack_slug:
        return False
    hay_tokens = haystack_slug.split("-")
    needle_tokens = needle_slug.split("-")
    n = len(needle_tokens)
    if n == 0 or n > len(hay_tokens):
        return False
    return any(hay_tokens[i : i + n] == needle_tokens for i in range(len(hay_tokens) - n + 1))


# ---------------------------------------------------------------------------
# Title edge segments
# ---------------------------------------------------------------------------


def _title_edge_segments(title: str) -> list[str]:
    """Return the leading and trailing separator-delimited segments of a
    <title>, never interior ones.

    "Widget Makers | Home" -> ["Widget Makers", "Home"]; "Home | About |
    Widget Makers" -> ["Home", "Widget Makers"] (the interior "About" is
    dropped). A title with no recognized separator is a single edge segment
    (itself). Anchoring on edges keeps a single-token brand from matching an
    unrelated interior word.
    """
    text = title.strip()
    if not text:
        return []
    # Split on the widest separator set, keeping only non-empty segments.
    pattern = "|".join(re.escape(sep) for sep in _TITLE_SEPARATORS)
    parts = [p.strip() for p in re.split(pattern, text) if p.strip()]
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0]]
    return [parts[0], parts[-1]]


# ---------------------------------------------------------------------------
# Identity evidence extraction from bounded HTML
# ---------------------------------------------------------------------------


def _extract_identity_evidence(html: str) -> tuple[set[str], set[str]]:
    """Extract identity-evidence slugs from <title>, og:site_name, and
    copyright-footer lines within a bounded HEAD slice of ``html``.

    Returns ``(anchored_slugs, loose_slugs)``:

    * ``anchored_slugs`` — high-confidence exact-brand fields where the whole
      field is expected to BE the brand: the og:site_name value and the
      leading/trailing <title> edge segments (via `_title_edge_segments`). A
      single-token company name may be accepted on an anchored match, because
      an anchored field equalling "makers" is strong evidence, whereas
      "makers" appearing inside a longer phrase is not.
    * ``loose_slugs`` — the full <title> text and each copyright-footer line.
      These may contain the brand alongside other words ("Coffee Makers
      United"), so they are only usable by MULTI-token names via
      `_slug_has_token_sequence`, never by a single-token name.

    Deliberately cheap and deterministic: regex extraction over the first
    `_IDENTITY_EVIDENCE_SLICE` characters only, no DOM parsing.
    """
    slice_ = html[:_IDENTITY_EVIDENCE_SLICE]
    anchored_raw: list[str] = []
    loose_raw: list[str] = []

    title_match = re.search(r"<title[^>]*>(.*?)</title>", slice_, re.IGNORECASE | re.DOTALL)
    if title_match:
        title_text = title_match.group(1)
        loose_raw.append(title_text)
        anchored_raw.extend(_title_edge_segments(title_text))

    og_match = re.search(
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']*)["\']',
        slice_,
        re.IGNORECASE,
    )
    if og_match:
        anchored_raw.append(og_match.group(1))
    else:
        # content= may precede property= in attribute order.
        og_match_alt = re.search(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:site_name["\']',
            slice_,
            re.IGNORECASE,
        )
        if og_match_alt:
            anchored_raw.append(og_match_alt.group(1))

    for footer_match in re.finditer(
        r"(?:©|&copy;)\s*(?:\d{4}\s*)?([A-Za-z0-9][A-Za-z0-9 ,.&'-]{1,60})",
        slice_,
        re.IGNORECASE,
    ):
        loose_raw.append(footer_match.group(1))
    for footer_match in re.finditer(
        r"Copyright\s*(?:©\s*)?(?:\d{4}\s*)?([A-Za-z0-9][A-Za-z0-9 ,.&'-]{1,60})",
        slice_,
        re.IGNORECASE,
    ):
        loose_raw.append(footer_match.group(1))

    def _slugify_all(raws: list[str]) -> set[str]:
        out: set[str] = set()
        for raw in raws:
            slug = _name_to_slug(raw)
            if slug:
                out.add(slug)
        return out

    return _slugify_all(anchored_raw), _slugify_all(loose_raw)


# ---------------------------------------------------------------------------
# Hiring-organization name extraction from schema.org JSON-LD
# ---------------------------------------------------------------------------


def _hiring_org_name(posting: dict) -> str:
    """Extract a hiring-organization name from a schema.org JobPosting dict.

    Handles both shapes: a plain string, or an ``Organization`` object with a
    ``name`` field. Returns "" when absent or unparseable — callers must never
    treat an empty result as evidence of anything (absence of data is not
    evidence).
    """
    org = posting.get("hiringOrganization")
    if isinstance(org, str):
        return org.strip()
    if isinstance(org, dict):
        name = org.get("name")
        if isinstance(name, str):
            return name.strip()
    return ""


def _identity_evidence_accepts(
    name: str,
    anchored_slugs: set[str],
    loose_slugs: set[str],
) -> bool:
    """True when ``name`` is consistent with the extracted identity evidence.

    Mirrors the acceptance logic in ``homepage_discoverer._validate_guessed_homepage``:

    * single- and multi-token names accept an exact anchored slug match;
    * multi-token names additionally accept a contiguous token-subsequence match
      against any loose slug.
    """
    name_slug = _name_to_slug(name)
    if not name_slug:
        return False
    if name_slug in anchored_slugs:
        return True
    return "-" in name_slug and any(_slug_has_token_sequence(ev, name_slug) for ev in loose_slugs)

"""Foundation-layer normalization utilities for job dedup keys.

Contains pure normalization functions (no host-layer dependencies) that can be
imported by both jobcannon.engine.models and jobcannon.engine.dedup_normalizer without
creating an upward dependency out of the engine purity boundary (enforced by
tests/engine/test_boundary.py).
"""

import html
import re

# ---------------------------------------------------------------------------
# Normalizer version (D-8: derived values are versioned)
# ---------------------------------------------------------------------------
#
# dedup_key is a pure function of (company, title) routed through
# normalize_company / normalize_title. Per D-8, any stored value that is a pure
# function of other stored data records the version of the function that derived
# it, and a standing, idempotent re-derivation runs when that version changes.
#
# NORMALIZER_VERSION is that version tag. Version 1 is the IMPLICIT original
# normalizer (no digit<->letter separator rule). Version 2 is the current
# algorithm (added the digit<->letter boundary split at line ~249).
#
# BUMP THIS whenever normalize_company / normalize_title semantics change so
# that the same (company, title) could map to a different dedup_key. In the
# private source repo, bumping it re-arms a standing startup hook
# (`_run_rekey_if_stale` in job_finder/web/migrations/_post_hooks.py) that
# re-derives every stored row's key under the new version. jobcannon ships no
# such hook — it is a pure library with no persistence of its own — so an
# embedding host that persists dedup keys is responsible for re-deriving them
# on a version bump. The canary test in the private repo's
# tests/test_dedup_normalizer.py fails loudly ("normalizer semantics changed --
# bump NORMALIZER_VERSION") if the functions drift without a bump — this is the
# enforcement that a once-ever-sentinel gap can never recur.
#
# strip_site_code_prefix() below was added but does NOT wire into
# normalize_company() / bump this version — the one-off migration script and
# its own precision tests exercise the function directly. Wiring a
# site-code-prefix strip into the live normalization path (and re-arming the
# standing re-key across the whole registry) is deferred until the ingestion
# boundary that actually emits these names is proven (see the function
# docstring for what was and wasn't established).
NORMALIZER_VERSION: int = 2


# ---------------------------------------------------------------------------
# Company name deterministic cleanup regexes
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Leading numeric prefix junk: "1. ", "123) ", "42 - " at start of string.
# Only stripped when the remainder after the match is non-empty.
_LEADING_NUMERIC_JUNK_RE = re.compile(r"^\d+[\.\-\)\s]+")


# ---------------------------------------------------------------------------
# Company suffix stripping
# Strip common legal entity suffixes, with or without preceding comma/period.
# Pattern: optional whitespace + optional comma + whitespace + suffix + optional period
# ---------------------------------------------------------------------------

_COMPANY_SUFFIXES = re.compile(
    r"""
    [,\s]+                          # optional comma then whitespace before suffix
    (?:
        inc\.?
        | incorporated\.?
        | llc\.?
        | corp\.?
        | corporation\.?
        | ltd\.?
        | limited\.?
        | co\.?
        | company\.?
        | technologies\.?
        | technology\.?
        | tech\.?
        | group\.?
        | holdings?\.?
        | services?\.?
        | solutions?\.?
    )
    \s*$                            # must be at end of string
    """,
    re.IGNORECASE | re.VERBOSE,
)

# ---------------------------------------------------------------------------
# Title abbreviation expansion
# Each tuple is (compiled_pattern, replacement_string).
# Order matters: sr. before sr (to handle period variant first).
# ---------------------------------------------------------------------------

_TITLE_ABBREVS = [
    # Seniority — match the abbreviation (with optional trailing period) surrounded
    # by word boundaries or end of string. Using (?:...) to capture the optional period
    # as part of the match so it does not remain in the output.
    (re.compile(r"\bsr\.(?=\s|$)", re.IGNORECASE), "senior"),
    (re.compile(r"\bjr\.(?=\s|$)", re.IGNORECASE), "junior"),
    (re.compile(r"\bmgr\.(?=\s|$)", re.IGNORECASE), "manager"),
    (re.compile(r"\beng\.(?=\s|$)", re.IGNORECASE), "engineering"),
    (re.compile(r"\bdir\.(?=\s|$)", re.IGNORECASE), "director"),
    (re.compile(r"\bvp\.(?=\s|$)", re.IGNORECASE), "vice president"),
    (re.compile(r"\bswe\.(?=\s|$)", re.IGNORECASE), "software engineer"),
    (re.compile(r"\bpm\.(?=\s|$)", re.IGNORECASE), "product manager"),
    # Also match without period (word boundary)
    (re.compile(r"\bsr\b(?!\.)", re.IGNORECASE), "senior"),
    (re.compile(r"\bjr\b(?!\.)", re.IGNORECASE), "junior"),
    (re.compile(r"\bmgr\b(?!\.)", re.IGNORECASE), "manager"),
]

# ---------------------------------------------------------------------------
# Legal-entity code prefix stripping
#
# Workday and aggregator feeds (DataForSEO crawling Workday tenants) often
# return the legal entity name with a leading internal cost-center / business-
# unit code: e.g. "HC1316 GE Precision Healthcare LLC", "1144 IHS GLOBAL INC",
# "200 Protiviti Inc.", "USA016 Refinitiv US LLC". The prefix is meaningless
# to the user and pollutes display, dedup keys, and history-cohort matching.
#
# The regex is intentionally narrow: it only fires when (a) the leading token
# matches a "code-shaped" pattern, AND (b) the remainder contains a recognized
# legal-entity suffix word (Inc/LLC/Corp/Co/etc.). The combined gate is what
# keeps it safe against legitimate brand names like "A10 Networks, Inc"
# (single leading alpha), "Point2 Technology Inc." (digits after alpha), or
# "21 Tech" (no legal-entity suffix). Without the alpha-prefix branch this
# overlaps with _LEADING_NUMERIC_JUNK_RE; the branches together cover both
# pure-digit ("1144 ") and alpha-digit ("HC1316 ", "USA016 ") legal codes.
# ---------------------------------------------------------------------------

_LEGAL_ENTITY_PREFIX_RE = re.compile(
    r"""
    ^
    (?:
        \d{2,6}             # pure-digit code (091, 1144, 7505, 00100, 09516)
        |
        [A-Z]{2,3}\d{2,5}   # alpha-prefix + digit-suffix (HC1316, USA016, LE10, KPG99)
    )
    \s+
    (?=[A-Za-z])            # followed by a letter (not another digit, not punctuation)
    """,
    re.VERBOSE,
)

# ---------------------------------------------------------------------------
# Site-code prefix detection
#
# ATS/career-page sources sometimes include internal site/branch/facility codes
# as a leading prefix: e.g. "0006 MA01-CAMBRIDGE-CROSSING-US4E", "0101 The
# Huntington National Bank", "C4000 Stewart Title Company", "09516 Banco
# Nacional de Mexico". These are different from legal-entity codes above —
# they're location/facility identifiers that pollute the company name field.
#
# The pattern is derived from the shape of the confirmed cases, not from an
# allowlist of "safe" brand names (an allowlist is toothpick-brittle — it only
# ever covers names already seen and breaks on the next one). The two leading
# shapes below are what real site codes look like, and legitimate numbered
# brands structurally cannot produce them:
#   (a) a leading-zero numeric token ("0006", "0101", "09516") — no real brand
#       name is spelled with a leading zero.
#   (b) a single letter followed by 3+ digits ("C4000") — brands like
#       "A10 Networks" pair a letter with only 1-2 digits, which this branch
#       deliberately excludes.
#
# Names with a leading digit token that has NEITHER signal (e.g. "3010 HYDRIL
# USA DISTRIBUTION", "410 ICR United States USA" — no leading zero, and not
# letter-prefixed) are intentionally NOT matched here. Those are exactly the
# borderline cases that need owner review rather than automatic
# stripping; a broader regex could catch them too, but only at the cost of
# also catching "3M Company", "84 Lumber", "1st Financial Bank USA", "99 Cents
# Only Stores", "2020 Companies", "1872 Consulting", and "21 Tech" — the
# precision test matrix in tests/test_strip_site_code_prefixes.py pins this
# tradeoff; loosening the regex must fail that test deliberately, not by
# accident.
# ---------------------------------------------------------------------------

_SITE_CODE_PREFIX_RE = re.compile(r"^(?:0\d+|[A-Za-z]\d{3,})\s+(.+)")

_LEGAL_ENTITY_SUFFIX_RE = re.compile(
    r"\b(?:Inc|Incorporated|LLC|L\.?\s?L\.?\s?C|Corp|Corporation|Ltd|Limited|Co|Company|S\.A\.|GmbH)\b\.?",
    re.IGNORECASE,
)


def strip_legal_entity_prefix(company: str) -> str:
    """Strip a leading legal-entity code prefix from a company name.

    Only fires when both (a) the leading token matches a code-shaped pattern
    AND (b) the remainder contains a legal-entity suffix word. The second
    gate protects legitimate brands like "A10 Networks, Inc" (only one
    leading alpha char), "Point2 Technology Inc." (digits after alpha not
    before), and "21 Tech" (no legal-entity suffix in the name).

    Guards against degenerate cases where the prefix turns out to BE the
    brand name itself (e.g. "KPG99 INC" → stripping would leave only "INC"):
    when the cleaned residue contains nothing beyond the entity suffix, the
    original name is returned unchanged.

    Args:
        company: Raw company name (any casing).

    Returns:
        Cleaned company name with prefix removed, or the original name
        unchanged if no prefix was detected or the strip would leave only
        a bare legal-entity suffix.
    """
    if not company:
        return company
    s = company.strip()
    if not (_LEGAL_ENTITY_PREFIX_RE.match(s) and _LEGAL_ENTITY_SUFFIX_RE.search(s)):
        return s
    cleaned = _LEGAL_ENTITY_PREFIX_RE.sub("", s, count=1).strip()
    if not cleaned:
        return s
    residue = _LEGAL_ENTITY_SUFFIX_RE.sub("", cleaned).strip(" ,.-")
    if not residue:
        return s
    return cleaned


def strip_site_code_prefix(company: str | None) -> str | None:
    """Strip a leading site-code prefix from a company name.

    ATS/career-page sources sometimes include internal site/branch/facility codes
    as a leading prefix: e.g. "0006 MA01-CAMBRIDGE-CROSSING-US4E", "0101 The
    Huntington National Bank", "C4000 Stewart Title Company". These are
    location/facility identifiers that pollute the company name field.

    Matches only two shapes (see _SITE_CODE_PREFIX_RE above for why): a
    leading-zero numeric token ("0006", "0101", "09516") or a single letter
    followed by 3+ digits ("C4000"). This is precise by construction — no
    allowlist of legitimate brand names is needed or maintained here.

    NOT wired into normalize_company(); this is a standalone helper used by
    scripts/strip_site_code_prefixes.py (the scoped one-off migration) and
    exercised directly by its precision test matrix.

    Args:
        company: Raw company name (any casing).

    Returns:
        Cleaned company name with the site-code prefix removed, or the
        original name unchanged if no prefix was detected.
    """
    if not company:
        return company
    s = company.strip()

    match = _SITE_CODE_PREFIX_RE.match(s)
    if match:
        return match.group(1).strip()

    return s


# ---------------------------------------------------------------------------
# Title level suffix stripping
# Strip "(IC5)", "L5", "Level 3", "- Level III" etc. at end of title.
# ---------------------------------------------------------------------------

_TITLE_STRIP_SUFFIX = re.compile(
    r"""
    \s*
    (?:
        \(IC\d+\)                   # (IC5), (IC6)
        | \bIC\d+\b                 # IC5, IC6 without parens
        | \bL\d+\b                  # L5, L6, L7
        | \bLevel\s+\d+\b           # Level 3, Level 4
        | \bLvl\.?\s*\d+\b         # Lvl 3, Lvl. 4
        | [-–]\s*Level\s+\d+        # - Level 3
        | [-–]\s*L\d+               # - L5
        | \bI{1,3}V?\b             # Roman numerals I, II, III, IV at word boundary
        | \bVII?\b                  # VI, VII
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_company(company: str) -> str:
    """Normalize a company name for dedup key generation.

    Applies deterministic cleanup in order: HTML entity decode, HTML tag
    strip, whitespace collapse, leading numeric prefix strip, lowercase,
    then legal suffix stripping. All steps preserve the dedup invariant:
    same real company always maps to the same canonical name.

    Args:
        company: Raw company name string.

    Returns:
        Lowercased, suffix-stripped company name. Always lowercase — do not
        use as a display value; the raw input or name_raw column serves that
        purpose.
    """
    # 1. Decode HTML entities (e.g. "&amp;" -> "&", "&#34;" -> '"')
    normalized = html.unescape(company)
    # 2. Strip HTML tags (e.g. "<b>Acme</b>" -> "Acme")
    normalized = _HTML_TAG_RE.sub("", normalized)
    # 3. Collapse repeated whitespace
    normalized = " ".join(normalized.split())
    # 4. Strip Workday-style legal-entity code prefix (e.g. "HC1316 ",
    #    "USA016 ") — the alpha-digit branch _LEADING_NUMERIC_JUNK_RE
    #    doesn't reach. The pure-digit case is handled both here and by
    #    _LEADING_NUMERIC_JUNK_RE; either ordering is correct.
    normalized = strip_legal_entity_prefix(normalized)
    # 5. Strip leading numeric prefix junk only when remainder is non-empty
    #    e.g. "1. Acme Corp" -> "Acme Corp", but "100" stays "100"
    m = _LEADING_NUMERIC_JUNK_RE.match(normalized)
    if m and normalized[m.end() :].strip():
        normalized = normalized[m.end() :]
    # 6. Strip and lowercase (original behavior)
    normalized = normalized.strip().lower()
    # 7. Strip legal suffixes repeatedly (e.g. "Acme Corp. Inc." -> "acme")
    prev = None
    while normalized != prev:
        prev = normalized
        normalized = _COMPANY_SUFFIXES.sub("", normalized).strip()
    return normalized


def normalize_title(title: str) -> str:
    """Normalize a job title for dedup key generation.

    Expands common abbreviations (Sr. -> Senior) and strips level suffixes
    (IC5, Level 3) to reduce formatting noise.

    Args:
        title: Raw job title string.

    Returns:
        Lowercased, normalized title.
    """
    normalized = title.strip()

    # Strip level suffixes first (e.g., "Staff Engineer (IC5)" -> "Staff Engineer")
    normalized = _TITLE_STRIP_SUFFIX.sub("", normalized).strip()

    # Expand abbreviations
    for pattern, replacement in _TITLE_ABBREVS:
        normalized = pattern.sub(replacement, normalized)

    # Insert a separator at digit<->letter transitions so scraper artifacts like
    # "84Data" and "84 Data" canonicalize identically. Mirrors the whitespace
    # collapse below — both exist to neutralize separator noise in the dedup key.
    normalized = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", normalized)

    # Normalize whitespace and lowercase
    normalized = " ".join(normalized.split()).lower()
    return normalized


def derive_dedup_key(company: str, title: str) -> str:
    """Derive the current-version dedup_key for a job (D-8).

    The dedup_key is ``"{normalize_company(company)}|{normalize_title(title)}"``.
    Location is intentionally excluded (same company + same title = same job).

    This is the single derivation entry point keyed to ``NORMALIZER_VERSION``.
    ``Job.normalized_dedup_key`` and ``dedup_normalizer.normalized_dedup_key``
    delegate to the same two normalize functions, so all derivation paths agree
    byte-for-byte (enforced by the foundation/web parity test).

    Args:
        company: Raw company name.
        title: Raw job title.

    Returns:
        ``"{normalized_company}|{normalized_title}"`` under the current
        normalizer version.
    """
    return f"{normalize_company(company)}|{normalize_title(title)}"


# ---------------------------------------------------------------------------
# Shared cross-field token helpers
#
# Used by BOTH the title contract (does the JD mention its own title? —
# title_jd_mismatch) and the jd-content contract (is this body the posting for
# THIS job?). Extracted to the foundation layer so the two contracts share ONE
# stopword set + tokenizer instead of each carrying a private copy (the exact
# copy-paste the field-contract work is trying to eliminate). Pure functions,
# no host-layer dependency — safe to import from anywhere inside the engine
# purity boundary.
# ---------------------------------------------------------------------------

#: Generic title words that carry no matching signal (seniority / level / format).
#: Kept identical to the historical _title_contract set; do not prune without
#: re-checking title_jd_mismatch behaviour.
TITLE_STOPWORDS: frozenset[str] = frozenset(
    {
        "senior",
        "sr",
        "junior",
        "jr",
        "staff",
        "principal",
        "lead",
        "head",
        "associate",
        "assistant",
        "manager",
        "director",
        "vp",
        "vice",
        "president",
        "chief",
        "intern",
        "internship",
        "co",
        "op",
        "coop",
        "the",
        "and",
        "or",
        "of",
        "for",
        "in",
        "at",
        "to",
        "a",
        "an",
        "remote",
        "hybrid",
        "onsite",
        "fulltime",
        "part",
        "time",
        "contract",
        "i",
        "ii",
        "iii",
        "iv",
        "v",
    }
)

_SIGNIFICANT_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

#: Stem-prefix length for fuzzy token<->body matching: compare the first N chars
#: so "researcher" matches a body that says "research", "analytics" matches
#: "analytic", etc. Tolerating morphological variants is what keeps the
#: cross-field false-positive rate near zero.
TITLE_STEM_LEN: int = 5


def significant_tokens(text: str) -> list[str]:
    """Lowercased alphanumeric tokens (len>=3) minus generic stopwords."""
    return [t for t in _SIGNIFICANT_TOKEN_RE.findall(text.lower()) if t not in TITLE_STOPWORDS]


def body_mentions_any_stem(
    tokens: list[str], body_lower: str, stem_len: int = TITLE_STEM_LEN
) -> bool:
    """True if any token's stem prefix appears in *body_lower*.

    ``body_lower`` MUST already be lowercased by the caller — both callers
    (``title_jd_mismatch`` and the jd-content contract) hold a lowercased body
    on the hot path, so this avoids re-scanning a multi-KB string per row.
    """
    if not tokens or not body_lower:
        return False
    return any(tok[:stem_len] in body_lower for tok in tokens)

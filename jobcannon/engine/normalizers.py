# PORTED from job_finder/normalizers.py @ 6a2af961fbffb78564ce8783277d916d60ad0906 (private job-cannon). Ledger L-0007.
"""Foundation-layer normalization utilities for job dedup keys.

Contains pure normalization functions (no web-layer dependencies) that can be
imported by both jobcannon.engine.models and jobcannon.engine.dedup_normalizer without
creating an upward dependency from the foundation layer into the web layer.
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
# NORMALIZER_VERSION is that version tag. Version 1 is the IMPLICIT pre-#238
# normalizer (no digit<->letter separator rule). Version 2 is the current
# algorithm (#212/#238 added the digit<->letter boundary split at line ~249).
#
# BUMP THIS whenever normalize_company / normalize_title semantics change so
# that the same (company, title) could map to a different dedup_key. Bumping it
# re-arms the standing re-key operation (`_run_rekey_if_stale` in
# job_finder/web/migrations/_post_hooks.py), which re-derives every row's key
# under the new version on next startup. The canary test in
# tests/test_dedup_normalizer.py fails loudly ("normalizer semantics changed --
# bump NORMALIZER_VERSION") if the functions drift without a bump — this is the
# enforcement that #238's once-ever-sentinel gap can never recur.
#
# Issue #1046 added strip_site_code_prefix() below but does NOT wire it into
# normalize_company() / bump this version — the one-off migration script and
# its own precision tests exercise the function directly. Wiring a
# site-code-prefix strip into the live normalization path (and re-arming the
# standing re-key across the whole registry) is deferred until the ingestion
# boundary that actually emits these names is proven (see the function
# docstring for what was and wasn't established).
NORMALIZER_VERSION: int = 2


# ---------------------------------------------------------------------------
# Company-match normalizer version (WI-15 / #1829)
# ---------------------------------------------------------------------------
#
# COMPANY_MATCH_NORMALIZER_VERSION tags `normalize_company_v2`, a MORE
# aggressive canonicalizer used ONLY for near-duplicate *detection / reporting*
# (company_resolver.find_duplicate_companies, scripts/company_dedup_report.py).
# It is deliberately NOT the dedup_key version: `normalize_company_v2` is never
# on the derive_dedup_key path, so bumping it does NOT re-arm the standing
# re-key (`_run_rekey_if_stale`) and does NOT mutate any stored dedup_key.
#
# This follows the same sanctioned precedent as #1046's strip_site_code_prefix
# above: a stronger normalization helper can exist beside normalize_company
# without being wired into the live dedup path or bumping NORMALIZER_VERSION.
# Moving the fold into the dedup_key path (which WOULD re-key the whole job
# table) is out of scope for WI-15 and tracked as a follow-up issue.
COMPANY_MATCH_NORMALIZER_VERSION: int = 2


# ---------------------------------------------------------------------------
# Company name deterministic cleanup regexes
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# --- normalize_company_v2 (detection-only) folds ---------------------------
# Leading article ("The Home Depot" -> "Home Depot"). Case-insensitive so it
# works whether applied before or after lowercasing.
_LEADING_ARTICLE_RE = re.compile(r"^the\s+", re.IGNORECASE)
# Trademark / copyright glyphs ("BetterSleep™" -> "BetterSleep").
_TRADEMARK_RE = re.compile(r"[™®©]")
# Apostrophe family: ASCII ', curly ' ', and backtick/acute ` ´. Stripped
# entirely (not just word-final "'s") so "Ken's Foods", "Ken`s Foods", and
# "Kens Foods" all fold together — the live registry contains a backtick twin
# (id 8168 "ken`s foods" vs id 1324 "ken's foods"), which a literal "'s" strip
# would miss.
_APOSTROPHE_FAMILY_RE = re.compile(r"['’‘`´]")
# Trailing punctuation ("Airwallex-" -> "Airwallex", "Acme," -> "Acme").
_TRAILING_PUNCT_RE = re.compile(r"[-–—,.\s]+$")

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
# Site-code prefix detection (Issue #1046)
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
# issue's "borderline" cases that need owner review rather than automatic
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
    """Strip a leading site-code prefix from a company name (Issue #1046).

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


def normalize_company_v2(company: str) -> str:
    """Aggressive company-name canonicalizer for near-duplicate DETECTION.

    Builds on ``normalize_company`` (v1) with additional folds that are too
    lossy for the dedup_key path but correct for grouping registry rows that
    are the same real employer written differently:

      - leading article ``the`` ("The Home Depot" -> "home depot")
      - trailing punctuation ("Airwallex-" -> "airwallex")
      - trademark / copyright glyphs ``™®©`` ("BetterSleep™" -> "bettersleep")
      - apostrophe-family chars ``' ' ' ` ´`` ("Ken's"/"Ken`s" -> "kens")
      - whitespace collapse

    v1 is re-applied inside a fixpoint loop so folds that unblock a v1 rule
    compose correctly — e.g. "Acme, Inc.-" needs the trailing dash removed
    (a v2 fold) before v1's legal-suffix strip can reach ", inc".

    IMPORTANT: this function is NEVER on the ``derive_dedup_key`` path. It is
    tagged by ``COMPANY_MATCH_NORMALIZER_VERSION``, not ``NORMALIZER_VERSION``,
    and is used only by detection/reporting callers. Do not use its output as a
    display value (always lowercase).

    Args:
        company: Raw company name string.

    Returns:
        Aggressively canonicalized, lowercased company name.
    """
    normalized = company
    prev = None
    while normalized != prev:
        prev = normalized
        # Re-apply the full v1 pass (html/tag/prefix/lowercase/suffix strip).
        normalized = normalize_company(normalized)
        # Remove trademark glyphs and every apostrophe-family char.
        normalized = _TRADEMARK_RE.sub("", normalized)
        normalized = _APOSTROPHE_FAMILY_RE.sub("", normalized)
        # Strip a leading article, but never to empty.
        m = _LEADING_ARTICLE_RE.match(normalized)
        if m and normalized[m.end() :].strip():
            normalized = normalized[m.end() :]
        # Strip trailing punctuation/whitespace, but never to empty.
        stripped = _TRAILING_PUNCT_RE.sub("", normalized)
        if stripped:
            normalized = stripped
        # Collapse any whitespace the folds exposed.
        normalized = " ".join(normalized.split()).strip()
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


# ---------------------------------------------------------------------------
# Self-duplicated title-suffix collapse (Issue #2017)
#
# An employer's API can return a title whose trailing comma-delimited segment
# repeats an earlier segment verbatim — e.g. Amazon's search.json returned
# "Data Scientist, EU Prime and Marketing Analytics & Science (PRIMAS), EU
# Prime and Marketing Analytics & Science (PRIMAS)" (id_icims 10521285,
# verified live 2026-09-01). The job_path slug carried the doubling too,
# confirming it is stored that way in Amazon's system (employer-authored, not
# a client-side concatenation defect — the Amazon scanner at
# _platforms_amazon.py:176 takes posting["title"] verbatim with no
# concatenation).
#
# Because derive_dedup_key is a function of the title, the doubling is not
# cosmetic: the same posting re-scraped with a correctly-constructed title
# produces a DIFFERENT key and lands as a new row. collapse_duplicated_suffix
# strips the repeated trailing segment at the ingestion write boundary
# (ParsedJob.from_job, after clean_title and before derive_dedup_key) so both
# the stored title and the dedup key are stable.
#
# This is NOT wired into normalize_title / NORMALIZER_VERSION — doing so would
# re-key the whole corpus (out of scope, #1866 owns the standing re-key
# question). It is a pre-derive_dedup_key step applied to newly ingested rows
# only. The title-hygiene re-sweep reports existing affected rows (detection +
# reporting only, no collapse) so the owner can find them without a hand audit.
# ---------------------------------------------------------------------------

#: Minimum normalized length for a comma-delimited segment to be considered
#: for the duplicate-suffix collapse. Guards against short-token repeats like
#: "Analyst, Analyst" (7 chars) or "Data Scientist, Data Scientist" (14 chars)
#: that could be legitimate (or at least not worth silently rewriting). The
#: PRIMAS subtitle is ~50 chars, well above this floor.
_MIN_DUPLICATE_SEGMENT_LEN: int = 15


def _normalize_segment(segment: str) -> str:
    """Case- and whitespace-normalized form of a comma-delimited title segment."""
    return " ".join(segment.split()).lower()


def collapse_duplicated_suffix(title: str) -> str:
    """Collapse a trailing comma-delimited segment that repeats an earlier one.

    Detects a title whose last comma-delimited segment is a verbatim repeat
    (case- and whitespace-normalized) of an earlier segment, and removes the
    duplicate. A minimum-length guard (``_MIN_DUPLICATE_SEGMENT_LEN``) prevents
    short-token repeats like ``"Analyst, Analyst"`` from being over-matched.

    Only the TRAILING segment is checked — a repeated segment in the middle of
    the title is not touched (it is not a suffix-doubling pattern). At most one
    duplicate is removed per call; if the result still has a duplicated suffix,
    the caller may re-apply (but the observed pattern is a single doubling).

    Examples::

        "Data Scientist, PRIMAS, PRIMAS"  ->  "Data Scientist, PRIMAS"
        "Data Scientist II, PRIMAS"       ->  "Data Scientist II, PRIMAS"  (no repeat)
        "Analyst, Analyst"                ->  "Analyst, Analyst"           (too short)

    Args:
        title: Cleaned job title (post ``clean_title``).

    Returns:
        Title with a duplicated trailing segment removed, or the original
        title unchanged if no duplicated suffix was detected.
    """
    if not title or "," not in title:
        return title
    segments = title.split(",")
    if len(segments) < 3:
        # Need at least 3 segments: <head>, <segment>, <segment> — the last
        # must repeat an earlier one. A 2-segment title ("A, B") has no earlier
        # segment for B to duplicate (A != B is the normal case).
        return title
    last_norm = _normalize_segment(segments[-1])
    if len(last_norm) < _MIN_DUPLICATE_SEGMENT_LEN:
        return title
    for earlier in segments[:-1]:
        if _normalize_segment(earlier) == last_norm:
            return ",".join(segments[:-1])
    return title


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
# no web/db dependency — safe to import from either layer.
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
    tokens: list[str],
    body_lower: str,
    stem_len: int = TITLE_STEM_LEN,
    *,
    boundary_short: bool = False,
) -> bool:
    """True if any token's stem prefix appears in *body_lower*.

    ``body_lower`` MUST already be lowercased by the caller — both callers
    (``title_jd_mismatch`` and the jd-content contract) hold a lowercased body
    on the hot path, so this avoids re-scanning a multi-KB string per row.

    When ``boundary_short`` is True, a token whose stem prefix is shorter than
    ``stem_len`` is matched with a word-boundary regex (``\\b{stem}\\b``)
    instead of an unanchored substring test. This keeps short
    employer-identifying stems (e.g. ``c3``, ``iot``) in the jd-content
    company-absence cross-field check from substring-matching inside
    unrelated words (``patriot``, ``riot``) — issue #1892. Stems of length
    >= ``stem_len`` keep the unanchored prefix-substring behaviour that
    tolerates morphological variants and holds the cross-field false-positive
    rate near zero.
    """
    if not tokens or not body_lower:
        return False
    for tok in tokens:
        stem = tok[:stem_len]
        if boundary_short and len(stem) < stem_len:
            if re.search(rf"\b{re.escape(stem)}\b", body_lower):
                return True
        elif stem in body_lower:
            return True
    return False

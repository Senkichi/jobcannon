"""Title hygiene + URL-path navigation filters for careers crawl extraction.

Pure functions. No I/O, no shared state. Imported by the static and
Playwright tier modules to filter and clean candidate job postings
before keyword matching.
"""

from __future__ import annotations

import re

from jobcannon.engine.careers_crawler._title_contract import (
    _CTA_RE,
    _DATE_TOKEN_RE,
    _RELATIVE_POSTED_RE,
    _TRAILING_ARROW_RE,
)

# Links with these path prefixes are navigation, not job listings
_NAV_PATH_PREFIXES = (
    "/about",
    "/contact",
    "/blog",
    "/news",
    "/press",
    "/privacy",
    "/terms",
    "/legal",
    "/login",
    "/signup",
    "/register",
    "/faq",
    "/help",
    "/support",
    "/accessibility",
    "/sitemap",
    "/cookie",
    "/search",
    "/events",
)

# Prefixes that map to "search form / search results" pages. We keep the bare
# path filtered (the form itself is nav), but allow `<prefix>/<segment>` shapes
# through — some ATS sites (e.g. ByteDance, `joinbytedance.com/search/<id>`)
# encode job-detail URLs under the same path, and over-aggressive prefix
# filtering eats every tile on the listing page.
_NAV_PREFIXES_WITH_SUBPATH_JOBS = ("/search",)


def _is_nav_path(path: str) -> bool:
    """Return True if `path` is a known navigation link (not a job listing).

    Most prefixes in `_NAV_PATH_PREFIXES` are matched as plain prefixes — any
    sub-path under `/about`, `/blog`, etc. is nav. The prefixes listed in
    `_NAV_PREFIXES_WITH_SUBPATH_JOBS` are filtered only when the path IS the
    prefix (optionally with a trailing slash); deeper paths under them are
    treated as job-detail URLs and let through.

    See FOLLOWUPS round-15 Gap #3 (ByteDance `/search/<id>` tiles).
    """
    path_lower = path.lower()
    for prefix in _NAV_PATH_PREFIXES:
        if not path_lower.startswith(prefix):
            continue
        if prefix in _NAV_PREFIXES_WITH_SUBPATH_JOBS:
            rest = path_lower[len(prefix) :].strip("/")
            if rest:
                # `/search/<id>` — job detail, not nav.
                continue
        return True
    return False


# Regex to strip trailing location text from concatenated title+location
_LOCATION_SUFFIX_RE = re.compile(
    r"\s*[-–—|·•]\s*(?:Remote|Hybrid|On-?site|Anywhere|Multiple|Worldwide).*$",
    re.IGNORECASE,
)

# Broader location suffix: city/state/country patterns at end of title
# Matches: "- New York, NY", "- San Francisco, CA", "- United States", etc.
_CITY_SUFFIX_RE = re.compile(
    r"\s*[-–—|·•]\s*[A-Z][a-z]+(?:\s[A-Z][a-z]+)*(?:,\s*[A-Z]{2,})?\s*$",
)

# No-separator trailing location: title ends with a 2+ uppercase character
# run preceded immediately by a lowercase letter or closing paren (no space
# or hyphen between them). The trailing run must either run to end-of-string
# alone, be followed by a comma-separated list of TitleCase tokens, or be
# followed by a parenthesized qualifier — these three shapes match real
# concatenated location text like "(Evergreen)NY, DC, Oakland",
# "ScientistUSA(Remote)", or "(Backend)CA". The ALLCAPS-only end-of-string
# requirement protects single-word PascalCase titles like "iOS Developer"
# (where stripping starting at "OS" would leave just "i").
_NOSEP_TRAIL_LOC_RE = re.compile(
    r"""
    (?<=[a-z\)])                            # title-end context: lowercase or close paren
    (?=[A-Z][A-Z])                          # next chars are 2+ caps
    [A-Z]{2,}                               # an ALLCAPS run (state code, country, etc.)
    (?:
        (?:,\s*[A-Za-z][A-Za-z]+)+          # comma-separated list of TitleCase words
        |
        \([^)]*\)                           # parenthesized qualifier (Remote)
    )?
    \s*$                                    # must reach end of string
    """,
    re.VERBOSE,
)

# Workday-style req ID glued to the end of the title with no separator:
# "Senior Technical Data Analyst - Operations E2E Data Intelligent Systems
# JR2018470US, CA, Santa Clara" — title runs into "JR2018470" (req ID) then
# straight into "US, CA, Santa Clara" (location). The req-id is the anchor:
# 2+ ALLCAPS letters followed by digits, preceded by a lowercase letter or
# closing paren (so we don't false-match an internal token like "E2E" which
# is preceded by whitespace). Strips from the req-id to end-of-string,
# absorbing any trailing location text that came glued on with it.
#
# The lookbehind plus `[A-Z]{2,}\d+` requirement keeps the false-positive
# rate low: bare ALLCAPS runs (state codes) are already handled by
# _NOSEP_TRAIL_LOC_RE, and natural title tokens like "E2E" / "AI" / "iOS"
# either have whitespace before them or lack the digit suffix.
#
# See FOLLOWUPS round 15 Gap #1 (NVIDIA Workday concatenation).
_REQID_PREFIX_RE = re.compile(r"(?<=[a-z\)])[A-Z]{2,}\d+.*$")

# Leading "logo placeholder" letters: 1-2 ALLCAPS letters glued to the start
# of the real title with no separator, where the next character starts a
# Capital+lowercase word. Catches aggregator garbage like "CSenior Vice
# President" (Citigroup logo letter), "EHApplication Development" (Evernorth
# Health), "NLead Analyst" (NewYork-Presbyterian). Safe because "IT
# Specialist", "AI Engineer", "MSI - Marvell" all have a space or non-
# lowercase second char that breaks the lookahead.
_LEADING_LOGO_LETTERS_RE = re.compile(r"^([A-Z]{1,2})(?=[A-Z][a-z])")


def _strip_leading_logo_letters(s: str) -> str:
    """Strip 1-2 leading ALLCAPS letters that are a logo placeholder."""
    return _LEADING_LOGO_LETTERS_RE.sub("", s, count=1)


# Trailing card-chrome tail: from the first embedded date/CTA token to end of
# string, plus a bare trailing arrow glyph. This is the deterministic REPAIR
# counterpart to the contract's quarantine detection — it recovers the real
# title from a scraped job card ("Data Scientist / IA Engineer Jun 15, 2026
# View Job ->" -> "Data Scientist / IA Engineer"). The date/CTA alternations are
# composed from the SAME canonical patterns the contract validates against
# (_title_contract._DATE_TOKEN_RE / _CTA_RE), so the strip anchor can never
# drift from what title_contract_violation flags.
_CARD_JUNK_ANCHOR_RE = re.compile(
    rf"\s*(?:(?:{_DATE_TOKEN_RE.pattern})|(?:{_CTA_RE.pattern})).*$",
    re.IGNORECASE,
)
_MIN_REPAIR_HEAD = 3

# Glued listing-tile card: "<Title><Location>Posted N <unit> ago" where the
# location is concatenated to the title with NO separator (a lowercase / close-
# paren -> uppercase boundary) and the whole tail is confirmed as card chrome by
# the trailing relative posted-date. Strips from the no-separator boundary to
# end-of-string, recovering just the role name
# ("Senior Data ScientistUnited States, Multiple LocationsPosted 15 days ago"
# -> "Senior Data Scientist"). The trailing relative-posted-date confirmation is
# load-bearing — no legitimate title ends in "Posted N days ago", so it lets us
# strip a camelCase-glued tail that the generic ALLCAPS-only _NOSEP_TRAIL_LOC_RE
# cannot. Only the posted-date sub-pattern is case-insensitive (inline (?i:...));
# the boundary lookarounds stay case-sensitive so they actually detect camelCase.
# Seen on Microsoft (company_id=217) and other Phenom careers tiles scraped via
# careers_crawl before the metadata-blob ingest gate existed.
_GLUED_LOC_POSTED_RE = re.compile(
    rf"(?<=[a-z\)])(?=[A-Z]).*?(?i:{_RELATIVE_POSTED_RE.pattern})\s*$",
)


def _strip_from_anchor(s: str, anchor: re.Pattern[str]) -> str:
    """Strip from the first *anchor* match to end-of-string, if a non-trivial
    head (>= _MIN_REPAIR_HEAD chars) remains.

    Conservative: never reduces a title to nothing. Shared by the glued-location,
    relative-posted, and date/CTA card-chrome strippers so all three honor the
    same minimum-head guard.
    """
    match = anchor.search(s)
    if match:
        head = s[: match.start()].strip()
        if len(head) >= _MIN_REPAIR_HEAD:
            return head
    return s


def _strip_trailing_card_junk(s: str) -> str:
    """Strip a trailing date/CTA/posting-age card-chrome tail, recovering the title.

    Conservative: only strips when a non-trivial head (>= _MIN_REPAIR_HEAD chars)
    remains, so a title is never reduced to nothing. A bare trailing arrow glyph
    is always removed. Deterministic and idempotent — re-running on an already
    repaired title is a no-op (which the retroactive re-sweep relies on).

    Anchor order matters: the glued "<Title><Location>Posted N ago" strip runs
    FIRST because it also consumes the no-separator location, which the bare
    relative-posted / date anchors below would otherwise leave stranded.
    """
    cleaned = _TRAILING_ARROW_RE.sub("", s).rstrip()
    cleaned = _strip_from_anchor(cleaned, _GLUED_LOC_POSTED_RE)
    cleaned = _strip_from_anchor(cleaned, _RELATIVE_POSTED_RE)
    cleaned = _strip_from_anchor(cleaned, _CARD_JUNK_ANCHOR_RE)
    return cleaned


# Heuristic guards for detecting that a "title" is actually a glued metadata
# blob (description + location + posting date + req ID concatenated together).
# These come up on aggregator-style careers pages where the underlying HTML
# lays out fields as adjacent inline siblings — get_text(strip=True) merges
# them all without separators. See FOLLOWUPS.md 2026-05-27 audit.

# Maximum plausible job title length in characters. Real titles top out around
# 110 chars even with senior/staff/principal modifiers + parenthesized scopes.
# Beyond 140 the candidate is almost certainly a metadata blob.
_MAX_TITLE_LEN = 140

# Phrase markers that only appear in description/metadata text — never in a
# real title. Case-insensitive substring match.
_METADATA_PHRASE_MARKERS = (
    "posted ",  # "Posted 10 days ago"
    "apply by",  # "Apply byApr-29-26"
    "agency",  # "AgencyUNDP" (labeled-form aggregator)
    "post level",  # UNDP-style label
    "job title",  # UNDP-style label glued in
    "more accessible",  # "Innovation and Automation" body text
    "description ",  # Generic description leader
    "required:",  # Glued-in body text
    "responsibilities",  # Glued-in body text
)

# Currency symbol indicates compensation got concatenated into the title.
_HAS_DOLLAR_RE = re.compile(r"\$\s*\d")

# Req-ID-followed-by-pipe pattern: "SQL2354308|Chennai, Tamil Nadu" —
# digits run followed by a pipe and TitleCase text.
_REQ_ID_PIPE_RE = re.compile(r"\d{4,}\s*\|\s*[A-Z]")

# Result-count / category-landing tile pattern (#211): a leading integer
# (optionally comma-grouped and/or `+`-suffixed) followed by descriptive text
# that END-anchors on an aggregate-listing noun ("jobs", "positions",
# "openings", "roles", "opportunities", "results"). These are category landing
# pages — "84 Data Scientist Jobs", "1,200+ openings" — not single postings.
#
# The leading-digit + end-anchored-noun shape is what makes this safe: a real
# posting that happens to start with a number ("100 Women in Finance — Analyst",
# "3D Artist", "5G Network Engineer") does not end on a listing noun, so it
# stays unmatched. The end anchor (`\s*$`) is load-bearing — it prevents
# matching titles where a listing noun appears mid-string ("Jobs Data Analyst").
_LISTING_TILE_RE = re.compile(
    r"^\s*\d[\d,]*\+?\s+.*\b"
    r"(?:jobs?|positions?|openings?|roles?|opportunities|results)"
    r"\s*$",
    re.IGNORECASE,
)


def _is_listing_tile(title: str) -> bool:
    """Detect result-count / category-landing tiles masquerading as postings.

    A "listing tile" is the anchor text of a careers-page category link —
    "84 Data Scientist Jobs", "1,200+ openings", "12 results" — that the static
    crawler can mistake for a single posting because it ordered-words-matches a
    target title. A count tile is categorically not an applyable posting; it has
    zero human-triage value, so callers HARD-DROP it (see ``ListingTileError``
    in ``parsed_job``) rather than persisting it for review.

    Shape (case-insensitive): a leading integer (optionally comma-grouped,
    optionally `+`-suffixed) + whitespace + any text + an end-anchored
    aggregate-listing noun. The leading-count requirement plus the end anchor
    keep legitimate numeric-prefixed titles ("100 Women in Finance — Analyst")
    unmatched.

    Public (no leading underscore): the SERP/portal and static-crawler paths
    plus ``ParsedJob.from_job`` all reuse this single predicate so the
    "only real postings enter the pipeline" invariant has one definition.
    """
    if not title:
        return False
    return bool(_LISTING_TILE_RE.search(title))


def _is_metadata_blob(title: str) -> bool:
    """Detect titles that are actually concatenated metadata/description text.

    Used by careers_crawl extraction to skip aggregator pages where the
    surrounding markup glues the title together with location, req ID,
    posting date, and description preview without separator whitespace.
    Those rows produce titles like "Senior Data Scientist - GenAI...
    SQL2354308|Chennai, Tamil Nadu" or "Job TitleTech Lead AnalystPost
    levelNPSA-9Apply byApr-29-26AgencyUNDP..." that are useless for
    scoring or display.

    Conservative: prefers false negatives (let some glued blobs through)
    over false positives (drop a legitimate long title). Run this AFTER
    _clean_title has stripped suffixes and logo letters — short legitimate
    titles will never trip it.
    """
    if not title:
        return False
    if len(title) > _MAX_TITLE_LEN:
        return True
    lowered = title.lower()
    if any(marker in lowered for marker in _METADATA_PHRASE_MARKERS):
        return True
    if _HAS_DOLLAR_RE.search(title):
        return True
    return bool(_REQ_ID_PIPE_RE.search(title))


# ---------------------------------------------------------------------------
# Title-NODE isolation (the structural fix for sibling glue).
#
# A careers listing tile renders the role name, location, and posting-date as
# adjacent inline children of one <a>. ``tag.get_text(strip=True)`` concatenates
# them with NO separator, so the greedy text run is a glued blob:
#   "Senior Data ScientistUnited States, Multiple LocationsPosted 15 days ago"
# (seen on Microsoft company_id=217 and other Phenom/iCIMS/Workday tiles).
#
# Rather than repair that blob downstream with a growing set of strip regexes
# (the whack-a-mole the title contract warns against), we isolate the title
# NODE and take ONLY its own text — the location/date are never glued on. Every
# major ATS marks its title element (Phenom ``data-ph-at-id="job-title-text"``,
# iCIMS/Workday ``class="...title..."``, schema.org ``itemprop="title"``); when
# no marker exists we descend wrappers to the first text-bearing child leaf,
# which is the role name (siblings render after it). Regex repair stays as the
# fallback for bare-text links with an appended dash/pipe location.
# ---------------------------------------------------------------------------

#: Minimum length for an extracted title-node text to be accepted (mirrors the
#: pre-split heading/first-child >= 5 guard).
_MIN_TITLE_NODE_CHARS = 5

#: Element tags that can carry a tile's title text (descended in DOM order).
_TITLE_NODE_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "span", "div", "p"]

#: 'title' as a delimited component — anchored on start/whitespace/-/_ so
#: "job-title" / "posting_title" match while "subtitle" / "untitled" (no
#: delimiter before 'title') do not. camelCase is pre-split to a hyphen by
#: ``_has_title_token`` so "jobTitle" also matches.
_TITLE_TOKEN_RE = re.compile(r"(?:^|[\s_\-])title(?:[\s_\-]|$)", re.IGNORECASE)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _has_title_token(value: str) -> bool:
    """True if *value* carries 'title' as a delimited component (camelCase-aware)."""
    return bool(_TITLE_TOKEN_RE.search(_CAMEL_BOUNDARY_RE.sub("-", value)))


def _is_title_marked(el) -> bool:
    """Predicate: does *el* explicitly flag itself as the job-title node?

    Matches a ``title`` ``itemprop`` (schema.org microdata), any ``class`` token
    containing 'title', or any ``data-*`` attribute whose value contains 'title'
    (Phenom ``data-ph-at-id="job-title-text"``). Passed to ``BeautifulSoup.find``
    so only Tags reach it.
    """
    if (el.get("itemprop") or "").strip().lower() == "title":
        return True
    if any(_has_title_token(c) for c in el.get("class") or []):
        return True
    return any(
        name.startswith("data-") and isinstance(value, str) and _has_title_token(value)
        for name, value in el.attrs.items()
    )


def _looks_glued(text: str) -> bool:
    """True if *text* still carries card-chrome / metadata glue (not a clean title).

    Guards the wrapper-text fallback in ``_leaf_title_text``: a mislabeled
    container is rejected rather than trusted as the title node.
    """
    return _strip_trailing_card_junk(text) != text or _is_metadata_blob(text)


def _leaf_title_text(el) -> str:
    """Return the title text of the single title-bearing leaf within *el*.

    Descends wrapper elements to the FIRST usable child leaf so a sibling
    location / posting-date field rendered in the same container is never glued
    onto the role name. Falls back to the element's own text only when it has no
    block/inline child leaf AND that text is not itself a glued multi-field blob
    (e.g. ``<h3>Senior <em>Data</em> Scientist</h3>``). Returns '' when nothing
    yields >= ``_MIN_TITLE_NODE_CHARS``.
    """
    if el.find(True) is None:
        # Leaf element: its text is one text node — safe to take whole.
        text = el.get_text(strip=True)
    else:
        text = ""
        for child in el.find_all(_TITLE_NODE_TAGS, recursive=False):
            text = _leaf_title_text(child)
            if text:
                break
        if not text:
            own = el.get_text(strip=True)
            if not _looks_glued(own):
                text = own
    return text if len(text) >= _MIN_TITLE_NODE_CHARS else ""


def _title_node_text(tag) -> str:
    """Isolate the title NODE's own text from a link tag (no sibling glue).

    Priority: (1) a real heading (h1-h6) anywhere inside, (2) an element
    explicitly flagged as the title (class / data-* / itemprop), (3) the first
    text-bearing child leaf, descending one wrapper level. Each takes ONLY that
    node's text. Returns '' when the link has no element children (its raw text
    must go to the regex strippers for separator-based location removal) or no
    usable node is found.
    """
    heading = tag.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if heading is not None:
        text = _leaf_title_text(heading)
        if text:
            return text

    marked = tag.find(_is_title_marked)
    if marked is not None:
        text = _leaf_title_text(marked)
        if text:
            return text

    if tag.find(True) is not None:
        for child in tag.find_all(_TITLE_NODE_TAGS, recursive=False):
            text = _leaf_title_text(child)
            if text:
                return text
    return ""


def _clean_title(tag, raw_text: str) -> str:
    """Extract clean job title from a link tag, stripping appended location.

    Strategy:
    1. Isolate the title NODE's own text via ``_title_node_text`` — a heading,
       an element explicitly flagged as the title, or the first child leaf.
       This is the STRUCTURAL fix for sibling glue: a wrapper <a> renders title
       + location + posting-date as adjacent inline children, so
       ``tag.get_text(strip=True)`` concatenates them with no separator. Taking
       only the title node's text means the location/date are never glued on in
       the first place (Microsoft company_id=217 + other Phenom tiles).
    2. Regex stripping on the raw text — the fallback for links whose title is a
       bare text node (no element children) with a dash/pipe-separated location
       suffix, a no-separator state list, a Workday-style req-id, a trailing
       date/CTA card-chrome tail, or leading logo placeholder letters.

    Args:
        tag: BeautifulSoup <a> tag.
        raw_text: Full text from tag.get_text(strip=True).

    Returns:
        Cleaned title string.
    """
    # Strategy 1: isolate the title node's own text (never the greedy subtree).
    node_text = _title_node_text(tag)
    if node_text:
        return _strip_leading_logo_letters(node_text)

    # Strategy 2: regex strip on the raw text. Order matters — strip the
    # trailing date/CTA card-chrome tail FIRST (so the real title is exposed
    # for the location/req-id strippers below), then separator-based location
    # patterns, then the Workday-style req-id glob (which also absorbs trailing
    # location text glued on with the req-id), then the no-separator trailing-
    # uppercase run, then leading logo letters last.
    cleaned = _strip_trailing_card_junk(raw_text)
    cleaned = _LOCATION_SUFFIX_RE.sub("", cleaned)
    cleaned = _strip_city_suffix_guarded(cleaned)
    cleaned = _REQID_PREFIX_RE.sub("", cleaned)
    cleaned = _NOSEP_TRAIL_LOC_RE.sub("", cleaned)
    cleaned = _strip_leading_logo_letters(cleaned)
    return cleaned.strip() or raw_text


def clean_title(title: str) -> str:
    """String-only variant of the HTML-aware _clean_title().

    Applies regex-based location suffix stripping (strategy 3 from the
    HTML-aware ``_clean_title``) without needing a BeautifulSoup tag.
    Called from ``ParsedJob.from_job()`` to normalize titles from all
    ingestion paths before downstream field storage and I-09 bleed checks.

    For HTML-context extraction (where a BeautifulSoup ``<a>`` tag is
    available), use ``_clean_title()`` which has higher precision via
    heading/first-child strategies.
    """
    cleaned = _strip_trailing_card_junk(title)
    cleaned = _LOCATION_SUFFIX_RE.sub("", cleaned)
    cleaned = _strip_city_suffix_guarded(cleaned)
    cleaned = _REQID_PREFIX_RE.sub("", cleaned)
    cleaned = _NOSEP_TRAIL_LOC_RE.sub("", cleaned)
    cleaned = _strip_leading_logo_letters(cleaned)
    return cleaned.strip() or title


#: Public alias for ``_is_metadata_blob``.
#: Imported by ``ParsedJob.from_job`` for universal metadata-blob detection
#: across every ingestion path (Phase 48.01).
is_metadata_blob = _is_metadata_blob

#: Public alias for ``_is_listing_tile``.
#: Imported by ``ParsedJob.from_job`` (#211) so result-count / category-landing
#: tiles are hard-dropped at the universal posting-hygiene enforcement point,
#: across every ingestion path. Reused by the static crawler tier for a cheap
#: early exit before ParsedJob construction.
is_listing_tile = _is_listing_tile


def _strip_city_suffix_guarded(text: str) -> str:
    """Apply _CITY_SUFFIX_RE only when the text BEFORE the dash looks like a
    real job title — short + ALLCAPS prefixes are almost always brand
    abbreviations, not job titles, and stripping the brand off them is a
    silent data-loss bug.

    Concrete case that motivated this guard: 'MSI - Marvell Semiconductor'
    has the same trailing-TitleCase-after-dash shape as
    'Senior Engineer - San Francisco', but the suffix is a brand name, not a
    location. Heuristic: a legitimate job-title prefix is at least 5 chars
    long AND contains at least one lowercase letter (so 'MSI', 'IBM', 'AWS'
    are skipped). Tradeoff: we leak some bare-city suffixes through when the
    preceding title is short — but those titles are still informative, where
    'MSI' alone has zero brand signal.

    See FOLLOWUPS.md 2026-05-27 audit ("_CITY_SUFFIX_RE over-strip").
    """
    match = _CITY_SUFFIX_RE.search(text)
    if not match:
        return text
    before = text[: match.start()].strip()
    if len(before) >= 5 and any(c.islower() for c in before):
        return before
    return text

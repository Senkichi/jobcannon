# PORTED from job_finder/db/_jd_content_contract.py @ 3aaa360feac49fcd96e21c52e4fc7295ad6914c4 (private job-cannon). Ledger L-0004.
"""Positive jd_full content contract — "is this text the description of THIS job?"

WHY THIS EXISTS (the same architectural inversion as ``_title_contract``)
-------------------------------------------------------------------------
The historical jd_full gate (``_jd_full._is_jd_junk``, I-13) is a *fail-open
denylist*: ``len < 200`` OR the first 200 chars ``.startswith()`` one of 7
hardcoded prefixes. Anything long that does not *begin* with those 7 strings is
treated as a valid job description, scored, and surfaced. A live-corpus audit
(13,664 rows) found that lets through **~14–22%** non-JD bodies — Wikipedia
articles, "REQUEST DENIED" bot walls, expired-posting pages, careers-landing
chrome, job-listing index pages, even a 29 KB site-maintenance notice — and that
**~1,400–2,500 of them had already been scored** against that garbage.

Enumerating bad shapes cannot win (the junk is far too heterogeneous) and
over-fires on real JDs (a JD that mentions "cookies" or "cloudflare" is not
junk). So this module inverts the default to **fail-closed**: a stored jd_full is
trustworthy only if it positively looks like a single job posting for its own
title. Unknown ⇒ quarantine (clear + re-enrich), never silently scored.

THREE-OUTCOME VERDICT (so the LLM only runs on the genuine residual)
--------------------------------------------------------------------
``classify_jd_content`` returns one of:

* ``REJECT`` — deterministic, HIGH precision. The body is provably not this job's
  posting: a wrong page (Wikipedia / bot wall / block page / listing index /
  404), a dead posting (expired / filled), or a substantial body that shares
  ZERO of the title's content stems (the I-17 ``title_jd_mismatch`` signal,
  finally wired here as a *jd-content* signal exactly as its deferral note
  anticipated). Safe to act on with no LLM and no human.
* ``CLEAN`` — deterministic, HIGH confidence: a JD-shape signal is present AND the
  body is grounded in the title/company AND it is substantial. The common case.
* ``AMBIGUOUS`` — everything else. Resolved by a cheap local-LLM tie-breaker
  ("is this the JD for <title> at <company>?") run by the background adjudicator,
  NOT on the hot ingest path and NOT during the synchronous startup re-sweep.

ENFORCEMENT (single points, mirrored from the title contract)
-------------------------------------------------------------
* ``jd_content_reject`` runs inside the sole sanctioned writer ``set_jd_full``
  and at the ``ParsedJob.from_job`` ingest gate — the deterministic floor that
  can never store obvious junk. Both callers pass the job's ``title`` when they
  have it (the enrichment write path always does), so the title cross-field
  reject (I-17 ``title_zero_overlap``) fires at the write chokepoint, falling
  back to content-only signals when no title is available. A leading JSON
  configuration blob with an empty or missing ``job_description`` is also
  rejected here, so a long Eightfold/Netflix micro-site config payload cannot
  satisfy the length gate while carrying no prose.
* ``classify_jd_content`` (the full 3-way) runs in the background adjudicator
  and the versioned re-sweep, so the AMBIGUOUS residual is LLM-resolved off the
  hot path and a rule improvement heals the whole corpus on a
  ``JD_CONTENT_VERSION`` bump.

The module is PURE (regex + JSON structural checks + the shared ``normalizers``
token helpers) so it is deterministic, unit-testable, and importable from
``db/`` without a web cycle.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum

# PORT-SEAM: private source imports get_jd_full_thresholds from job_finder.config
# (app-level, not portable). The resolver is inlined below instead of imported.
from jobcannon.engine.normalizers import (
    TITLE_STOPWORDS,
    body_mentions_any_stem,
    significant_tokens,
)

# ---------------------------------------------------------------------------
# Version watermark (D-8). BUMP whenever the rules below change such that an
# already-stored jd_full could newly pass or newly fail. Bumping re-arms the
# standing re-sweep so the whole corpus is re-validated under the new version.
# Mirrors NORMALIZER_VERSION / TITLE_HYGIENE_VERSION.
# ---------------------------------------------------------------------------
# v9 (#1952): re-arm after the ``empty_requirements_header`` AMBIGUOUS-widening
# signal was added — a structurally-complete posting whose requirement-bearing
# section headers (## Key Responsibilities / ## Skills / ## Qualifications /
# etc.) are EMPTY no longer passes ``classify_jd_content`` as CLEAN. Rows
# stamped CLEAN under v8 or earlier may have been classified before this
# signal existed, so the watermark bump is what re-arms them.
#
# The deterministic startup re-sweep (``_run_jd_content_resweep_if_stale``)
# does NOT call ``classify_jd_content`` — it only runs ``jd_content_reject``,
# which this signal deliberately does not touch (no new write-time rejection,
# per the #1814 discipline this signal follows). So the bump is a no-op on the
# startup sweep. The operative effect is entirely in the adjudicator:
# ``jd_adjudicator`` re-selects every row with
# ``jd_adjudicated_version < JD_CONTENT_VERSION`` (including previously-CLEAN
# rows whose stamped version fell behind) for LLM re-adjudication, and
# ``job_scorer``'s ``awaiting_jd_adjudication`` precheck gate follows suit.
JD_CONTENT_VERSION: int = 9

# Reason codes emitted into jobs.unresolved_reasons (the m078 quarantine surface).
# Distinct from I-13's ``jd_full_junk`` (the length/density gate, owned by
# ``_jd_full``): these are content-provenance failures owned by THIS contract and
# recomputed by its re-sweep.
JD_OFFSITE: str = "jd_full_offsite"
JD_EXPIRED: str = "jd_full_expired"
JD_TRUNCATED: str = "jd_full_truncated"

#: All jd-content reason codes the re-sweep owns + recomputes.
JD_CONTENT_REASON_CODES: frozenset[str] = frozenset({JD_OFFSITE, JD_EXPIRED, JD_TRUNCATED})

# ---------------------------------------------------------------------------
# Tunables (validated against the live 13,664-row corpus via scripts/jd_*).
# ---------------------------------------------------------------------------
_HEAD_WINDOW: int = 400  # chars examined for "leads-with-junk" signals
_CLEAN_MIN_CHARS: int = 600  # a confidently-CLEAN body must be at least this long
_XFIELD_MIN_CHARS: int = 300  # min body before the title zero-overlap check can fire
_XFIELD_MIN_TOKENS: int = 2  # min significant title tokens before zero-overlap fires

# ---------------------------------------------------------------------------
# HIGH-PRECISION REJECT signals. Each was confirmed near-zero-false-positive on
# the live corpus. Keep this set tight and high-precision — it is NOT an
# open-ended junk blocklist; the AMBIGUOUS→LLM path is where uncertainty goes.
# ---------------------------------------------------------------------------

#: Block / challenge / encyclopedic markers — only meaningful when they LEAD the
#: body (checked in the first _HEAD_WINDOW chars), so a JD that merely mentions
#: "cloudflare" or "javascript" deep in prose is not flagged.
_HEAD_BLOCK_RE = re.compile(
    r"from wikipedia, the free encyclopedia"
    r"|request denied"
    r"|are you a robot"
    r"|verify you are (?:human|not a robot)"
    r"|attention required"
    r"|just a moment"
    r"|checking your browser"
    r"|you have been blocked"
    r"|access (?:to this page )?(?:has been )?denied"
    r"|enable javascript"
    r"|please enable (?:js|javascript|cookies)"
    r"|unusual traffic from your"
    r"|complete the security check"
    r"|ddos protection",
    re.IGNORECASE,
)

#: A job-listing INDEX captured as a posting: "399 ... jobs in Boston",
#: "1,000+ Chief Clinical Officer jobs in United States", "# 9 Fox Motors Jobs in
#: United States". The count (optionally comma-grouped / "+"-suffixed) + "jobs in
#: <place>" header is the structural tell and does not occur in a single
#: posting's body.
_LISTING_COUNT_RE = re.compile(
    r"\b\d[\d,]{0,4}\+?\s+[\w\s,&/+.\-]{0,40}?\bjobs\s+in\b",
    re.IGNORECASE,
)

#: A job-search RESULTS listing captured as a posting: "50,048 results",
#: "1,239 results". The ``<count> results`` header is the structural tell of a
#: search-results index page and does not occur in a single posting's body.
#: Sister of ``_LISTING_COUNT_RE`` for the "results" phrasing observed on
#: Eightfold/Capital One style result pages (issue #1814). Evaluated against
#: the WHOLE body by ``classify_jd_content`` (not just ``_HEAD_WINDOW``) — a
#: real JD does not contain a result-count block anywhere — and routed to
#: AMBIGUOUS, not REJECT, so the tight REJECT set is unchanged.
_LISTING_RESULTS_RE = re.compile(
    r"\b\d[\d,]{0,4}\+?\s+results\b",
    re.IGNORECASE,
)

#: 404 / page-not-found offsite captures — head-only (a real JD does not LEAD
#: with these). "404" must appear in an explicit error context: a bare "404"
#: matches legitimate content ("404 Total Employees", a "$404" rate), so it is
#: NOT accepted on its own.
_NOT_FOUND_RE = re.compile(
    r"\b404\s+(?:error|not\s+found|page)"
    r"|\b(?:error|http)\s+404\b"
    r"|\bpage\s+not\s+found\b"
    r"|the\s+page\s+you\s+(?:requested|are\s+looking\s+for)"
    r"|\bpage\s+(?:cannot|can(?:'|’)?t)\s+be\s+found\b"
    r"|\b410\s+gone\b",
    re.IGNORECASE,
)

#: Unfilled CMS/site-builder placeholder scaffold (a careers page whose template
#: was never customized with real content) — head-only, same discipline as
#: _HEAD_BLOCK_RE. NOT checked against the whole body: a real, already-scored JD
#: can carry this exact text as leftover widget noise (a "related content"/video
#: carousel the employer never cleaned up) FAR into the page — confirmed on the
#: live corpus (PNC "Watch the video... Widget title goes here. Your engaging
#: subtitle goes here" at char 1678, and four UnitedHealth/Petco rows with
#: "Lorem Ipsum is simply dummy text" past char 5000 — all genuine, grounded,
#: shape-bearing JDs). Only diagnostic when it LEADS the page, i.e. the page IS
#: the unfilled template rather than a real JD with template noise appended.
_CMS_PLACEHOLDER_RE = re.compile(
    r"your engaging (?:footer )?subtitle goes here"
    r"|widget title goes here"
    r"|lorem ipsum is simply dummy text",
    re.IGNORECASE,
)

#: "Zero results for your search" chrome from a careers-search widget — head-only.
#: Deliberately requires "your search" co-occurring (not just "no jobs"/"no
#: positions") because a real JD can legitimately say "there are no jobs to
#: display" about an unrelated RELATED-JOBS widget elsewhere on its own page
#: (confirmed on a live Target row, head char 356) without itself being junk.
_NO_SEARCH_RESULTS_RE = re.compile(
    r"no jobs? (?:for|match(?:ing)?) your search"
    r"|no (?:results|positions|openings) (?:found )?for your search",
    re.IGNORECASE,
)

#: Dead-posting markers (anywhere). Phrases are full page-template sentences that
#: a live posting's own body never contains about itself.
_EXPIRED_RE = re.compile(
    r"\bthis\s+(?:job|position|posting|role|listing|vacancy|opening)\s+(?:is|has\s+been)\s+"
    r"(?:no\s+longer\s+available|no\s+longer\s+active|filled|closed|expired)"
    r"|\bthis\s+(?:job|position|posting)\s+is\s+no\s+longer\b"
    r"|\bno\s+longer\s+accepting\s+applications\b"
    r"|the\s+job\s+you\s+are\s+trying\s+to\s+apply\s+for\s+has\s+been\s+filled"
    r"|\bthis\s+job\s+has\s+closed\b"
    r"|\bposition\s+has\s+been\s+filled\b"
    r"|\bjob\s+expired\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# POSITIVE JD-shape signal — at least one section/affordance a real posting has.
# Necessary (not sufficient) for a deterministic CLEAN; the grounding + length
# checks supply the rest of the confidence.
# ---------------------------------------------------------------------------
_JD_POSITIVE_RE = re.compile(
    r"\bresponsibilities\b"
    r"|\bqualifications\b"
    r"|\brequirements\b"
    r"|what\s+you(?:'|’)?(?:ll|\s+will)\s+do"
    r"|what\s+we(?:'|’)?(?:re|\s+are)\s+looking\s+for"
    r"|about\s+(?:the|this)\s+role"
    r"|we(?:'|’)?(?:re|\s+are)\s+looking\s+for"
    r"|minimum\s+qualifications"
    r"|preferred\s+qualifications"
    r"|who\s+you\s+are"
    r"|your\s+(?:impact|role|responsibilities)"
    r"|in\s+this\s+role"
    r"|the\s+ideal\s+candidate"
    r"|what\s+you(?:'|’)?(?:ll|\s+will)\s+bring"
    r"|you\s+will\s+be\s+responsible"
    r"|key\s+(?:responsibilities|duties)"
    r"|essential\s+(?:functions|duties)"
    r"|job\s+(?:description|summary|duties)"
    r"|role\s+(?:overview|summary)"
    r"|day[\s-]to[\s-]day"
    r"|duties\s+include",
    re.IGNORECASE,
)


#: Trailing ellipsis/… (optionally followed by whitespace). A body ending like
#: this is almost always a search-result snippet, not a full JD.
_TRAILING_ELLIPSIS_RE = re.compile(r"(?:\.\.\.|…)\s*$")


#: ATX markdown section header line (``#{1,6} text``), optionally closed with
#: trailing ``#`` chars. Used by ``_has_empty_requirement_header`` to locate
#: section headers whose body — the text up to the next header or end-of-doc —
#: is empty. Setext (underlined) headers are deliberately not recognized: the
#: observed failure shape (issue #1952) is ATX-only, and Setext detection on
#: arbitrary JD bodies carries a higher false-positive risk (a line of ``----``
#: is also a horizontal rule / table border).
_ATX_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)


#: Requirement-bearing section-header vocabulary (issue #1952). A markdown
#: header whose text matches one of these keywords names a substantive
#: requirement / responsibility / qualification / skills / duties section —
#: the content that actually carries the job's demands. When such a header is
#: present but its body is EMPTY (immediately followed by another header or
#: end-of-document), the posting is structurally complete yet carries no
#: requirement content, and the gate must not call it CLEAN.
#:
#: Deliberately NARROW — only the concrete requirement-list headers. Empty
#: metadata headers (``## Company``, ``## Leadership Team``, ``## About Us``)
#: are common and benign across the corpus and MUST keep passing, so the
#: vocabulary excludes role-description / about / culture / benefits headers.
#: Mirrors the requirement-bearing subset of ``_JD_POSITIVE_RE`` plus ``skills``
#: (which ``_JD_POSITIVE_RE`` does not carry as a standalone marker but which
#: the issue names explicitly — ``## Skills, Knowledge & Expertise``).
_REQUIREMENT_HEADER_RE = re.compile(
    r"\b(?:"
    r"responsibilit(?:y|ies)"
    r"|qualification(?:s)?"
    r"|requirement(?:s)?"
    r"|skills?"
    r"|duties"
    r"|essential\s+functions"
    r")\b",
    re.IGNORECASE,
)


# --- jd_full completeness thresholds ---
# Minimum characters for a job description to be accepted as the full jd_full.
# A body below this floor, or ending in a trailing ellipsis/…, is treated as a
# truncated snippet and routed back to enrichment.
#
# PORT-SEAM: upstream these defaults and the resolver live in the app-level
# config module. The engine cannot import that layer (boundary: pure engine,
# no host config), and this contract module is the only engine consumer, so
# the resolver is inlined here verbatim.
DEFAULT_JD_FULL_MIN_CHARS = 200
DEFAULT_JD_FULL_REJECT_TRAILING_ELLIPSIS = True


def get_jd_full_thresholds(config: dict | None = None) -> tuple[int, bool]:
    """Resolve jd_full completeness thresholds from config.

    Every level of the lookup chain is normalized independently (``isinstance``
    + fallback), not just the outermost default (issue #37): a null/non-dict
    ``config``, ``enrichment``, or ``jd_full`` section, or a ``min_chars`` leaf
    that doesn't coerce to int, all degrade to the module defaults instead of
    raising. A single ``or {}`` at the outer level alone is not enough — a YAML
    section whose children are all commented out parses to ``None``, so
    ``{"enrichment": None}`` is a realistic shape, and a truthy non-dict
    ``enrichment`` (e.g. a stray scalar) would otherwise reach ``.get()`` on a
    non-dict and raise ``AttributeError``. This does not change the resolved
    value for any config shape that already resolved successfully — it only
    defines behavior for shapes that previously threw (``AttributeError`` /
    ``ValueError`` / ``TypeError``) inside the ``jd_content_reject``
    storage/ingest gate.

    Returns:
        (min_chars, reject_trailing_ellipsis) with safe defaults.
    """
    config = config if isinstance(config, dict) else {}
    enrichment_cfg = config.get("enrichment")
    enrichment_cfg = enrichment_cfg if isinstance(enrichment_cfg, dict) else {}
    jd_cfg = enrichment_cfg.get("jd_full")
    jd_cfg = jd_cfg if isinstance(jd_cfg, dict) else {}
    try:
        min_chars = int(jd_cfg.get("min_chars", DEFAULT_JD_FULL_MIN_CHARS))
    except (TypeError, ValueError):
        min_chars = DEFAULT_JD_FULL_MIN_CHARS
    reject_ellipsis = bool(
        jd_cfg.get("reject_trailing_ellipsis", DEFAULT_JD_FULL_REJECT_TRAILING_ELLIPSIS)
    )
    if min_chars < 1:
        min_chars = DEFAULT_JD_FULL_MIN_CHARS
    return min_chars, reject_ellipsis


def _has_empty_requirement_header(stripped: str) -> bool:
    """True if a requirement-bearing markdown header has an empty body.

    Issue #1952: a posting whose ``## Key Responsibilities`` and ``## Skills,
    Knowledge & Expertise`` sections are empty headers (immediately followed by
    another header or end-of-document) is structurally complete yet carries no
    requirement content. The boilerplate / DEI / benefits text that remains
    supplies length and title grounding, so the plain shape+grounded+substantial
    CLEAN test passes while the body has no actual demands to score against.

    This structural check scans ATX headers (``#{1,6} text``) whose text matches
    the requirement-bearing vocabulary (``_REQUIREMENT_HEADER_RE``) and checks
    whether the body between the header and the next header of EQUAL OR
    SHALLOWER depth (or end-of-document) is empty (whitespace-only). When at
    least one such empty requirement-bearing header is found, the body must not
    be CLEAN — it is routed to AMBIGUOUS for LLM adjudication by
    ``_ambiguous_widening_signal``.

    Depth-aware terminator (bug found in adversarial review of the original
    any-depth version): a header's body legitimately continues past a DEEPER
    sub-header — ``## Requirements`` followed by ``### Minimum Qualifications``
    / ``### Preferred Qualifications`` sub-sections is a populated,
    fully-formed section, not an empty one. trafilatura's markdown output
    (``html_extract.py``) preserves the source page's nested header hierarchy,
    so this shape is common on real postings. Terminating on ANY header
    (including deeper ones) misread that populated section as empty. Only a
    header at the SAME depth or SHALLOWER genuinely ends the section — deeper
    headers are the section's own subdivided content. This introduces no blind
    spot: a genuinely empty requirement sub-header (e.g. an ``### Skills``
    sub-section with nothing under it) still trips on its own pass through the
    outer loop, since it independently matches ``_REQUIREMENT_HEADER_RE`` with
    its own empty body.

    Pure and deterministic — no model call. Narrow by construction: only
    requirement-bearing headers gate (``## Company`` / ``## Leadership Team``
    do not match ``_REQUIREMENT_HEADER_RE``), and only EMPTY headers gate (a
    populated ``## Responsibilities`` section does not trip).
    """
    lines = stripped.split("\n")
    headers: list[tuple[int, int]] = []  # (line index, ATX depth)
    requirement_line_indices: list[int] = []
    for i, line in enumerate(lines):
        m = _ATX_HEADER_RE.match(line)
        if m:
            depth = len(m.group(1))
            headers.append((i, depth))
            if _REQUIREMENT_HEADER_RE.search(m.group(2)):
                requirement_line_indices.append(i)
    depth_by_line = dict(headers)
    for idx in requirement_line_indices:
        own_depth = depth_by_line[idx]
        # Body = lines up to the next header at equal-or-shallower depth (or
        # EOF). A deeper sub-header is section content, not a terminator.
        next_header = next(
            (j for j, d in headers if j > idx and d <= own_depth),
            len(lines),
        )
        body_text = "\n".join(lines[idx + 1 : next_header]).strip()
        if not body_text:
            return True
    return False


#: Generic organizational / legal / structural words that appear in company
#: names but carry no employer-identifying signal — "Highgate Hotels Corporate
#: Office" is grounded by "Highgate" / "Hotels", not by "Corporate" or
#: "Office". Filtered out of the company-stem presence check (issue #1813) so a
#: wrong-employer body that happens to mention "office" or "corporate" is not
#: silently let through as CLEAN. Mirrors the web-layer
#: ``enrichment_tiers._COMPANY_STOP_WORDS`` (kept local because this module is
#: PURE — see the module docstring — and must not import from
#: ``job_finder.web``); the union with ``TITLE_STOPWORDS`` (already applied by
#: ``significant_tokens``) covers seniority/level words that also leak into
#: company names ("co", "contract", "manager").
_COMPANY_STOPWORDS: frozenset[str] = frozenset(
    {
        "corp",
        "corporate",
        "corporation",
        "inc",
        "llc",
        "ltd",
        "company",
        "companies",
        "group",
        "holdings",
        "partners",
        "office",
        "offices",
        "enterprise",
        "enterprises",
        "global",
        "international",
        "national",
        "services",
        "service",
        "solutions",
        "technologies",
        "technology",
        "systems",
        "digital",
    }
)


#: Alphanumeric runs of *any* length — company tokenization needs the >= 2
#: floor (issue #1892), not the title tokenizer's >= 3 floor, so this module
#: derives its own runs and filters by length locally rather than reusing
#: ``_SIGNIFICANT_TOKEN_RE`` (which is calibrated for the *title* contract's
#: own false-positive budget and must not change).
_COMPANY_ALNUM_RUN_RE = re.compile(r"[a-z0-9]+")


def _company_acronym(company: str | None) -> str | None:
    """Punctuation-stripped acronym stem of *company*, or None when not needed.

    The whole name with non-alphanumeric characters removed, lowercased
    (``AT&T`` -> ``att``, ``C3 IoT`` -> ``c3iot``). Emitted only when the name
    has a token the 3-char title tokenizer would drop (an alphanumeric run
    shorter than 3) OR internal punctuation — i.e. the cases where the brand
    identity survives neither ``significant_tokens`` nor the >= 2 company
    floor. A purely generic name whose every run is >= 3 and has no
    punctuation (e.g. "The Corporate Group") yields no acronym, so the skip
    branch still fires for names with nothing distinctive to be absent.
    """
    if not company:
        return None
    low = company.lower()
    compacted = re.sub(r"[^a-z0-9]", "", low)
    if len(compacted) < 2:
        return None
    runs = _COMPANY_ALNUM_RUN_RE.findall(low)
    has_short_run = any(len(r) < 3 for r in runs)
    has_punct = bool(re.search(r"[^a-z0-9\s]", low))
    if not (has_short_run or has_punct):
        return None
    if compacted in TITLE_STOPWORDS or compacted in _COMPANY_STOPWORDS:
        return None
    return compacted


def _company_stems(company: str | None) -> list[str]:
    """Distinctive tokens of *company* for the cross-field presence check.

    Company-specific tokenization (issue #1892): alphanumeric runs of length
    >= 2 (the title tokenizer's 3-char floor drops employer-identifying
    initialisms like ``AT&T`` / ``C3`` / ``3M``), minus ``_COMPANY_STOPWORDS``
    and the shared ``TITLE_STOPWORDS``, PLUS a punctuation-stripped acronym
    stem (``AT&T`` -> ``att``) so initialisms survive tokenization.

    Returns ``[]`` when the name yields no employer-identifying stem (e.g. a
    generic "The Corporate Group"), in which case the company-absence check
    is skipped — absence cannot be asserted of a name with no distinctive
    stem, and the row stays eligible for CLEAN on the existing title-grounding
    + shape + length evidence.
    """
    if not company:
        return []
    low = company.lower()
    tokens = [t for t in _COMPANY_ALNUM_RUN_RE.findall(low) if len(t) >= 2]
    stems = [t for t in tokens if t not in _COMPANY_STOPWORDS and t not in TITLE_STOPWORDS]
    acronym = _company_acronym(company)
    if acronym and acronym not in stems:
        stems.append(acronym)
    return stems


#: Optional single non-alphanumeric separator between acronym characters, so
#: ``AT&T`` in the body matches the stem ``att`` (``\ba[^a-z0-9]?t[^a-z0-9]?t\b``)
#: while ``attend`` / ``attack`` do not (no trailing boundary after ``att``).
_ACRONYM_SEP = r"[^a-z0-9]?"


def _acronym_mentioned(acronym: str, body_lower: str) -> bool:
    """True if the punctuation-stripped *acronym* appears in *body_lower*.

    Matches the acronym with an optional single non-alphanumeric separator
    between each character and word boundaries at both ends. This lets a body
    that writes the brand with its original punctuation (``AT&T``) match the
    punctuation-stripped stem (``att``) while a body that merely contains a
    word starting with the same letters (``attend``) does not — the trailing
    ``\\b`` requires the acronym to end at a word boundary.
    """
    if not acronym or not body_lower:
        return False
    pattern = r"\b" + _ACRONYM_SEP.join(re.escape(c) for c in acronym) + r"\b"
    return re.search(pattern, body_lower) is not None


def _body_mentions_company(company: str | None, body_lower: str) -> bool:
    """True if *body_lower* mentions a distinctive stem of *company*.

    The company-absence cross-field presence check (issues #1813 / #1892).
    Short stems (< ``TITLE_STEM_LEN``) are matched with word boundaries so
    they cannot substring-match inside unrelated words (``iot`` vs
    ``patriot``); the punctuation-stripped acronym is matched with optional
    non-alphanumeric separators between its characters so ``AT&T`` in the
    body matches the stem ``att``. Stems >= ``TITLE_STEM_LEN`` keep the
    unanchored prefix-substring tolerance that holds the cross-field
    false-positive rate near zero.
    """
    acronym = _company_acronym(company)
    stems = _company_stems(company)
    if not stems:
        return False
    regular = [s for s in stems if s != acronym]
    if regular and body_mentions_any_stem(regular, body_lower, boundary_short=True):
        return True
    return bool(acronym) and _acronym_mentioned(acronym, body_lower)


# Leading non-whitespace characters that indicate a serialized JSON payload.
_JSON_START_CHARS: frozenset[str] = frozenset({"{", "["})

#: Keys (case- and separator-insensitive) that mark a JD-prose field inside a
#: leading JSON object — the escape hatch in ``_is_json_config_blob`` that
#: keeps a real JSON-served posting from being misclassified as a config
#: blob (public jobcannon#37). Widened beyond snake_case ``job_description``/
#: ``description`` to also recognize camelCase (``jobDescription``, which
#: normalizes the same as ``job_description`` once separators are stripped)
#: and the generic ``content`` key some ATS micro-sites use for the prose
#: body. Compared via ``_normalize_json_key`` so ``job_description``,
#: ``jobDescription``, and ``JobDescription`` all match the same alias.
_DESCRIPTION_KEY_ALIASES: frozenset[str] = frozenset({"jobdescription", "description", "content"})

#: Recursion depth cap for ``_has_prose`` — a JD payload nests a handful of
#: levels deep at most (e.g. ``{"description": {"raw": "...", "html": "..."}}``);
#: bounding the walk keeps it from following a pathological/adversarial
#: structure unboundedly.
_PROSE_SEARCH_MAX_DEPTH = 3

#: Minimum stripped length for a string leaf under a description-like key to
#: count as real JD prose. Without this floor, a config blob whose
#: description field carries an unrelated short string -- a format tag
#: (``{"description": {"format": "html", "text": ""}}``), a section label
#: (``{"content": {"header": "Careers", "footer": "..."}}``) -- trips the
#: escape hatch and a genuine config blob (REJECT) is misclassified as real
#: prose. Deliberately well below a full JD's length (``_CLEAN_MIN_CHARS``):
#: this only needs to rule out single-word/label-length filler, not validate
#: that the leaf is a complete posting on its own.
_PROSE_LEAF_MIN_CHARS = 40


def _normalize_json_key(key: object) -> str:
    """Fold a JSON key to a separator- and case-insensitive comparison form.

    ``job_description``, ``jobDescription``, and ``JobDescription`` all
    normalize to ``"jobdescription"`` so the description-key escape hatch
    recognizes the same field across snake_case and camelCase payloads.
    """
    if not isinstance(key, str):
        return ""
    return re.sub(r"[_\-\s]", "", key).lower()


def _has_prose(value: object, *, _depth: int = 0) -> bool:
    """True if *value* contains a string leaf that reads as real JD prose.

    Real JD prose is sometimes nested one level under a description-like key
    rather than being the string value directly (e.g. some ATS JSON payloads
    wrap the body as ``{"description": {"raw": "...", "html": "..."}}``, or
    a JSON-LD posting is wrapped in a bare array of block objects). Recurses
    a bounded depth into dicts and lists so that wrapping shape does not
    defeat the escape hatch — a leading array whose element carries a real
    ``description``/``content`` field with substantial text is real prose,
    not a config blob, even though the top-level JSON value is a list.

    A string leaf only counts once it clears ``_PROSE_LEAF_MIN_CHARS`` —
    otherwise a config blob's own short filler under a description-like key
    (a ``"format": "html"`` tag, a ``"header": "Careers"`` label) would trip
    the escape hatch and hide a genuine config blob from REJECT.
    """
    if _depth > _PROSE_SEARCH_MAX_DEPTH:
        return False
    if isinstance(value, str):
        return len(value.strip()) >= _PROSE_LEAF_MIN_CHARS
    if isinstance(value, dict):
        return any(_has_prose(v, _depth=_depth + 1) for v in value.values())
    if isinstance(value, list):
        return any(_has_prose(v, _depth=_depth + 1) for v in value)
    return False


def _dict_has_description_prose(obj: dict) -> bool:
    """True if *obj* has a description-like key whose value carries prose."""
    for key, val in obj.items():
        if _normalize_json_key(key) in _DESCRIPTION_KEY_ALIASES and _has_prose(val):
            return True
    return False


# ---------------------------------------------------------------------------
# AMBIGUOUS-widening signals (issue #1814). These do NOT join the REJECT set —
# they route a previously-CLEAN body to the AMBIGUOUS->LLM adjudication lane so
# the tight, high-precision REJECT set is unchanged and no new write-time
# rejection is introduced at set_jd_full. Each is a co-occurring-marker signal
# (single-marker bodies must not trip) calibrated against the observed
# non-posting captures that scored as real requisitions.
# ---------------------------------------------------------------------------

#: Career-explainer / SEO topic markers — a page whose title (or leading H1) is
#: a question/topic about a role rather than the role itself. Observed on
#: Randstad marketing/SEO pages (issue #1814): "what is a data scientist",
#: "salary of a data scientist", "data scientist profile page", "data scientist
#: career path", "how to become a data scientist". Checked against the job
#: title AND the body (the H1 may live in the body when the title field is just
#: the bare role name, e.g. ``randstad|data scientist``).
_EXPLAINER_TOPIC_RE = re.compile(
    r"\bwhat\s+is\s+(?:a|an)\b"
    r"|\bsalary\s+of\s+(?:a|an)\b"
    r"|\bprofile\s+page\b"
    r"|\bcareer\s+path\b"
    r"|\bhow\s+to\s+become\s+(?:a|an)\b",
    re.IGNORECASE,
)

#: Aggregate-salary language — national/BLS statistics cited instead of an
#: actual employer offer. The co-occurring marker that distinguishes an
#: explainer/SEO page from a real requisition: a real JD states a role's
#: duties/requirements and (when it lists comp) a specific offer, not a Bureau
#: of Labor Statistics national median.
_AGGREGATE_SALARY_RE = re.compile(
    r"\bnational\s+average\b"
    r"|\bBLS\b"
    r"|\bmedian\s+salary\s+in\s+the\b",
    re.IGNORECASE,
)


def _is_json_config_blob(jd_full: str, *, anchored: bool = True) -> bool:
    """Return True when *jd_full* is a dominating JSON object with no real prose.

    Eightfold/Netflix micro-sites sometimes serve the entire page body as a
    configuration JSON blob (theme, fonts, supported locales, and an empty
    ``job_description``). The blob clears the length gate while containing no
    actual job description, so the scorer sees a long "JD" with no signal.

    Detection is intentionally tight:
      * a JSON value (``{`` or ``[``) must be locatable — at the very start of
        the stripped body when ``anchored`` (the write-gate / REJECT path), or
        anywhere when ``anchored=False`` (the AMBIGUOUS-widening path, issue
        #1814: the blob may be preceded by a short markdown heading / nav
        markup wrapper);
      * the JSON value must parse and dominate the body (so a JD that merely
        contains an inline JSON block is not flagged — the prose dominates,
        not the JSON);
      * for an object, there must be no description-like key (``description``,
        ``jobDescription``/``job_description``, or ``content``, matched
        case-/separator-insensitively via ``_normalize_json_key``) carrying a
        string leaf of at least ``_PROSE_LEAF_MIN_CHARS`` — checked via
        ``_dict_has_description_prose``/``_has_prose``, which also recurses a
        bounded depth into nested dicts/lists (e.g.
        ``{"description": {"raw": "..."}}``). For a bare array, the same
        prose search runs over each element (a JSON-LD-style wrapper). A real
        posting served as JSON with a genuine, substantial prose description
        is left for the normal contract; a short label/tag under a
        description-like key does not count as prose and does not escape.

    ``anchored=True`` backs the deterministic REJECT in ``jd_content_reject``
    (a leading config blob). ``anchored=False`` backs the AMBIGUOUS signal in
    ``classify_jd_content`` for a config blob that is not the leading value —
    it never adds a REJECT member and never fires at the write gate.
    """
    stripped = jd_full.strip()
    if not stripped:
        return False
    if anchored:
        if stripped[0] not in _JSON_START_CHARS:
            return False
        start = 0
    else:
        # First JSON start char anywhere — a config blob may be preceded by a
        # short markdown heading / nav markup wrapper (issue #1814).
        start = min(
            (i for i in (stripped.find(c) for c in _JSON_START_CHARS) if i >= 0),
            default=-1,
        )
        if start < 0:
            return False
    try:
        obj, end = json.JSONDecoder().raw_decode(stripped, start)
    except json.JSONDecodeError:
        return False

    # The JSON value must be the bulk of the body. Anything after the first
    # complete value that is more than trailing whitespace is real prose, not a
    # pure serialized blob.
    if end < len(stripped) - 20:
        return False

    if not anchored:
        # The non-JSON leading prefix must be small relative to the JSON value
        # so a real JD whose body happens to contain an inline JSON block
        # (prose dominates, JSON is a minority) is not flagged. The JSON value
        # itself must be the large majority of the body.
        json_len = end - start
        if start > len(stripped) // 5 or json_len < (len(stripped) * 4) // 5:
            return False

    if not isinstance(obj, dict):
        if isinstance(obj, list):
            # A bare array wrapping JSON-LD-style posting objects (e.g.
            # ``[{"@type": "JobPosting", "description": "..."}]``) whose
            # element carries real JD prose under a description-like key ->
            # not a config blob, even though the top-level value is a list.
            return not any(
                isinstance(item, dict) and _dict_has_description_prose(item) for item in obj
            )
        # A leading scalar dominating the body is also not prose.
        return True

    # Real prose description (possibly camelCase-keyed or nested one level,
    # e.g. {"description": {"raw": "..."}}) -> not a config blob. Empty or
    # missing description in a leading JSON object -> config blob.
    return not _dict_has_description_prose(obj)


def _is_jd_truncated(
    jd_full: str | None,
    config: dict | None = None,
) -> tuple[str, str] | None:
    """Return (JD_TRUNCATED, signal) if the body is a truncated snippet.

    Two independent signals, both config-driven:
      * ``too_short`` — stripped length is below ``enrichment.jd_full.min_chars``.
      * ``trailing_ellipsis`` — body ends with ``...`` or ``…`` and
        ``enrichment.jd_full.reject_trailing_ellipsis`` is true.
    """
    if not jd_full:
        return None
    stripped = jd_full.strip()
    min_chars, reject_ellipsis = get_jd_full_thresholds(config)
    if len(stripped) < min_chars:
        return (JD_TRUNCATED, "too_short")
    if reject_ellipsis and _TRAILING_ELLIPSIS_RE.search(stripped):
        return (JD_TRUNCATED, "trailing_ellipsis")
    return None


class JdVerdict(Enum):
    """Outcome of the jd-content contract."""

    CLEAN = "clean"
    REJECT = "reject"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class JdContentResult:
    """A jd-content verdict plus the forensic signal that produced it.

    ``reason`` is the ``unresolved_reasons`` code (``jd_full_offsite`` /
    ``jd_full_expired``) when ``verdict is REJECT``, else None. ``signal`` is a
    short human-readable tag for logging and the dry-run (e.g. ``"head_block``
    ``_or_wiki"``, ``"title_zero_overlap"``, ``"shape+grounded"``).
    """

    verdict: JdVerdict
    reason: str | None
    signal: str


def jd_content_reject(
    jd_full: str | None,
    title: str | None = None,
    config: dict | None = None,
) -> tuple[str, str] | None:
    """Deterministic HIGH-precision reject check.

    Returns ``(reason_code, signal)`` if the body is provably not this job's
    posting, else None. The content-only signals (wiki / block / listing / 404 /
    expired / truncated) need no title and are safe to enforce at every write
    (``set_jd_full``). The title zero-overlap signal additionally requires
    *title* and a substantial body; it is the wired I-17 ``title_jd_mismatch``.

    Pure and side-effect free — the identical call backs the storage gate, the
    ingest gate, and the re-sweep.
    """
    if not jd_full:
        return None
    stripped = jd_full.strip()

    truncated = _is_jd_truncated(stripped, config)
    if truncated is not None:
        return truncated

    low = stripped.lower()
    head = low[:_HEAD_WINDOW]

    if _HEAD_BLOCK_RE.search(head):
        return (JD_OFFSITE, "head_block_or_wiki")
    if _LISTING_COUNT_RE.search(head):
        return (JD_OFFSITE, "listing_index")
    if _NOT_FOUND_RE.search(head):
        return (JD_OFFSITE, "not_found")
    if _CMS_PLACEHOLDER_RE.search(head):
        return (JD_OFFSITE, "cms_placeholder")
    if _NO_SEARCH_RESULTS_RE.search(head):
        return (JD_OFFSITE, "no_search_results")
    if _EXPIRED_RE.search(low):
        return (JD_EXPIRED, "expired_or_filled")

    # Serialized configuration / markup with no prose job description (issue
    # #1558). A LEADING JSON blob (Eightfold/Netflix micro-site config, empty
    # ``job_description``) fools the length gate; reject at the content layer
    # so the scorer never sees it. The un-anchored (non-leading) case is an
    # AMBIGUOUS-widening signal handled by ``classify_jd_content`` (issue
    # #1814) and deliberately NOT enforced here, so the REJECT set gains no
    # new member and no new write-time rejection is introduced.
    if _is_json_config_blob(stripped):
        return (JD_OFFSITE, "json_config_blob")

    # I-17 wired: a substantial body that shares ZERO of the title's content
    # stems is the silent wrong-page case (the body is about something else).
    if title and len(stripped) >= _XFIELD_MIN_CHARS:
        tokens = significant_tokens(title)
        if len(tokens) >= _XFIELD_MIN_TOKENS and not body_mentions_any_stem(tokens, low):
            return (JD_OFFSITE, "title_zero_overlap")
    return None


def _ambiguous_widening_signal(stripped: str, low: str, title: str | None) -> str | None:
    """Return an AMBIGUOUS-widening signal tag, or None (issue #1814).

    These signals route a body that the deterministic REJECT set lets through
    — and that the plain shape+grounded+substantial test would otherwise CLEAN
    — to the AMBIGUOUS->LLM adjudication lane. They NEVER join the REJECT set
    and never fire at the write gate (``jd_content_reject`` does not call this).
    Each is a co-occurring-marker signal so a single-marker body does not trip.

    Returns one of ``"json_config_blob_unanchored"``, ``"listing_index"``,
    ``"career_explainer_seo"``, ``"empty_requirements_header"`` — or None when
    no widening signal fires.
    """
    # Issue #1952: a structurally-complete posting whose requirement-bearing
    # section headers (## Key Responsibilities / ## Skills / ## Qualifications
    # / etc.) are EMPTY — immediately followed by another header at
    # equal-or-shallower depth, or end-of-document. (A DEEPER sub-header, e.g.
    # ``### Minimum Qualifications`` under ``## Requirements``, is the
    # section's own content and does not end it — see
    # ``_has_empty_requirement_header``.) The remaining boilerplate / DEI /
    # benefits text supplies length and title grounding, so the plain
    # shape+grounded+substantial CLEAN test passes while the body has no
    # actual demands to score against. Route to AMBIGUOUS for LLM
    # adjudication; never REJECT (the body IS a posting page, just one whose
    # content did not render / was never filled).
    if _has_empty_requirement_header(stripped):
        return "empty_requirements_header"

    # A dominating JSON config blob that is NOT the leading value (a leading
    # blob was already caught as REJECT by jd_content_reject). The blob may be
    # preceded by a short markdown heading / nav markup wrapper.
    if _is_json_config_blob(stripped, anchored=False):
        return "json_config_blob_unanchored"

    # A job-listing INDEX captured as a posting. ``_LISTING_COUNT_RE`` ("jobs
    # in <place>") and ``_LISTING_RESULTS_RE`` ("<count> results") are both
    # evaluated against the WHOLE body here — the head-only "jobs in" REJECT
    # already fired in jd_content_reject, so this catches the same structural
    # tell beyond the head window and the "results" phrasing anywhere. A real
    # JD does not contain a result-count-plus-dozens-of-titles block anywhere.
    if _LISTING_COUNT_RE.search(low) or _LISTING_RESULTS_RE.search(low):
        return "listing_index"

    # Career-explainer / SEO page: an explainer-shaped topic (in the title OR
    # the body) co-occurring with aggregate-salary language. BOTH markers are
    # required — a single-marker body must not trip (a real JD can mention a
    # national average, and a real JD's body can quote a role's title in a
    # "what is a ..." FAQ snippet, but not both at once).
    if _EXPLAINER_TOPIC_RE.search(title or "") or _EXPLAINER_TOPIC_RE.search(low):
        if _AGGREGATE_SALARY_RE.search(low):
            return "career_explainer_seo"

    return None


def classify_jd_content(
    jd_full: str | None,
    title: str | None = None,
    company: str | None = None,
    config: dict | None = None,
) -> JdContentResult:
    """Full three-way jd-content verdict (REJECT / CLEAN / AMBIGUOUS).

    Used by the fetch-path gate and the versioned re-sweep, which both hold the
    job's title and company for cross-field grounding. The storage/ingest gates
    use the cheaper ``jd_content_reject`` directly.

    CLEAN requires ALL of: a positive JD-shape signal, grounding in the job's own
    TITLE (a content stem of the title appears in the body), and a substantial
    length. Title grounding (not company) is deliberate: a company *About*/
    marketing page is grounded by the company name yet is not the posting, so
    company-only grounding is treated as weak evidence and routed to the LLM. When
    no title is available, the company name is the only fallback. Anything short
    of CLEAN — but not a deterministic REJECT — is AMBIGUOUS for the LLM tie-breaker.

    ``company`` additionally feeds a negative cross-field counter-signal
    (issue #1813): a substantial, JD-shaped, title-grounded body that contains
    NO distinctive stem of the listing's own company name is downgraded from
    CLEAN to AMBIGUOUS (signal ``company_absent``) for LLM adjudication. This
    catches the wrong-employer contamination case — a genuine Northrop Grumman
    requisition attached to a Highgate Hotels listing shares the generic "Data
    Scientist" title stems, so it passes every title-grounded CLEAN gate while
    being about a different employer. It is deliberately NOT a hard REJECT
    (the module's discipline, lines 95-97: AMBIGUOUS is where uncertainty
    goes); company *absence* is the cheap deterministic half, and a body that
    names a *different* employer is left for the LLM to weigh. A company name
    with no distinctive stem (e.g. "The Corporate Group") skips the check —
    absence cannot be asserted of a name with nothing to be absent.

    AMBIGUOUS-widening signals (issues #1814 / #1952) run AFTER the deterministic
    REJECT and BEFORE the CLEAN test. A body that the tight REJECT set lets
    through but that carries a non-posting structural tell (a dominating
    non-leading JSON config blob, a listing-index result-count block, a
    career-explainer/SEO page, or a structurally-complete posting whose
    requirement-bearing section headers are empty) is routed to AMBIGUOUS even
    when it would otherwise satisfy shape+grounded+substantial — so the LLM
    adjudicator, not the scorer, decides. These signals add no REJECT member and
    introduce no write-time rejection.

    The company stem derivation is company-specific (issue #1892): it accepts
    alphanumeric runs of length >= 2 (not the title tokenizer's >= 3 floor)
    and additionally emits a punctuation-stripped acronym (``AT&T`` -> ``att``)
    so initialisms survive tokenization, and short stems are matched with word
    boundaries so they cannot substring-match inside unrelated words (``iot``
    vs ``patriot``). The title contract's ``TITLE_STEM_LEN`` /
    ``_SIGNIFICANT_TOKEN_RE`` are unchanged — they have their own calibrated
    false-positive budget.
    """
    rej = jd_content_reject(jd_full, title, config)
    if rej is not None:
        return JdContentResult(JdVerdict.REJECT, rej[0], rej[1])
    if not jd_full:
        return JdContentResult(JdVerdict.AMBIGUOUS, None, "empty")

    stripped = jd_full.strip()
    low = stripped.lower()

    widen = _ambiguous_widening_signal(stripped, low, title)
    if widen is not None:
        return JdContentResult(JdVerdict.AMBIGUOUS, None, widen)

    has_shape = bool(_JD_POSITIVE_RE.search(low))
    substantial = len(stripped) >= _CLEAN_MIN_CHARS

    ground_tokens = significant_tokens(title) if title else significant_tokens(company or "")
    grounded = body_mentions_any_stem(ground_tokens, low)

    if has_shape and grounded and substantial:
        # Cross-field counter-signal (issues #1813 / #1892): a substantial,
        # JD-shaped, title-grounded body that contains NO distinctive stem of
        # the listing's own company is the wrong-employer contamination case.
        # Downgrade to AMBIGUOUS for the LLM tie-breaker — never a hard REJECT
        # (the module's discipline: AMBIGUOUS is where uncertainty goes).
        # ``_body_mentions_company`` derives its own stems (alphanumeric runs
        # >= 2 + a punctuation-stripped acronym) instead of reusing the title
        # tokenizer's 3-char floor, so brands like AT&T / C3 / 3M are
        # representable (#1892); short stems match with word boundaries so
        # they cannot substring-match inside unrelated words.
        if _company_stems(company) and not _body_mentions_company(company, low):
            return JdContentResult(JdVerdict.AMBIGUOUS, None, "company_absent")
        return JdContentResult(JdVerdict.CLEAN, None, "shape+grounded")
    return JdContentResult(JdVerdict.AMBIGUOUS, None, "needs_adjudication")

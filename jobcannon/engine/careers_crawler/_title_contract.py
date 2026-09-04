"""Positive title contract — the single definition of "a clean job title".

WHY THIS EXISTS (the architectural inversion)
---------------------------------------------
Historically title hygiene was a *denylist of bad shapes* (``is_metadata_blob``,
``clean_title``'s suffix regexes, I-07..I-15): a title that did not match a known-bad
pattern was treated as **clean**, scored, and surfaced as ``classification='apply'``.
That is *fail-open*: every new scrape layout glues card text a new way, so each new
junk shape needs a new regex — unbounded, point-source whack-a-mole.

This module inverts the default to **fail-closed**: a title is board-eligible only if
it *positively* satisfies the contract. "Unknown shape" ⇒ quarantine, never clean.
``title_contract_violation`` is the single predicate; it is enforced at the one
ingestion chokepoint (``ParsedJob.from_job``). In the private source, a standing
re-sweep (``_run_title_resweep_if_stale`` in ``migrations/_post_hooks.py``, not
carried into this port) re-applied it retroactively to every existing row so a
rule improvement healed the whole corpus at once.

THE SAFE RULE SET (empirically bounded, NOT a growing blocklist)
----------------------------------------------------------------
The quarantine rules below were validated against the live 13,672-row corpus: together
they flag exactly the structural-junk population (date/CTA/arrow/control) with **zero**
observed legitimate casualties. Rules the corpus proved net-negative are DELIBERATELY
EXCLUDED and must NOT be added here as hard quarantine rules:

  * non-ASCII ratio  → kills legitimate CJK titles (Coupang/Chinese-pharma/Japanese-legal)
  * pipe count >= 2  → kills "Role | Team | Remote" titles the user wants
  * bare length      → ~70% false-positive (verbose government/civil-service titles)
  * lone 4-digit year → appears in 32 legitimate intern/cohort/contract titles

Those may only ever be used as *soft suspicion signals* (e.g. routing to review), never
as deterministic drops. Keep this list small and high-precision on purpose.

REPAIR vs QUARANTINE
--------------------
The same regexes power a deterministic *repair* in ``_title_filters._strip_trailing_card_junk``:
a trailing ``<Mon D, YYYY> View Job ->`` tail is stripped so the real title is recovered
("Data Scientist / IA Engineer Jun 15, 2026 View Job ->" -> "Data Scientist / IA Engineer").
The contract is the safety net for whatever repair cannot cleanly salvage.
"""

from __future__ import annotations

import re

from jobcannon.engine.normalizers import body_mentions_any_stem, significant_tokens

# ---------------------------------------------------------------------------
# Hygiene version watermark (D-8: derived/validated values are versioned).
#
# Mirrors normalizers.NORMALIZER_VERSION. BUMP whenever the rules below change
# such that an already-stored title could newly pass or newly fail the contract.
# Bumping re-arms _run_title_resweep_if_stale (migrations/_post_hooks.py), which
# re-cleans + re-validates every row under the new version on next startup.
#
# v2: added _RELATIVE_POSTED_RE — the relative "Posted N <unit> ago" listing-tile
# chrome (Phenom careers pages glue it onto the title with no separator). This is
# both a quarantine signal here and a repair anchor in _title_filters; the bump
# re-sweeps the legacy careers_crawl corruption (e.g. company_id=217 Microsoft).
#
# v3: added _LEADING_RELATIVE_AGE_RE — abbreviated "18h ago"/"1d ago" posting-age
# chrome glued to the FRONT of the title (no "Posted" prefix, unlike
# _RELATIVE_POSTED_RE). Anchors the TryApplyNow aggregator listing-tile
# corruption (company_id=4925: PwC/Amazon/Meta/NOAA/Chase/PNNL/Diva postings all
# scraped under the wrong company). The bump re-sweeps that legacy corpus.
#
# v4: Issue #2017 — the re-sweep now also DETECTS + REPORTS rows whose title
# carries a self-duplicated trailing comma-delimited segment (e.g. Amazon's
# PRIMAS title where the subtitle appeared twice). The detection is reporting-
# only (no collapse / no re-key — #1866 owns the standing re-key question);
# new rows are collapsed at the ingestion write boundary by
# collapse_duplicated_suffix in ParsedJob.from_job. The bump re-arms the
# re-sweep so existing affected rows are enumerated in the runs row on the
# next startup.
# ---------------------------------------------------------------------------
TITLE_HYGIENE_VERSION: int = 4

# Reason codes emitted into jobs.unresolved_reasons (the m078 quarantine surface).
TITLE_INVALID_SHAPE: str = "title_invalid_shape"
TITLE_JD_MISMATCH: str = "title_jd_mismatch"
TITLE_NON_POSTING: str = "title_non_posting"

#: All title-hygiene reason codes (the ones the re-sweep owns + recomputes).
TITLE_REASON_CODES: frozenset[str] = frozenset(
    {TITLE_INVALID_SHAPE, TITLE_JD_MISMATCH, TITLE_NON_POSTING}
)

# ---------------------------------------------------------------------------
# The four safe quarantine signals (also reused as the repair-strip anchors).
# ---------------------------------------------------------------------------

#: Full date token: month-name + day + 4-digit year, OR ISO 8601 date.
#: REQUIRES a month name (or ISO) — a lone 4-digit year is intentionally NOT a
#: date token, because "[Summer 2026] Data Scientist Intern" etc. are legitimate.
_DATE_TOKEN_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)

#: Relative posting-age chrome: "Posted N <unit> ago" ("Posted 15 days ago",
#: "Posted 3 hours ago", "Posted 30+ days ago"). Distinct from _DATE_TOKEN_RE,
#: which requires an absolute month name / ISO date — this is the relative form
#: that Phenom / Workday listing tiles glue onto the title with no separator
#: ("...Multiple LocationsPosted 15 days ago"). NO leading word boundary on
#: "Posted": the location often glues directly into it ("LocationsPosted"), so a
#: \b would never anchor. The "\d+ ... ago" core is what bounds the false-positive
#: rate to ~zero — a legitimate recruiter tag like "(Posted by SAM)" has neither a
#: digit nor "ago", so it never matches.
_RELATIVE_POSTED_RE = re.compile(
    r"Posted\s+\d+\+?\s+(?:second|minute|hour|day|week|month|year)s?\s+ago\b",
    re.IGNORECASE,
)

#: Leading relative-age chrome glued at the very START of the title with no
#: "Posted" prefix and an abbreviated unit letter: "18h ago", "1d ago", "3w ago"
#: (TryApplyNow and similar listing-tile aggregators render the relative post
#: age as the first token, directly ahead of the role name, with no separator).
#: Distinct from _RELATIVE_POSTED_RE, which requires the literal word "Posted"
#: and matches at the END of the title — this is the abbreviated h/d/w form
#: anchored at the START. No real job title begins with a relative time delta,
#: so anchoring to ``^`` keeps this at effectively zero false-positive risk
#: (verified: zero matches among 21,324 other live-corpus titles).
_LEADING_RELATIVE_AGE_RE = re.compile(
    r"^\d+\+?\s*[hdw]\s+ago\b",
    re.IGNORECASE,
)

#: UI-affordance / call-to-action phrases that only appear when card chrome was
#: concatenated into the title. None of these is a substring of a real title
#: (the "view"/"apply" anchors require their following CTA word).
#:
#: NOTE: this pattern doubles as the repair anchor in ``_CARD_JUNK_ANCHOR_RE``
#: (``_title_filters``), which strips from the FIRST match to end-of-string.
#: That assumes the CTA is TRAILING chrome. A phrase that can appear before or
#: in the middle of the real title (e.g. TryApplyNow's "Be an Early Applicant"
#: badge, which precedes the role name) must NOT be added here — it would
#: truncate the repair down to whatever precedes it. Such phrases belong in
#: their own quarantine-only pattern instead (see _LEADING_RELATIVE_AGE_RE).
_CTA_RE = re.compile(
    r"\b(?:"
    r"view\s+job|view\s+details|view\s+posting|view\s+role|"
    r"apply\s+now|apply\s+today|apply\s+here|quick\s+apply|easy\s+apply|"
    r"save\s+job|saved\s+job|"
    r"learn\s+more|see\s+details|read\s+more|show\s+more|"
    r"please\s+wait|load\s+more"
    r")\b",
    re.IGNORECASE,
)

#: Trailing arrow / chevron glyph (or ASCII "->") at end of string — a link
#: affordance, never part of a title.
_TRAILING_ARROW_RE = re.compile(r"(?:[→➔➙➜➤⟶⇒»›▶▸⮕]|-{1,2}>)\s*$")

#: Control / layout whitespace that should never live inside a stored title.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\n\r\t]")

#: Non-posting funnel entries: a clean-LOOKING title that is not a single
#: applyable posting — talent networks/pools, general/speculative applications,
#: "future opportunities" landing entries. The sibling of the I-14 listing tile
#: (a category landing page), but routed to QUARANTINE not hard-drop because the
#: occasional one is a real evergreen req (e.g. a named "Talent Pool - <role>"):
#: a reviewer approves it from /admin/review. Phrases are specific two-word
#: combinations that do not occur in real titles ("Talent Acquisition Specialist"
#: does NOT match "talent network"). Keep this list tight and quarantine-routed —
#: it is a bounded category, NOT an open-ended junk-shape blocklist.
_NON_POSTING_RE = re.compile(
    r"\b(?:"
    r"talent\s+(?:network|community|pool|connection|pipeline)"
    r"|join\s+(?:our|the)\s+talent"
    r"|general\s+application"
    r"|open\s+application"
    r"|speculative\s+application"
    r"|spontaneous\s+application"
    r"|unsolicited\s+application"
    r"|future\s+(?:opportunities|openings|vacancies)"
    r"|expression\s+of\s+interest"
    r"|candidate\s+pool"
    r"|stay\s+connected"
    r")\b",
    re.IGNORECASE,
)


def title_contract_violation(title: str | None) -> str | None:
    """Return the quarantine reason code if *title* violates the contract, else None.

    The single, high-precision title predicate. None means the title positively
    satisfies the contract; a non-None return is a reason code suitable for
    ``unresolved_reasons`` (``title_invalid_shape`` for a mangled shape, or
    ``title_non_posting`` for a clean-looking non-applyable funnel entry).
    Deterministic and side-effect free — the identical call decides both the ingest
    gate (``ParsedJob.from_job``) and the retroactive sweep.

    Run this on the CLEANED title (post ``clean_title``), so deterministic repair
    has already had its chance and only genuine residue is quarantined.
    """
    if not title or not title.strip():
        return TITLE_INVALID_SHAPE
    if _CONTROL_RE.search(title):
        return TITLE_INVALID_SHAPE
    if _DATE_TOKEN_RE.search(title):
        return TITLE_INVALID_SHAPE
    if _RELATIVE_POSTED_RE.search(title):
        return TITLE_INVALID_SHAPE
    if _LEADING_RELATIVE_AGE_RE.search(title):
        return TITLE_INVALID_SHAPE
    if _CTA_RE.search(title):
        return TITLE_INVALID_SHAPE
    if _TRAILING_ARROW_RE.search(title):
        return TITLE_INVALID_SHAPE
    if _NON_POSTING_RE.search(title):
        return TITLE_NON_POSTING
    return None


# ---------------------------------------------------------------------------
# Title <-> JD cross-validation (the silent-wrong-title defense).
#
# A title can be perfectly clean-LOOKING yet WRONG — e.g. extraction grabbed a
# section heading ("Engineering Roles") instead of the posting title. Such a row
# passes every shape rule. The only available signal is the job body: a real
# title's significant tokens should appear in its own JD. This is intentionally
# HIGH-PRECISION (conservative) so it never false-quarantines a real job:
#   * only runs when jd_full is substantial (>= _JD_MIN_CHARS)
#   * only fires on ZERO overlap of significant title tokens with the JD
#   * a quarantine (recoverable via /admin/review), never a hard drop
# ---------------------------------------------------------------------------

_JD_MIN_CHARS = 300

# Significant-token tooling now lives in jobcannon.engine.normalizers (shared with the
# jd-content contract) — see the import at the top of this module. The stopword
# set + tokenizer used to be duplicated here; they were extracted so both
# cross-field contracts share one definition.

#: Minimum significant title tokens required before a JD cross-check can fire.
#: A single-content-word title ("Staff UX Researcher" -> just "researcher") is too
#: easy to false-flag when the JD phrases it differently ("UX research"), so we
#: require >= 2 content words and treat their AGREEMENT as the confidence signal.
_JD_MIN_TOKENS = 2

#: Stem-prefix length for token matching: compare the first N chars so "researcher"
#: matches a JD that says "research", "analytics" matches "analytic", etc. Tolerating
#: morphological variants is what keeps the false-positive rate near zero (the
#: dry-run gate showed exact-substring matching false-flagged 574 real jobs).
_JD_STEM_LEN = 5


def title_jd_mismatch(title: str | None, jd_full: str | None) -> bool:
    """Return True only when a substantial JD shares ZERO of the title's content stems.

    HIGH precision by construction (see module note + the dry-run finding): returns
    False unless ALL of these hold, so it can only ever flag the clear silent-wrong-
    title case where the stored title plainly does not belong to its own body text:
      * the JD is substantial (>= _JD_MIN_CHARS), AND
      * the title has >= _JD_MIN_TOKENS significant (non-stopword) tokens, AND
      * NONE of those tokens' stem prefixes appear anywhere in the JD body.
    """
    if not title or not jd_full or len(jd_full) < _JD_MIN_CHARS:
        return False
    tokens = significant_tokens(title)
    if len(tokens) < _JD_MIN_TOKENS:
        return False
    return not body_mentions_any_stem(tokens, jd_full.lower(), _JD_STEM_LEN)

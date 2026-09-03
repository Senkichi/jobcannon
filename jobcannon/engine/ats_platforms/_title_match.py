# PORTED from job_finder/web/ats_platforms/_title_match.py @ 7788dcfa71b4ad05f7015c357cf57c67fa436e32 (private job-cannon). Ledger L-0015.
"""Title normalization + word-boundary matching shared across ATS platforms.

Recruiters use shorthand ("Sr DS", "ML Eng", "PM, Growth") that the old
verbatim-substring matcher missed entirely. ``_normalize_title`` expands the
common abbreviations BEFORE the keyword check, so a config keyword of
"Data Scientist" hits both "Senior Data Scientist" and "Sr DS".

Extracted from ``ats_platforms.py`` during the H3 package promotion
(2026-05-28). Module-level state — ``_TITLE_EXPANSIONS``, ``_PUNCT_RUN``,
``_WS_RUN`` — is intentionally kept private to the package; the package
``__init__`` re-exports the callables for external consumers (~30 import
sites). ``TITLE_MATCH_VERSION`` names the current tier-3 fallback
semantics (bumped to 2 by WI-10 / #1834 when the ordered matcher became
an order-insensitive token-set match).

All-caps abbreviations (EM, DS, DA, …) are expanded *case-sensitively*
so that a lowercased "em" (Portuguese preposition) or "da" (Italian / Da
Nang) is not mistaken for the recruiter shorthand (#1861).
"""

from __future__ import annotations

import re
from functools import lru_cache

# Order does not matter -- patterns are non-overlapping. Add new entries
# here when a new abbreviation shows up in a posting you would have wanted
# to catch.
#
# Each entry is (compiled regex, replacement). Regexes use \b word boundaries
# so "DS" does not match "DSP" or "SDS"; the replacement is the canonical
# spelled-out form lowercased once at module load.
#
# The list is split into two case-sensitivity classes (#1861):
#
# * **All-caps abbreviations** (EM, DS, DA, DE, PM, …) are compiled
#   *case-sensitively*. A lowercased "em" is the Portuguese preposition "in",
#   "da" is Italian "from" / the Vietnamese city Da Nang, "de" is
#   Portuguese/Spanish "of", "ds" is Polish "do spraw" (regarding), etc.
#   Case-insensitive expansion of these injected spurious tokens ("manager"
#   from "em", "data analyst" from "da") into non-English titles, producing
#   false-positive token-set matches.
# * **Period / title-case abbreviations** (Sr., Jr., Mgr., Ops, Admin, …)
#   remain case-insensitive: their lowercased forms do not collide with
#   common non-English words.
_ALL_CAPS_ABBREVIATIONS: list[tuple[str, str]] = [
    (r"VP\b", "Vice President"),
    (r"DS\b", "Data Scientist"),
    (r"DA\b", "Data Analyst"),
    (r"DE\b", "Data Engineer"),
    (r"PM\b", "Product Manager"),
    (r"TPM\b", "Technical Program Manager"),
    (r"EM\b", "Engineering Manager"),
    (r"MLE\b", "Machine Learning Engineer"),
    (r"ML\b", "Machine Learning"),
    (r"AI\b", "Artificial Intelligence"),
    (r"SRE\b", "Site Reliability Engineer"),
    (r"SWE\b", "Software Engineer"),
    (r"SE\b", "Software Engineer"),
    (r"IC\b", "Individual Contributor"),
    (r"QA\b", "Quality Assurance"),
    (r"UX\b", "User Experience"),
    (r"UI\b", "User Interface"),
]

_CI_ABBREVIATIONS: list[tuple[str, str]] = [
    (r"Sr\.?", "Senior"),
    (r"Jr\.?", "Junior"),
    (r"Mgr\.?", "Manager"),
    (r"Mgmt\.?", "Management"),
    (r"Eng\.?", "Engineer"),
    (r"Engr\.?", "Engineer"),
    (r"Dev\.?", "Developer"),
    (r"Arch\.?", "Architect"),
    (r"Ops\b", "Operations"),
    (r"Admin\b", "Administrator"),
    (r"Dir\.?", "Director"),
]

_TITLE_EXPANSIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{abbr}\b"), full.lower()) for abbr, full in _ALL_CAPS_ABBREVIATIONS
] + [(re.compile(rf"\b{abbr}\b", re.IGNORECASE), full.lower()) for abbr, full in _CI_ABBREVIATIONS]


_PUNCT_RUN = re.compile(r"[^\w\s]+")
_WS_RUN = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    """Lowercase, expand common recruiter abbreviations, normalize whitespace.

    After abbreviation expansion ("Sr." -> "Senior"), the original
    punctuation may strand inside a multi-word keyword's match window
    -- "Sr. DS" expands to "Senior. Data Scientist", which a literal-space
    regex for "Senior Data Scientist" will not match. We therefore collapse
    runs of punctuation to a single space and runs of whitespace to one
    space before lowercasing.

    Idempotent: applying twice produces the same output as applying once.
    The expansions never produce abbreviations the same regexes would
    re-match, and the whitespace collapse is already at a fixed point.
    """
    out = title
    for pat, sub in _TITLE_EXPANSIONS:
        out = pat.sub(sub, out)
    out = _PUNCT_RUN.sub(" ", out)
    out = _WS_RUN.sub(" ", out).strip()
    return out.lower()


@lru_cache(maxsize=512)
def _compile_word_boundary(keyword: str) -> re.Pattern:
    r"""Return a compiled \bkeyword\b regex (case-insensitive).

    Cached because the same target_titles list is reused across every job
    in a scan -- a single scan of 850 companies x ~50 jobs each compiles
    each keyword's pattern once, not 42,500 times.

    The keyword is normalized through _normalize_title first so that a
    config entry of "Sr Data Scientist" gets matched as
    "senior data scientist" -- consistent with how candidate titles are
    matched. re.escape() is applied AFTER normalization to defang any
    regex metacharacters that survive normalization.
    """
    norm = _normalize_title(keyword)
    return re.compile(rf"\b{re.escape(norm)}\b", re.IGNORECASE)


TITLE_MATCH_VERSION = 2
"""Semantic version of the tier-3 title-matching fallback.

- **1** — ordered words with a bounded intervening-token gap
  (``_ordered_words_match``): target words had to appear *in order*.
- **2** — order-insensitive token-set subset (``_token_set_match``,
  WI-10 / #1834): target words may appear in any order, any gap.

The value is **persisted nowhere** — the title filter runs per-scan
against the live ``target_titles``, so there is no stored score to
re-version. This constant exists purely so callers and tests can assert
which matcher semantics are in force."""


def _token_set_match(target_norm: str, candidate_norm: str) -> bool:
    """Return True if every word of ``target_norm`` appears in ``candidate_norm``.

    Both inputs are already-normalised titles (lowercased, abbreviations
    expanded, punctuation/whitespace collapsed). Matching is *order-free*
    and *gap-free*: the target's words may appear anywhere in the
    candidate, in any order, with any number of intervening tokens.
    Seniority/level tokens ("senior", "staff", "lead", "ii") are ordinary
    required words — neither stripped nor treated specially, exactly as in
    the strict tier.

    Each target word must equal a *complete* candidate token — set
    membership is whole-token, so this preserves the word-boundary
    semantic of the strict matcher ("data" still does not match
    "database", "lead" still does not match "leadership").

    This is the tier-3 fallback used by ``_title_matches`` only when the
    strict phrase match fails. It replaces the former ordered/bounded-gap
    matcher (``_ordered_words_match``, TITLE_MATCH_VERSION 1): an
    order-insensitive subset test lets a configured phrase like
    "Analytics Lead" match a scrambled posting title such as
    "Lead, Advanced Analytics" — the false-negative class documented in
    the 2026-08-22 ATS pipeline review (D-4, #1834) — without
    re-introducing substring sloppiness (whole-token equality is kept).
    """
    target_words = target_norm.split()
    if not target_words:
        return False
    candidate_tokens = set(candidate_norm.split())
    return all(word in candidate_tokens for word in target_words)


def _title_matches(title: str, target_titles: list[str], exclusions: list[str]) -> bool:
    r"""Return True if title matches any target keyword and no exclusion keyword.

    Three-stage matcher:

    1. **Normalize**: both the candidate title and each keyword are passed
       through _normalize_title, which lowercases and expands common
       abbreviations (Sr -> Senior, DS -> Data Scientist, MLE -> Machine
       Learning Engineer, etc.). This lets "Sr DS, Growth" match a
       configured keyword of "Senior Data Scientist".

    2. **Word-boundary phrase match**: \bkeyword\b regex instead of plain
       substring. Prevents short keywords like "Lead" from matching inside
       "Leadership" or "Misleading", and short ones like "Data" from
       matching "Database". This is the strict tier — it requires the
       target's words to appear contiguously in the candidate.

    3. **Token-set fallback** (inclusion only): when the strict phrase
       check fails, fall back to checking that *every* word of the target
       appears somewhere in the candidate's token set — order-free and
       gap-free (``_token_set_match``, TITLE_MATCH_VERSION 2). This lets
       "Senior Manager, Analytics" match "Senior Manager, Data Analytics"
       (NVIDIA-style narrow-phrase miss) *and* "Analytics Lead" match a
       scrambled "Lead, Advanced Analytics" (WI-10 / #1834), without
       re-introducing substring sloppiness — whole-token equality still
       means "data" ≠ "database". Exclusions still use strict phrase
       match so a sloppier exclude doesn't over-filter.

    Args:
        title: Job title to evaluate.
        target_titles: Keywords; title must match at least one (OR
            semantics). If empty, all titles pass -- but configs reaching
            this code path with an empty list have bypassed the
            config.validate_target_titles guard.
        exclusions: Keywords; title must match none (AND NOT semantics).
            Exclusion wins over inclusion.

    Returns:
        True if title should be included in results, False if filtered out.
    """
    normalized = _normalize_title(title)

    if target_titles:
        # Strict tier first — fast and unambiguous.
        if not any(_compile_word_boundary(t).search(normalized) for t in target_titles):
            # Token-set fallback — order-free subset match, rescues
            # scrambled/qualified phrases (WI-10 / #1834).
            if not any(_token_set_match(_normalize_title(t), normalized) for t in target_titles):
                return False

    return not any(_compile_word_boundary(ex).search(normalized) for ex in exclusions)

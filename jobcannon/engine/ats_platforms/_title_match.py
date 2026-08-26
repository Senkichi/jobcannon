"""Title normalization + word-boundary matching shared across ATS platforms.

Recruiters use shorthand ("Sr DS", "ML Eng", "PM, Growth") that the old
verbatim-substring matcher missed entirely. ``_normalize_title`` expands the
common abbreviations BEFORE the keyword check, so a config keyword of
"Data Scientist" hits both "Senior Data Scientist" and "Sr DS".

Extracted from ``ats_platforms.py`` during the H3 package promotion
(2026-05-28). Module-level state — ``_TITLE_EXPANSIONS``, ``_PUNCT_RUN``,
``_WS_RUN``, ``_MAX_TARGET_GAP`` — is intentionally kept private to the
package; the package ``__init__`` re-exports the callables for external
consumers (~30 import sites).
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
_TITLE_EXPANSIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{abbr}\b", re.IGNORECASE), full.lower())
    for abbr, full in [
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
]


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


_MAX_TARGET_GAP = 2
"""Maximum tokens allowed between consecutive target words in the
ordered-fallback matcher. Tuned so "Senior Manager, Analytics" matches
"Senior Manager, Data Analytics" (1 intervening token = "data") while
"Senior Data Analyst" still rejects "Senior Marketing Manager — Data
Analyst Hiring Help" (4 intervening tokens between "senior" and
"data")."""


def _ordered_words_match(target_norm: str, candidate_norm: str) -> bool:
    """Return True if target's words appear in order in candidate.

    Tokens are split on whitespace from the already-normalised forms.
    Up to ``_MAX_TARGET_GAP`` intervening tokens are tolerated between
    each consecutive pair of target words; the first target token may
    appear at any position in the candidate.

    Each target word must match a *complete* candidate token — this
    preserves the word-boundary semantic from the strict matcher
    ("data" still doesn't match "database", "lead" still doesn't match
    "leadership").

    This is the fallback used by ``_title_matches`` only when the
    strict phrase match fails. It exists to unstick narrow user-
    configured phrases ("Senior Manager, Analytics") that legitimate
    job postings break apart with intervening qualifiers ("Senior
    Manager, **Data** Analytics", "Senior **Technical** Data Analyst").
    """
    target_words = target_norm.split()
    candidate_words = candidate_norm.split()
    if not target_words or len(target_words) > len(candidate_words):
        return False

    # The first target word may appear anywhere — try each candidate
    # position as the anchor and see if the remaining target words can
    # be matched in order with bounded gaps from there.
    for start in range(len(candidate_words)):
        if candidate_words[start] != target_words[0]:
            continue
        pos = start + 1
        ok = True
        for tw in target_words[1:]:
            stop = min(len(candidate_words), pos + _MAX_TARGET_GAP + 1)
            found_at = -1
            for i in range(pos, stop):
                if candidate_words[i] == tw:
                    found_at = i
                    break
            if found_at < 0:
                ok = False
                break
            pos = found_at + 1
        if ok:
            return True
    return False


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

    3. **Ordered-words fallback** (inclusion only): when the strict phrase
       check fails, fall back to checking that the target's words appear
       *in order* in the candidate with at most ``_MAX_TARGET_GAP``
       intervening tokens. This lets "Senior Manager, Analytics" match
       "Senior Manager, Data Analytics" (NVIDIA-style narrow-phrase
       miss) without re-introducing substring sloppiness. Exclusions
       still use strict phrase match so a sloppier exclude doesn't
       over-filter.

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
            # Ordered-words fallback — slower but rescues narrow phrases.
            if not any(
                _ordered_words_match(_normalize_title(t), normalized) for t in target_titles
            ):
                return False

    return not any(_compile_word_boundary(ex).search(normalized) for ex in exclusions)

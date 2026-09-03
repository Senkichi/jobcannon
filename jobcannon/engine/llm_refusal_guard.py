# PORTED from job_finder/web/llm_refusal_guard.py @ 65e5ce021068b70a2369ac279c75395a078e1013 (private job-cannon). Ledger L-0475.
"""Detects conversational refusals/meta-responses masquerading as valid LLM output.

Several call sites ask a quick-tier LLM to produce free-form prose into a
structured ``{"field": str}`` schema (reformat a description, draft an
application answer). Schema validation only proves the SHAPE is right — a
model can still comply with the shape while filling the field with a
conversational refusal or request for more input instead of the requested
content, e.g. "I'd be happy to reformat this job description professionally!
However, the content you've provided appears to be incomplete...". A
live-corpus audit found exactly this silently persisted into jobs.description
(and, via a downstream auto-promotion path, into jobs.jd_full) because the
caller only checked the returned string was non-empty.

Genuine output for these prompts — reformatted job-description prose, a
drafted application answer — is always written in third person or directly
answers the question; it never opens with first-person address to the
requester. That register is a high-precision tell without needing to compare
the response against the source text (which is unreliable here: refusal
boilerplate often echoes category words from the prompt, e.g. "salary",
"qualifications", back at the reader).

Exports:
    looks_like_llm_refusal: True if text reads like a refusal/meta-response.
"""

import re

# Chars searched for a refusal/meta-response signature. A fabricated preamble
# (e.g. a markdown header the model invents before still refusing) can push
# the tell a little past position 0, so this searches a head window rather
# than anchoring at the very start of the string — mirrors the _HEAD_WINDOW
# convention in job_finder.db._jd_content_contract for the same category of
# problem (detect degenerate LLM output via a tight, high-precision regex).
_REFUSAL_HEAD_WINDOW = 400

_REFUSAL_RE = re.compile(
    r"\bi'?d be happy to\b"
    r"|\bi'?m (?:ready|not able)\b"
    # "understand" is deliberately excluded from this alternation: a live-DB
    # remediation audit found "By clicking the 'Apply' button, I understand
    # that my employment application process..." — genuine scraped ATS consent
    # boilerplate, not an LLM refusal — false-triggering on "\bi understand\b"
    # in the head window. None of the confirmed live-corpus refusal samples
    # rely on it (see tests below).
    r"|\bi (?:need|notice|cannot|can't|don't have|only have)\b"
    r"|\bunable to (?:complete|reformat|draft|answer)\b"
    r"|\byou(?:'ve| have) (?:provided|shared)\b"
    r"|\bplease (?:provide|share|give) the\b"
    r"|\bcould you (?:please )?provide\b"
    r"|\bwas not provided\b"
    r"|\bno job description (?:content|text) was provided\b"
    r"|\binsufficient (?:information|context|detail)\b"
    r"|\bmore (?:information|context|detail)s? (?:is|are) needed\b",
    re.IGNORECASE,
)


def looks_like_llm_refusal(text: str) -> bool:
    """True if *text* reads like a conversational refusal/meta-response rather
    than the requested content (reformatted prose, a drafted answer, etc.)."""
    return bool(_REFUSAL_RE.search(text[:_REFUSAL_HEAD_WINDOW]))

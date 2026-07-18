"""JD-quality structural axis (0-1): length band + shape signal + boilerplate.

Reuses the engine's positive JD-shape vocabulary
(``jobcannon.engine.jd_content_contract.has_recognizable_jd_shape``) rather
than duplicating the section-keyword regex — single point of enforcement.
No NLP dependency: the boilerplate check is a plain word-shingle Jaccard
overlap against sibling postings at the same company.
"""

from __future__ import annotations

import re

from jobcannon.engine.jd_content_contract import has_recognizable_jd_shape

_WORD_RE = re.compile(r"\S+")
_SHINGLE_SIZE = 5


def _shingles(text: str, n: int = _SHINGLE_SIZE) -> set[tuple[str, ...]]:
    """Word n-gram set for *text*, lowercased. Short texts collapse to one shingle."""
    words = _WORD_RE.findall(text.lower())
    if not words:
        return set()
    if len(words) < n:
        return {tuple(words)}
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _boilerplate_ratio(jd_full: str, sibling_jds: list[str]) -> float:
    """Word-shingle Jaccard overlap between jd_full and its most-similar sibling.

    A JD that is near-identical to a sibling posting at the same company (a
    copy-pasted template) scores a high overlap here, which the caller then
    penalizes. No siblings -> 0.0 (cold start; nothing to compare against, so
    no boilerplate penalty is applied).
    """
    if not sibling_jds:
        return 0.0
    own = _shingles(jd_full)
    if not own:
        return 0.0
    overlaps = [_jaccard(own, _shingles(sib)) for sib in sibling_jds if sib]
    return max(overlaps) if overlaps else 0.0


def score_jd_quality(
    jd_full: str | None, sibling_jds: list[str], *, ideal_min: int = 200, ideal_max: int = 1200
) -> dict:
    if not jd_full:
        return {"value": 0.0, "method": "rules_v1"}
    wc = len(_WORD_RE.findall(jd_full))
    band = 1.0 if ideal_min <= wc <= ideal_max else 0.5
    section = 1.0 if has_recognizable_jd_shape(jd_full) else 0.0
    boiler = _boilerplate_ratio(jd_full, sibling_jds)
    value = round(0.4 * band + 0.4 * section + 0.2 * (1.0 - boiler), 3)
    return {"value": value, "method": "rules_v1"}

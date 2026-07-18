"""Comp-transparency structural axis: does the posting state real pay?

Reuses the engine's salary grammar (``jobcannon.engine.salary_normalizer``)
as a READ-ONLY classifier — this module NEVER writes ``salary_min``/
``salary_max``; those columns have a single writer
(``jobcannon.db._jobs.upsert_job``, via the capture-site salary functions).

A structured salary already present on the row wins outright (``"structured"``
method, unambiguous ``True``). Otherwise the JD body is scanned sentence-by-
sentence for a resolvable salary range via the same parser/normalizer the
engine uses for real salary capture, so this axis never invents its own
salary-detection heuristics. A resolvable range whose sentence ALSO quotes a
non-base-pay dollar figure (401k, sign-on/relocation bonus, equity, revenue/
ARR, funding round, stipend/per-diem) is flagged ``"ambiguous"`` rather than
``True`` — the regex grammar cannot tell a real base-pay range from a bonus/
funding figure that happens to parse the same way, so it declines to guess.
"""

from __future__ import annotations

import re

from jobcannon.engine.salary_normalizer import (
    RESOLVED_RESOLUTIONS,
    normalize_observation,
    parse_salary_text,
)

_EXCLUSION_TRAP_RE = re.compile(
    r"401\(?k\)?|sign[- ]on bonus|signing bonus|relocation (?:bonus|assistance|package)"
    r"|equity|RSUs?|stock options?|\bARR\b|annual revenue|revenue growth"
    r"|series [a-e]\b|\bstipend\b|per diem",
    re.IGNORECASE,
)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def score_comp_transparency(salary_min: object, salary_max: object, jd_full: str | None) -> dict:
    if salary_min is not None or salary_max is not None:
        return {"value": True, "method": "structured"}
    if not jd_full:
        return {"value": False, "method": "regex_grammar"}
    for sentence in _SENT_SPLIT_RE.split(jd_full):
        obs = parse_salary_text(sentence, provenance="jd_regex")
        if obs is None:
            continue
        if normalize_observation(obs).resolution not in RESOLVED_RESOLUTIONS:
            continue
        if _EXCLUSION_TRAP_RE.search(sentence):
            return {
                "value": "ambiguous",
                "method": "regex_grammar",
                "candidate_sentence": sentence[:200],
            }
        return {"value": True, "method": "regex_grammar"}
    return {"value": False, "method": "regex_grammar"}

"""Seniority-clarity structural axis: does the title itself declare a level?

Boolean, title-only, word-boundary guarded so substring collisions ("Team
Leader" containing "lead") don't false-positive.
"""

from __future__ import annotations

import re

_LEVEL_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|junior|jr\.?|entry[- ]?level|director|"
    r"vp|vice president|head of|chief|lead(?!\s+gen(?:eration)?\b)|manager|associate|intern"
    r"|level\s*[1-9])\b",
    re.IGNORECASE,
)


def score_seniority_clarity(title: str | None) -> dict:
    return {"value": bool(_LEVEL_RE.search(title or "")), "method": "rules_v1"}

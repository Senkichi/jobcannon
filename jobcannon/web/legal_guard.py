"""jobcannon/web/legal_guard.py — structural guard against publishing
drafting/review matter as if it were ratified legal text (issue #94).

`check_published_text(text)` is the single point of enforcement (see
CLAUDE.md's fix-at-the-right-abstraction-layer rule): both
`scripts/import_legal_text.py` (before it ever writes a committed .md) and
`tests/host/test_legal_pages.py` (against the committed .md files, as a
standing CI gate) call this SAME function. No second, drifted copy of "what
counts as leftover drafting matter" exists anywhere else in the repo.

Every rule below exists to catch one concrete way non-publication matter
could survive into the committed markdown: an HTML comment the strip pass
missed, a `[PLACEHOLDER]` token nobody filled in, a phrase that only makes
sense inside a drafting/review note, a raw commit SHA copied out of an audit
citation, or a missing/malformed effective date. `FORBIDDEN_PHRASES` is
exported (not just used internally) specifically so the test suite's
sabotage self-tests are parametrized off THIS list rather than a
hand-maintained copy — a phrase added here with no accompanying positive
control fails the suite instead of shipping silently unverified.
"""

from __future__ import annotations

import re

# Case-insensitive substrings that belong only in drafting/review matter,
# never in text meant to be read by a site visitor. Every entry here has a
# corresponding sabotage-test case in tests/host/test_legal_pages.py, driven
# off this tuple directly.
FORBIDDEN_PHRASES: tuple[str, ...] = (
    "draft",
    "not published",
    "not legal advice",
    "non-publication",
    "owner/counsel",
    "owner:",
    "counsel",
    "audit §",
    "appendix a",
    "pr #",
    "issue #",
    "gap register",
    "todo",
    "tbd",
)

_HTML_COMMENT_OPEN = "<!--"
_HTML_COMMENT_CLOSE = "-->"

_BRACKET_TOKEN = re.compile(r"\[[A-Z][A-Z /-]{2,}\]")

# A commit-SHA-shaped token: 7-40 lowercase hex characters, word-bounded.
# Requiring at least one digit AND at least one a-f letter in the SAME token
# is what an actual git SHA looks like and what an ordinary lowercase English
# word never does (a word confined to a-f, like "defaced", "facade", or
# "cafe", is legal published prose far more often than it is a truncated
# SHA) — see the module docstring on why this rule exists.
_HEX_TOKEN = re.compile(r"\b[0-9a-f]{7,40}\b")

_EFFECTIVE_DATE_LINE = re.compile(r"^\**Effective date:\**\s*(.*)$", re.MULTILINE)
_EFFECTIVE_DATE_VALUE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _looks_like_sha(token: str) -> bool:
    has_digit = any(c.isdigit() for c in token)
    has_hex_letter = any(c in "abcdef" for c in token)
    return has_digit and has_hex_letter


def check_published_text(text: str) -> list[str]:
    """Return every violation found in `text`. An empty list means clean —
    the text contains no HTML comment delimiter, no unfilled bracket
    placeholder, none of FORBIDDEN_PHRASES (case-insensitive), no
    commit-SHA-shaped token, and a well-formed 'Effective date:' line."""
    violations: list[str] = []

    if _HTML_COMMENT_OPEN in text:
        violations.append("contains an HTML comment opener '<!--'")
    if _HTML_COMMENT_CLOSE in text:
        violations.append("contains an HTML comment closer '-->'")

    for match in _BRACKET_TOKEN.finditer(text):
        violations.append(f"contains an unfilled bracket placeholder: {match.group(0)!r}")

    lowered = text.lower()
    for phrase in FORBIDDEN_PHRASES:
        if phrase in lowered:
            violations.append(f"contains forbidden phrase {phrase!r}")

    for match in _HEX_TOKEN.finditer(lowered):
        if _looks_like_sha(match.group(0)):
            violations.append(f"contains a commit-SHA-shaped token: {match.group(0)!r}")

    date_match = _EFFECTIVE_DATE_LINE.search(text)
    if date_match is None:
        violations.append("missing an 'Effective date:' line")
    elif not _EFFECTIVE_DATE_VALUE.search(date_match.group(1)):
        violations.append(
            f"'Effective date:' line does not contain a YYYY-MM-DD date: {date_match.group(0)!r}"
        )

    return violations

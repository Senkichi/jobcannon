"""jobcannon/web/legal/{privacy,terms}.md — bulleted-list looseness pin
(issue #190).

Owner decision (2026-08-28): KEEP today's list rendering as-is. A general fix
to scripts/import_legal_text.py's comment-stripping — dropping every blank
line a comment-only line leaves behind, not just the table-header/delimiter
shape `_collapse_blank_before_table_delimiter` already handles — was tried
and rejected: measured against both committed files, it also collapsed §2's
"short version" list from LOOSE (each item's text wrapped in its own <p>,
because a stripped audit-citation comment sitting on its own line between two
"- " items leaves a blank line behind) to TIGHT (bare <li>text</li>, no <p>)
— a change to the RENDERED HTML, not just cosmetic .md whitespace. See the
design-rationale comment above `_TABLE_ROW` in scripts/import_legal_text.py.

tests/host/test_import_legal_text.py's
test_comment_between_list_items_leaves_the_list_loose already pins this same
invariant at the importer-TRANSFORM level, against a synthetic fixture. This
module pins it at the SERVED-DOCUMENT level, against the real committed
files — so a future change to either draft's comment placement (not just an
importer code change) is also caught, and the exact per-list state that
would break is on record rather than merely "some list somewhere is loose."

Structural, not prose: every assertion here is about the loose/tight
PATTERN of each rendered `<ul>` block (by position), never about list-item
text content, so wording edits inside these documents (e.g. the 2026-08-28
owner-ratified privacy-policy wording pass) cannot break this module.
"""

from __future__ import annotations

import pathlib
import re

import markdown
from bs4 import BeautifulSoup

from jobcannon.web.legal import _MD_EXTENSIONS

_LEGAL_DIR = pathlib.Path("jobcannon/web/legal")

# Position-indexed: for each committed file, the ordered list of per-item
# looseness booleans for every <ul> the document's markdown renders (True =
# item wrapped in <p>, i.e. "loose"; False = bare <li>text</li>, "tight") —
# captured 2026-08-28 against the live committed files. A future content
# change that adds or removes a list changes the number of entries here (a
# maintainer must consciously re-verify looseness for the new list, not
# silently inherit this snapshot); a future comment-format or importer
# change that flips any existing item's looseness changes one of the
# booleans and fails this test.
_EXPECTED_LIST_STRUCTURE: dict[str, list[list[bool]]] = {
    "privacy.md": [
        [True, True, True, True, True, True],  # ul#1 -- §2 "The short version"
        [True, True, False],  # ul#2 -- §6.2 PostHog properties bag
    ],
    "terms.md": [
        [False, False, False, False, False, False],  # ul#1 -- §4 prohibited uses
    ],
}


def _list_looseness(html: str) -> list[list[bool]]:
    """Return, for every <ul> in rendered order, the per-item looseness
    booleans (True = item's text is wrapped in a <p> -- markdown's "loose
    list" shape; False = bare <li>text</li>, "tight")."""
    soup = BeautifulSoup(html, "html.parser")
    return [
        [li.find("p") is not None for li in ul.find_all("li", recursive=False)]
        for ul in soup.find_all("ul")
    ]


def _render(filename: str) -> str:
    text = (_LEGAL_DIR / filename).read_text(encoding="utf-8")
    return markdown.markdown(text, extensions=_MD_EXTENSIONS)


# ---------------------------------------------------------------------------
# Positive: today's per-list looseness pattern, pinned.
# ---------------------------------------------------------------------------


def test_committed_legal_document_list_looseness_matches_baseline():
    for filename, expected in _EXPECTED_LIST_STRUCTURE.items():
        actual = _list_looseness(_render(filename))
        assert actual == expected, (
            filename,
            actual,
            expected,
            "a <ul>'s loose/tight pattern changed -- issue #190: this must "
            "stay pinned unless the change is a deliberate, reviewed "
            "decision to alter list rendering (update "
            "_EXPECTED_LIST_STRUCTURE consciously here, don't let it drift "
            "silently)",
        )


def test_baseline_covers_every_list_actually_rendered():
    """Positive control for the positive control: the baseline's per-file
    <ul> COUNT must match today's real count, or a list added/removed from
    either document would silently pass the test above by comparing against
    a baseline that no longer describes the real document shape (e.g. an
    IndexError-free but meaningless zip of two different-length lists)."""
    for filename, expected in _EXPECTED_LIST_STRUCTURE.items():
        actual = _list_looseness(_render(filename))
        assert len(actual) == len(expected), (
            filename,
            len(actual),
            len(expected),
            "number of <ul> blocks changed -- update _EXPECTED_LIST_STRUCTURE",
        )


# ---------------------------------------------------------------------------
# Sabotage: collapse the blank line between privacy.md §2's first two list
# items -- the exact shape a generalized comment-stripping fix would
# produce (see module docstring) -- and confirm the structural check above
# would actually catch it. Located structurally (the "## 2." / "## 3."
# section headings and the "- " list-item marker), not by matching the
# items' own prose, so this fixture survives future wording edits to §2.
# ---------------------------------------------------------------------------


def test_sabotage_collapsing_a_list_blank_line_flips_loose_to_tight():
    text = (_LEGAL_DIR / "privacy.md").read_text(encoding="utf-8")

    section_match = re.search(r"(?ms)^## 2\.[^\n]*\n(.*?)(?=^## 3\.)", text)
    assert section_match, "privacy.md's '## 2.' section heading not found -- update this fixture"
    section = section_match.group(1)

    # First blank line directly between a "- " item's own text and the next
    # "- " item -- collapsing it is structurally identical to what a
    # generalized comment-line strip would do.
    sabotaged_section, n = re.subn(r"(?m)(\n)\n(?=- )", r"\1", section, count=1)
    assert n == 1, (
        "no blank-line-then-'- '-item shape found in privacy.md's §2 -- update this fixture"
    )
    assert sabotaged_section != section

    sabotaged_text = text.replace(section, sabotaged_section, 1)
    assert sabotaged_text != text

    pattern = _list_looseness(markdown.markdown(sabotaged_text, extensions=_MD_EXTENSIONS))[0]
    assert False in pattern, (
        pattern,
        "collapsing the blank line between two §2 items must flip at least "
        "one item to TIGHT -- if this assertion fails, "
        "test_committed_legal_document_list_looseness_matches_baseline "
        "would silently pass a real loose-to-tight regression instead of "
        "catching it",
    )

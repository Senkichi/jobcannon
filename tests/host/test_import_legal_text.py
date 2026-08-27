"""scripts/import_legal_text.py — the mechanical draft -> published .md
transform (issue #178, issue #182 item 6).

No DB access, same no-DB shape as tests/host/test_legal_pages.py.

Things this module pins:
  (a) a whole-line HTML comment sitting between a table's header row and its
      delimiter row (the issue #178 shape: draft has an audit-citation
      comment there) does not break the table — the delimiter still lands
      immediately after the header row, and the real markdown renderer
      (the same extensions jobcannon/web/legal.py uses) produces a <table>
  (b) a whole-line HTML comment between two paragraphs does not glue them
      into one paragraph — at least one blank line still separates them
  (c) a whole-line HTML comment between two "- " list items is left alone
      (NOT specially collapsed) — the list stays "loose" (blank line
      between items), same as the committed .md files already ship. This is
      the pinned reason _strip_html_comments was deliberately NOT
      generalized to "a comment-only line never leaves a blank line": doing
      that would also collapse this shape from loose to tight, which changes
      the rendered HTML (loose list items are <p>-wrapped, tight ones are
      not) for content the general fix was never asked to touch. See the
      design-rationale comment above _TABLE_ROW in
      scripts/import_legal_text.py.
  (d) _link_first_cross_reference is idempotent — running it twice on the
      same text is a no-op the second time, so the importer stays safe to
      re-run against a target already published
"""

from __future__ import annotations

import markdown

from scripts.import_legal_text import (
    _link_first_cross_reference,
    build_published_text,
)

_MD_EXTENSIONS = ["tables", "sane_lists"]


def _render(text: str) -> str:
    return markdown.markdown(text, extensions=_MD_EXTENSIONS)


# ---------------------------------------------------------------------------
# (a) comment inside a table must not break the table
# ---------------------------------------------------------------------------


def test_comment_between_table_header_and_delimiter_does_not_break_table():
    raw = (
        "# Doc\n\n"
        "**Effective date:** [EFFECTIVE DATE]\n\n"
        "## 4. A section with a table\n\n"
        "| Purpose | Data | Legal basis |\n"
        "<!-- audit: reviewed against §4 counsel notes, 2026-08-12 -->\n"
        "|---|---|---|\n"
        "| Create and maintain your account | Clerk user id, email | Performance of a contract |\n"
    )

    published = build_published_text(raw, "privacy", "2026-08-27")

    assert "<!--" not in published
    html = _render(published)
    assert "<table>" in html
    assert "Legal basis" in html


def test_comment_inside_table_shape_matches_real_draft_bug():
    """Regression-pins the exact issue #178 mechanism: the comment removal
    alone (no other cleanup) would leave a blank line directly between the
    header and delimiter rows, which the real markdown table extension
    refuses to treat as a table at all."""
    raw = (
        "# Doc\n\n"
        "**Effective date:** [EFFECTIVE DATE]\n\n"
        "| A | B |\n"
        "<!-- comment -->\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
    )

    published = build_published_text(raw, "terms", "2026-08-27")

    # No blank line was left standing between the header row and the
    # delimiter row.
    lines = published.split("\n")
    header_idx = next(i for i, line in enumerate(lines) if line.strip() == "| A | B |")
    assert lines[header_idx + 1].strip() == "|---|---|"
    assert "<table>" in _render(published)


# ---------------------------------------------------------------------------
# (b) comment between paragraphs must not glue them together
# ---------------------------------------------------------------------------


def test_comment_between_paragraphs_does_not_glue_them():
    raw = (
        "# Doc\n\n"
        "**Effective date:** [EFFECTIVE DATE]\n\n"
        "This is the first paragraph of prose.\n\n"
        "<!-- audit: reviewed 2026-08-12 -->\n\n"
        "This is a second, distinct paragraph.\n"
    )

    published = build_published_text(raw, "privacy", "2026-08-27")

    assert "<!--" not in published
    html = _render(published)
    assert "<p>This is the first paragraph of prose.</p>" in html
    assert "<p>This is a second, distinct paragraph.</p>" in html


# ---------------------------------------------------------------------------
# (c) comment between list items: pins that the list stays LOOSE (this is
# the case that makes a general "no comment-only line ever leaves a blank
# line" fix unsafe — see the module docstring above).
# ---------------------------------------------------------------------------


def test_comment_between_list_items_leaves_the_list_loose():
    raw = (
        "# Doc\n\n"
        "**Effective date:** [EFFECTIVE DATE]\n\n"
        "- First item\n"
        "<!-- audit: reviewed 2026-08-12 -->\n"
        "- Second item\n"
    )

    published = build_published_text(raw, "privacy", "2026-08-27")

    assert "<!--" not in published
    html = _render(published)
    # A loose list wraps each item's text in its own <p>; a tight list
    # emits bare "<li>First item</li>" with no <p>. The committed .md files
    # rely on today's behavior (comment-only lines are NOT specially
    # collapsed outside the table-delimiter shape), so this must stay loose.
    assert "<p>First item</p>" in html
    assert "<p>Second item</p>" in html
    assert "<li>First item</li>" not in html


# ---------------------------------------------------------------------------
# (d) _link_first_cross_reference idempotency
# ---------------------------------------------------------------------------


def test_link_first_cross_reference_is_idempotent():
    """f(f(x)) == f(x): running the transform again on its own output must
    not re-wrap the mention it already linked (real usage: the importer
    processes a draft with exactly one plain mention, so this is the shape
    that actually matters, not repeated application to multiple mentions —
    see test_link_first_cross_reference_only_links_first_mention below for
    that separate, single-pass behavior)."""
    raw = "See our Privacy Policy for details.\n"

    once = _link_first_cross_reference(raw, "terms")
    twice = _link_first_cross_reference(once, "terms")

    assert once == "See our [Privacy Policy](/privacy) for details.\n"
    assert once == twice


def test_link_first_cross_reference_only_links_first_mention():
    raw = "See our Privacy Policy for details. Our Privacy Policy is important.\n"

    once = _link_first_cross_reference(raw, "terms")

    assert once.count("[Privacy Policy](/privacy)") == 1
    # Only the FIRST mention is linked — the second stays plain text.
    assert once.endswith("Our Privacy Policy is important.\n")


def test_link_first_cross_reference_no_op_for_unknown_target():
    raw = "Nothing to link here.\n"
    assert _link_first_cross_reference(raw, "bogus") == raw

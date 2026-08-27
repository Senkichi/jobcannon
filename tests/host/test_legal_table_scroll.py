"""jobcannon/web/templates/legal_page.html -- narrow-viewport table scroll
(issue #229).

At 390px, /privacy's section-4 legal-basis table (3 columns: Purpose /
Data / Legal basis) doesn't fit even with `.legal-prose table`'s existing
`width: 100%` -- `table-layout: auto` (the default) treats a percentage
width as a floor, not a cap (CSS2.1 17.5.2.2), so a browser still grows
the table past it when a cell's content can't wrap enough. The result was
the whole DOCUMENT overflowing horizontally (clientWidth 390 vs
scrollWidth 421), not just that one table looking cramped.

The fix is ONE CSS rule in legal_page.html's `<style>` block: at
`max-width: 640px`, `.legal-prose table { display: block; overflow-x:
auto; }` turns each table element itself into its own horizontal-scroll
container. `display: block` (not a wrapping `<div>`) was chosen
specifically so `jobcannon/web/legal.py`'s rendered `body_html` gains no
new HTML at all -- a `<div class="table-scroll">` wrapper would put a
second, nested `<div>` inside `.legal-prose`, breaking the "body_html
contains exactly one <div>" invariant
tests/host/test_legal_pages.py's `test_legal_page_body_is_byte_identical_regardless_of_auth_state`
documents and relies on (its own `_LEGAL_PROSE_RE` regex stops at the
FIRST `</div>` it finds). A CSS-only rule touches zero HTML, so that
invariant -- and every other test reading legal.py's output -- holds by
construction.

Because the rule is keyed off the existing `.legal-prose table` selector
(not a per-file or per-table class), it automatically covers every table
any committed jobcannon/web/legal/*.md file's markdown renders, on both
/privacy (5 tables today) and /terms (0 today, gains coverage for free if
one is ever added) -- there is nothing to hand-maintain. This module pins
that single point structurally (positive) and sabotage-proves the pin
actually catches a regression (negative) rather than trusting a `search()`
that could vacuously match anything.

Before/after DOM measurement (Playwright, local server, both a plain
390x844 viewport and full iPhone-13 emulation, walking every
`.legal-prose table` on the page rather than just the first -- see the PR
body for the numbers and the script) is the empirical proof this actually
fixes the reported overflow; this module is the standing regression guard
for the CSS itself.
"""

from __future__ import annotations

import pathlib
import re

from jobcannon.web import legal

_LEGAL_PAGE_TEMPLATE = pathlib.Path("jobcannon/web/templates/legal_page.html")
_LEGAL_MD_DIR = pathlib.Path("jobcannon/web/legal")

# Matches the WHOLE `@media (max-width: 640px) { .legal-prose table { ... } }`
# block and captures just the inner declarations, so a check on their
# CONTENT (display/overflow-x) is independent of exact whitespace/formatting.
_NARROW_TABLE_RULE_RE = re.compile(
    r"@media\s*\(\s*max-width\s*:\s*640px\s*\)\s*\{\s*"
    r"\.legal-prose\s+table\s*\{([^}]*)\}\s*\}",
    re.DOTALL,
)


def _narrow_table_scroll_declarations(css: str) -> str | None:
    """Return the declaration block of the `@media (max-width: 640px)
    .legal-prose table { ... }` rule, or None if no such rule is present
    at all (wrong selector, missing media query, rule dropped entirely)."""
    match = _NARROW_TABLE_RULE_RE.search(css)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Positive: the rule exists and declares the mechanism the PR body's
# measurement actually exercised.
# ---------------------------------------------------------------------------


def test_narrow_viewport_table_scroll_rule_is_present():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _narrow_table_scroll_declarations(css)
    assert decls is not None, (
        "legal_page.html has no `@media (max-width: 640px) .legal-prose "
        "table { ... }` rule -- issue #229's narrow-viewport table "
        "overflow fix is missing"
    )


def test_narrow_viewport_table_scroll_rule_declares_display_block():
    """`display: block` is what makes the table element itself become the
    scroll container -- without it, `overflow-x: auto` on a still-`table`-
    display element does not reliably clip/scroll (this is why the fix
    isn't just adding `overflow-x: auto` alone)."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _narrow_table_scroll_declarations(css)
    assert decls is not None
    assert re.search(r"display\s*:\s*block", decls), decls


def test_narrow_viewport_table_scroll_rule_declares_overflow_x_auto_or_scroll():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _narrow_table_scroll_declarations(css)
    assert decls is not None
    assert re.search(r"overflow-x\s*:\s*(auto|scroll)\b", decls), decls


def test_narrow_viewport_table_scroll_rule_does_not_use_white_space_nowrap():
    """`white-space: nowrap` is a common ingredient in the naive version of
    this CSS recipe, but it INFLATES every cell's min-content width and
    makes the scroll region wider, not narrower -- it must not be present
    on this rule."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _narrow_table_scroll_declarations(css)
    assert decls is not None
    assert "nowrap" not in decls, decls


# ---------------------------------------------------------------------------
# Negative / sabotage: prove the structural check above actually catches a
# regression, not just that `.search()` happens to return something.
# ---------------------------------------------------------------------------


def test_narrow_viewport_table_scroll_rule_detects_a_dropped_selector():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    original_selector = ".legal-prose table { display: block; overflow-x: auto; }"
    assert original_selector in css, (
        "legal_page.html's narrow-viewport table rule text changed -- update this sabotage fixture"
    )
    sabotaged = css.replace(
        original_selector, ".legal-prose nope { display: block; overflow-x: auto; }", 1
    )
    assert sabotaged != css
    assert _narrow_table_scroll_declarations(sabotaged) is None, (
        "sabotaging the selector must make the structural check fail -- "
        "if this assertion fails, the guard above would silently pass a "
        "rule that no longer targets .legal-prose table at all"
    )


def test_narrow_viewport_table_scroll_rule_detects_a_dropped_overflow_declaration():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    original_rule_body = "display: block; overflow-x: auto;"
    assert original_rule_body in css, (
        "legal_page.html's narrow-viewport table rule text changed -- update this sabotage fixture"
    )
    sabotaged = css.replace(original_rule_body, "display: block;", 1)
    assert sabotaged != css
    decls = _narrow_table_scroll_declarations(sabotaged)
    assert decls is not None  # selector still there
    assert not re.search(r"overflow-x\s*:\s*(auto|scroll)\b", decls), (
        "sabotaging away the overflow-x declaration must make the "
        "overflow-x check fail -- see "
        "test_narrow_viewport_table_scroll_rule_declares_overflow_x_auto_or_scroll"
    )


def test_narrow_viewport_table_scroll_rule_detects_a_reintroduced_nowrap():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    original_rule_body = "display: block; overflow-x: auto;"
    assert original_rule_body in css, (
        "legal_page.html's narrow-viewport table rule text changed -- update this sabotage fixture"
    )
    sabotaged = css.replace(
        original_rule_body, "display: block; overflow-x: auto; white-space: nowrap;", 1
    )
    assert sabotaged != css
    decls = _narrow_table_scroll_declarations(sabotaged)
    assert decls is not None
    assert "nowrap" in decls, (
        "sabotage fixture didn't actually reintroduce nowrap into the "
        "captured declarations -- fix the fixture"
    )


# ---------------------------------------------------------------------------
# Single point of enforcement: no per-table markup in the ratified .md
# files themselves -- the CSS rule above is the ONLY place this is
# expressed. jobcannon.web.legal_guard already rejects any raw HTML tag in
# the source markdown (tests/host/test_legal_pages.py's
# test_guard_fires_on_raw_html_tag), so this is a second, independent
# angle on the same invariant rather than the sole guard for it.
# ---------------------------------------------------------------------------


def test_no_per_table_markup_in_ratified_legal_markdown():
    served_files = sorted(set(legal._LEGAL_PAGES.values()))
    assert served_files, "legal._LEGAL_PAGES is empty -- nothing to cover"
    for filename in served_files:
        text = (_LEGAL_MD_DIR / filename).read_text(encoding="utf-8").lower()
        assert "<table" not in text, (filename, "raw <table markup found in ratified .md")
        assert "class=" not in text, (filename, "raw class= markup found in ratified .md")
        assert "table-scroll" not in text, (
            filename,
            "per-file scroll-wrapper markup found -- the CSS rule in "
            "legal_page.html is supposed to be the ONLY place this lives",
        )


def test_every_legal_page_table_renders_under_the_shared_legal_prose_selector():
    """Derives the page set from `legal._LEGAL_PAGES` (no hardcoded
    "/privacy"/"/terms" list) and counts `<table>` occurrences in each
    page's OWN rendered HTML dynamically (no hardcoded literal count) --
    every table found must be reachable by the single `.legal-prose table`
    selector the rule above targets, i.e. must render inside the
    `.legal-prose` container. Cross-checks against
    tests/host/test_touch_targets.py's
    test_every_served_legal_route_renders_inside_the_legal_prose_container,
    which proves the container itself is present; this test additionally
    confirms it actually contains the table markup when a file has any."""
    served_files = sorted(set(legal._LEGAL_PAGES.values()))
    assert served_files, "legal._LEGAL_PAGES is empty -- nothing to cover"

    saw_at_least_one_table = False
    for filename in served_files:
        _title, html = legal._render(filename)
        table_count = html.count("<table>")
        if table_count == 0:
            continue
        saw_at_least_one_table = True
        # legal._render()'s return value is the exact string
        # legal_page.html wraps in `<div class="legal-prose">...</div>`
        # (test_legal_page_wraps_body_html_in_the_padded_prose_container
        # pins that wrapping structurally) -- so every `<table>` this
        # produces is, by construction, a `.legal-prose table`.
        assert table_count == html.count("</table>"), (filename, "unbalanced <table> tags")

    assert saw_at_least_one_table, (
        "no committed legal markdown file rendered any <table> at all -- "
        "this test would otherwise pass vacuously with nothing to cover "
        "(privacy.md is expected to contribute at least one)"
    )

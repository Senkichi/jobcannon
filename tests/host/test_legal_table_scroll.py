"""jobcannon/web/templates/legal_page.html + jobcannon/web/legal.py --
narrow-viewport table scroll (issue #229).

At 390px, /privacy's section-4 legal-basis table (3 columns: Purpose /
Data / Legal basis) doesn't fit even with `.legal-prose table`'s existing
`width: 100%` -- `table-layout: auto` (the default) treats a percentage
width as a floor, not a cap (CSS2.1 17.5.2.2), so a browser still grows
the table past it when a cell's content can't wrap enough. The result was
the whole DOCUMENT overflowing horizontally (clientWidth 390 vs
scrollWidth 421), not just that one table looking cramped.

The fix has two parts:
  1. `jobcannon/web/legal.py`'s `_wrap_tables_for_scroll` (called from
     `_render`, the single render chokepoint both /privacy and /terms go
     through) wraps every rendered `<table>...</table>` in
     `<div class="table-scroll">...</div>`.
  2. `jobcannon/web/templates/legal_page.html` declares ONE CSS rule, with
     NO media query: `.legal-prose .table-scroll { overflow-x: auto;
     max-width: 100%; }`.

This replaces PR #231's first draft, `@media (max-width: 640px) {
.legal-prose table { display: block; overflow-x: auto; } }`. Adversarial
review found that rule changed the table's own box-sizing algorithm
(`display: table` -> `display: block`) for every table below the
breakpoint at every viewport from ~400px up to 640px, including tables
that never overflowed -- an undisclosed visual-change surface that exists
purely because the mechanism was keyed to a breakpoint at all. The wrapper
has no such surface: `.legal-prose table`'s `width: 100%` and
`display: table` are never touched, at any viewport width, so a table
that already fit cannot change. Only a table whose content genuinely
can't fit triggers the wrapper's own internal scrollbar.

The wrapper was rejected in PR #231's first draft specifically because
`tests/host/test_legal_pages.py`'s byte-identity test used to extract
`.legal-prose`'s contents with a non-greedy regex
(`<div class="legal-prose">(.*?)</div>`) that stopped at the FIRST
`</div>` -- a wrapper div would have silently truncated what that test
verified. That extraction was rewritten to use a real HTML parser
(BeautifulSoup), which finds `.legal-prose`'s true closing tag regardless
of nesting, so the wrapper is now safe to adopt; see that test file's
`_legal_body()` docstring.

Because `_wrap_tables_for_scroll` operates on ALL rendered HTML (not a
per-file or per-table opt-in) and the CSS rule is keyed off the existing
`.legal-prose` selector, both automatically cover every table any
committed jobcannon/web/legal/*.md file's markdown renders, on both
/privacy (5 tables today) and /terms (0 today, gains coverage for free if
one is ever added) -- there is nothing to hand-maintain. This module pins
that single point structurally (positive), sabotage-proves the pin
actually catches a regression (negative), and verifies the mechanism
behaviorally end-to-end through a real Flask test client and a real HTML
parser -- not just as a regex over template/rendered source.

Before/after DOM measurement (Playwright, local server, both a plain
390x844 viewport and full iPhone-13 emulation, walking every
`.legal-prose table` on the page at 390/500/641/1280px, before vs. after)
is the empirical proof this fixes the reported overflow with zero change
to any table's width above ~400px; see the PR body for the numbers and
script. This module is the standing regression guard for the mechanism
itself.
"""

from __future__ import annotations

import pathlib
import re

from bs4 import BeautifulSoup

from jobcannon.web import legal

_LEGAL_PAGE_TEMPLATE = pathlib.Path("jobcannon/web/templates/legal_page.html")
_LEGAL_MD_DIR = pathlib.Path("jobcannon/web/legal")


def _app():
    from jobcannon.web import create_app

    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": lambda req: None,
            "WEBHOOK_SECRET": "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=",
        }
    )


# ---------------------------------------------------------------------------
# CSS rule: `.legal-prose .table-scroll { ... }`, parsed into real
# declarations (property -> value), not compared as a literal string --
# reformatting (whitespace, declaration order, extra declarations added
# later) can't make these checks miss a semantic regression.
# ---------------------------------------------------------------------------

_TABLE_SCROLL_RULE_RE = re.compile(
    r"\.legal-prose\s+\.table-scroll\s*\{([^}]*)\}",
    re.DOTALL,
)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _table_scroll_declarations(css: str) -> dict[str, str] | None:
    """Return the `.legal-prose .table-scroll { ... }` rule's declarations
    as a {property: value} dict, or None if no such (LIVE) rule is present
    at all (wrong selector, rule dropped entirely, or the only match is
    inside a /* ... */ comment -- CSS comments are stripped first, so a
    rule that's present as text but disabled by commenting it out is
    correctly treated the same as a rule that was deleted outright; a
    browser applies neither)."""
    match = _TABLE_SCROLL_RULE_RE.search(_CSS_COMMENT_RE.sub("", css))
    if match is None:
        return None
    decls: dict[str, str] = {}
    for decl in match.group(1).split(";"):
        decl = decl.strip()
        if not decl:
            continue
        prop, _, value = decl.partition(":")
        decls[prop.strip().lower()] = value.strip()
    return decls


# ---------------------------------------------------------------------------
# `.legal-prose .table-scroll:focus-visible { ... }` (issue #239, WCAG
# 2.4.7): the wrapper's `tabindex="0"` (jobcannon/web/legal.py's
# `_wrap_tables_for_scroll`) puts it in the Tab order, but tabindex alone
# gives no VISIBLE indication a keyboard user landed there -- this rule is
# the other half of the fix. `:focus-visible`, not bare `:focus`, so the ring
# only shows for keyboard navigation, matching every other focus ring in
# this app.
# ---------------------------------------------------------------------------

_TABLE_SCROLL_FOCUS_RULE_RE = re.compile(
    r"\.legal-prose\s+\.table-scroll:focus-visible\s*\{([^}]*)\}",
    re.DOTALL,
)


def _table_scroll_focus_declarations(css: str) -> dict[str, str] | None:
    """Same idea as `_table_scroll_declarations`, but for the
    `:focus-visible` rule."""
    match = _TABLE_SCROLL_FOCUS_RULE_RE.search(_CSS_COMMENT_RE.sub("", css))
    if match is None:
        return None
    decls: dict[str, str] = {}
    for decl in match.group(1).split(";"):
        decl = decl.strip()
        if not decl:
            continue
        prop, _, value = decl.partition(":")
        decls[prop.strip().lower()] = value.strip()
    return decls


def test_table_scroll_focus_visible_rule_is_present():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _table_scroll_focus_declarations(css)
    assert decls is not None, (
        "legal_page.html has no `.legal-prose .table-scroll:focus-visible { ... }` "
        "rule -- keyboard focus on the scroll wrapper is invisible (WCAG 2.4.7)"
    )


def test_table_scroll_focus_visible_rule_declares_a_visible_outline():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _table_scroll_focus_declarations(css)
    assert decls is not None
    outline = decls.get("outline", "")
    assert outline and outline.strip().lower() != "none", (
        decls,
        "`outline` must be a real, visible value, not absent or 'none'",
    )


def test_table_scroll_focus_visible_rule_declares_the_outline_offset():
    """`outline-offset: 2px` is a second, independent property from
    `outline` itself -- a maintainer could keep a real, non-`none` outline
    (satisfying the test above) while silently dropping or shrinking the
    offset, which the outline test alone would not catch. The offset is
    load-bearing, not cosmetic: it draws the ring 2px OUTSIDE the wrapper's
    own border rather than overlapping it, which is what keeps the ring off
    an adjacent `.legal-prose th` cell's `#171717` background in practice
    (a `th` can sit flush against the wrapper's edge) -- see this rule's
    comment in legal_page.html for the full contrast reasoning."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _table_scroll_focus_declarations(css)
    assert decls is not None
    assert decls.get("outline-offset", "").strip().lower() == "2px", (
        decls,
        "`.legal-prose .table-scroll:focus-visible` must declare "
        "`outline-offset: 2px` -- without it the ring can overlap an "
        "adjacent table-header cell instead of sitting outside the wrapper",
    )


def test_table_scroll_focus_visible_rule_detects_a_dropped_outline_offset():
    """Sabotage-proves the check above actually fires, mirroring the
    dropped-outline sabotage below."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    live_css = _CSS_COMMENT_RE.sub("", css)
    match = _TABLE_SCROLL_FOCUS_RULE_RE.search(live_css)
    assert match, "legal_page.html's focus-visible rule is missing -- update this sabotage fixture"
    original_block = match.group(0)
    sabotaged_block = original_block.replace("outline-offset: 2px;", "", 1)
    assert sabotaged_block != original_block, (
        "legal_page.html's focus-visible rule text changed -- update this sabotage fixture"
    )
    sabotaged = css.replace(original_block, sabotaged_block, 1)
    assert sabotaged != css, (
        "the live .table-scroll:focus-visible rule text wasn't found verbatim in "
        "the raw (uncommented) file -- update this sabotage fixture"
    )
    decls = _table_scroll_focus_declarations(sabotaged)
    assert decls is not None  # selector still there
    assert decls.get("outline-offset", "").strip().lower() != "2px", (
        "sabotaging away the outline-offset declaration must make the "
        "offset check fail -- see "
        "test_table_scroll_focus_visible_rule_declares_the_outline_offset"
    )


def test_table_scroll_focus_visible_rule_detects_a_dropped_selector():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    original_selector = ".legal-prose .table-scroll:focus-visible"
    assert original_selector in css, (
        "legal_page.html's focus-visible rule selector changed -- update this sabotage fixture"
    )
    sabotaged = css.replace(original_selector, ".legal-prose .nope:focus-visible", 1)
    assert sabotaged != css
    assert _table_scroll_focus_declarations(sabotaged) is None, (
        "sabotaging the selector must make the structural check fail -- "
        "if this assertion fails, the guard above would silently pass a "
        "rule that no longer targets .legal-prose .table-scroll:focus-visible"
    )


def test_table_scroll_focus_visible_rule_detects_a_dropped_outline():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    # Comments stripped BEFORE searching -- same reasoning
    # `_table_scroll_focus_declarations` already applies: this file
    # documents rejected approaches by name in its own comments (see the
    # `display:block` history further down), so a future commented-out
    # `:focus-visible` rule must not let this fixture match the wrong
    # (dead) block and pass vacuously.
    live_css = _CSS_COMMENT_RE.sub("", css)
    match = _TABLE_SCROLL_FOCUS_RULE_RE.search(live_css)
    assert match, "legal_page.html's focus-visible rule is missing -- update this sabotage fixture"
    original_block = match.group(0)
    sabotaged_block = original_block.replace("outline: 2px solid var(--lj-gray);", "", 1)
    assert sabotaged_block != original_block, (
        "legal_page.html's focus-visible rule text changed -- update this sabotage fixture"
    )
    sabotaged = css.replace(original_block, sabotaged_block, 1)
    assert sabotaged != css, (
        "the live .table-scroll:focus-visible rule text wasn't found verbatim in "
        "the raw (uncommented) file -- update this sabotage fixture"
    )
    decls = _table_scroll_focus_declarations(sabotaged)
    assert decls is not None  # selector still there
    outline = decls.get("outline", "")
    assert not outline or outline.strip().lower() == "none", (
        "sabotaging away the outline declaration must make the visible-outline "
        "check fail -- see test_table_scroll_focus_visible_rule_declares_a_visible_outline"
    )


# ---------------------------------------------------------------------------
# `.legal-prose table { ... }` -- the OTHER selector on this page. Only the
# margin-collapse-relevant half of it matters here (see
# jobcannon/web/legal.py's `_wrap_tables_for_scroll` docstring): the wrapper
# rule above must own `margin`, and this rule must not, or the two either
# double-stack a table's vertical spacing or drop it entirely depending on
# which maintainer mistake happens.
# ---------------------------------------------------------------------------

_TABLE_RULE_RE = re.compile(
    r"\.legal-prose\s+table\s*\{([^}]*)\}",
    re.DOTALL,
)


def _table_rule_declarations(css: str) -> dict[str, str] | None:
    """Same idea as `_table_scroll_declarations`, but for `.legal-prose
    table { ... }`. Comments are stripped FIRST, not optionally:
    legal_page.html's own `<style>` comments have, in the past, quoted a
    rejected `.legal-prose table { display: block; ... }` draft rule by
    name as part of the fix's history -- a naive `.search()` against the
    RAW (uncommented) file is one `.findall()`-plus-last-match refactor
    away from reading that quoted, synthetic rule instead of the real, live
    one below. That synthetic rule never declares `margin` either, so such
    a bug would keep reporting "no margin" -- passing test_table_element_
    rule_declares_no_margin below for the WRONG reason -- even after a
    maintainer genuinely re-added `margin: 1rem 0 1.5rem` to the real,
    live `.legal-prose table` rule."""
    match = _TABLE_RULE_RE.search(_CSS_COMMENT_RE.sub("", css))
    if match is None:
        return None
    decls: dict[str, str] = {}
    for decl in match.group(1).split(";"):
        decl = decl.strip()
        if not decl:
            continue
        prop, _, value = decl.partition(":")
        decls[prop.strip().lower()] = value.strip()
    return decls


# ---------------------------------------------------------------------------
# Positive: the rule exists, declares the mechanism the PR body's
# measurement actually exercised, and is not gated behind a media query.
# ---------------------------------------------------------------------------


def test_table_scroll_rule_is_present():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _table_scroll_declarations(css)
    assert decls is not None, (
        "legal_page.html has no `.legal-prose .table-scroll { ... }` rule "
        "-- issue #229's narrow-viewport table overflow fix is missing"
    )


def test_table_scroll_rule_declares_overflow_x_auto_or_scroll():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _table_scroll_declarations(css)
    assert decls is not None
    assert re.fullmatch(r"auto|scroll", decls.get("overflow-x", ""), re.IGNORECASE), decls


def test_table_scroll_rule_is_not_gated_by_a_media_query():
    """Unlike PR #231's rejected first draft, this rule must apply at every
    viewport width -- there is no breakpoint at which a table's sizing
    should differ. Scoped to the whole file (not just this one rule)
    because legal_page.html's <style> block has never had a legitimate
    reason to carry a media query other than the one this fix replaces;
    a future maintainer adding an unrelated one here should look twice.
    CSS comments are stripped first -- this file's own comments document
    the rejected @media-gated approach by name, which would otherwise be a
    false positive."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    live_css = _CSS_COMMENT_RE.sub("", css)
    assert "@media" not in live_css, (
        "legal_page.html declares a live @media rule -- the table-scroll "
        "fix must not be viewport-width-conditional (see this module's "
        "docstring for why the previous @media-gated display:block "
        "approach was replaced)"
    )


# ---------------------------------------------------------------------------
# Margin-collapse fix (re-review, MEDIUM): the wrapper -- not the table --
# must own the vertical margin. `overflow-x: auto` makes `.table-scroll` a
# block-formatting-context root, which stops a child table's margin from
# collapsing through it with the wrapper's own siblings (CSS2.1 8.3.1); a
# margin left on `.legal-prose table` instead would stack additively with
# adjacent siblings rather than collapsing (measured +68px on /privacy when
# this was misplaced during this fix's own development -- see
# jobcannon/web/legal.py's `_wrap_tables_for_scroll` docstring). This was the
# one mechanism in the fix with no test until now.
# ---------------------------------------------------------------------------


def test_table_scroll_rule_declares_margin():
    """Half (a): `.legal-prose .table-scroll` must own the vertical margin
    -- see the module-level comment above for why."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _table_scroll_declarations(css)
    assert decls is not None
    assert "margin" in decls, (
        "`.legal-prose .table-scroll` has no margin declaration -- a "
        "table's vertical spacing must live on the wrapper (the BFC "
        "root), not on `.legal-prose table`, or it stacks instead of "
        "collapsing with adjacent siblings"
    )


def test_table_element_rule_declares_no_margin():
    """Half (b): `.legal-prose table` itself must NOT declare margin -- if
    it does, the table's own margin STACKS on top of `.table-scroll`'s
    margin (an element's own margin and a BFC-root ancestor's margin do not
    merge) instead of the single collapsed value a bare table used to
    produce. Comments are stripped before matching -- see
    `_table_rule_declarations`'s docstring for why that matters here
    specifically (unlike the sibling checks above)."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    decls = _table_rule_declarations(css)
    assert decls is not None, (
        "legal_page.html has no `.legal-prose table { ... }` rule -- "
        "update this test or the template"
    )
    margin_props = [prop for prop in decls if prop == "margin" or prop.startswith("margin-")]
    assert not margin_props, (
        f"`.legal-prose table` declares {margin_props} -- vertical margin "
        "belongs on `.legal-prose .table-scroll` (the BFC root), not on "
        "the table itself, or the two double-stack instead of collapsing "
        "with adjacent siblings"
    )


def test_table_element_rule_detects_margin_moved_back_from_the_wrapper():
    """Sabotage D from the re-review: move `margin: 1rem 0 1.5rem` from
    `.legal-prose .table-scroll` back onto `.legal-prose table` -- the
    layout that shipped before this fix. Proves
    test_table_element_rule_declares_no_margin actually fires on this
    exact regression rather than passing vacuously."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    match = _TABLE_RULE_RE.search(_CSS_COMMENT_RE.sub("", css))
    assert match, (
        "legal_page.html's `.legal-prose table` rule is missing -- update this sabotage fixture"
    )
    original_block = match.group(0)
    assert original_block.rstrip().endswith("}")
    sabotaged_block = original_block.rstrip()[:-1] + " margin: 1rem 0 1.5rem; }"
    assert sabotaged_block != original_block
    sabotaged = css.replace(original_block, sabotaged_block, 1)
    assert sabotaged != css, (
        "the live `.legal-prose table` rule text wasn't found verbatim in "
        "the raw (uncommented) file -- update this sabotage fixture"
    )
    decls = _table_rule_declarations(sabotaged)
    assert decls is not None
    margin_props = [prop for prop in decls if prop == "margin" or prop.startswith("margin-")]
    assert margin_props, (
        "sabotaging margin back onto `.legal-prose table` must make "
        "test_table_element_rule_declares_no_margin fail -- if this "
        "assertion fails, that check would silently pass the exact "
        "regression it exists to catch"
    )


# ---------------------------------------------------------------------------
# Negative / sabotage: prove the structural checks above actually catch a
# regression, not just that `.search()` happens to return something.
# ---------------------------------------------------------------------------


def test_table_scroll_rule_detects_a_dropped_selector():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    original_selector = ".legal-prose .table-scroll"
    assert original_selector in css, (
        "legal_page.html's table-scroll rule selector changed -- update this sabotage fixture"
    )
    sabotaged = css.replace(original_selector, ".legal-prose .nope", 1)
    assert sabotaged != css
    assert _table_scroll_declarations(sabotaged) is None, (
        "sabotaging the selector must make the structural check fail -- "
        "if this assertion fails, the guard above would silently pass a "
        "rule that no longer targets .legal-prose .table-scroll at all"
    )


def test_table_scroll_rule_detects_a_dropped_overflow_declaration():
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    match = _TABLE_SCROLL_RULE_RE.search(css)
    assert match, "legal_page.html's table-scroll rule is missing -- update this sabotage fixture"
    original_block = match.group(0)
    sabotaged_block = original_block.replace("overflow-x: auto;", "", 1)
    assert sabotaged_block != original_block, (
        "legal_page.html's table-scroll rule text changed -- update this sabotage fixture"
    )
    sabotaged = css.replace(original_block, sabotaged_block, 1)
    decls = _table_scroll_declarations(sabotaged)
    assert decls is not None  # selector still there
    assert "overflow-x" not in decls or not re.fullmatch(
        r"auto|scroll", decls["overflow-x"], re.IGNORECASE
    ), (
        "sabotaging away the overflow-x declaration must make the "
        "overflow-x check fail -- see "
        "test_table_scroll_rule_declares_overflow_x_auto_or_scroll"
    )


def test_table_scroll_rule_detects_a_reintroduced_media_query():
    """Exercises the SAME comment-stripping logic
    test_table_scroll_rule_is_not_gated_by_a_media_query uses (not a bare
    substring check) -- proves the guard actually fires on a live @media
    rule wrapped around the real selector, not merely that the literal
    string "@media" appears somewhere (this file's own comments already
    contain that word, describing the rejected approach by name)."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    match = _TABLE_SCROLL_RULE_RE.search(css)
    assert match, "legal_page.html's table-scroll rule is missing -- update this sabotage fixture"
    original_block = match.group(0)
    sabotaged_block = f"@media (max-width: 640px) {{ {original_block} }}"
    sabotaged = css.replace(original_block, sabotaged_block, 1)
    assert sabotaged != css
    live_sabotaged = _CSS_COMMENT_RE.sub("", sabotaged)
    assert "@media" in live_sabotaged, (
        "sabotage fixture didn't actually reintroduce a live @media block -- fix the fixture"
    )
    # And the rule is still individually well-formed inside it -- proves
    # the media-query check above is the ONLY thing catching this, not an
    # incidental side effect of a now-broken selector.
    assert _table_scroll_declarations(sabotaged) is not None


# ---------------------------------------------------------------------------
# `_wrap_tables_for_scroll` itself: a pure function on HTML strings, tested
# directly rather than through legal._RENDERED (which is built once at
# import time -- monkeypatching the wrapper function after import would
# not affect the already-rendered dict every route serves from).
# ---------------------------------------------------------------------------


def test_wrap_tables_for_scroll_wraps_a_bare_table_exactly():
    html = "<h1>X</h1>\n<table>\n<tbody><tr><td>a</td></tr></tbody>\n</table>\n<p>after</p>"
    wrapped = legal._wrap_tables_for_scroll(html)
    assert wrapped == (
        '<h1>X</h1>\n<div class="table-scroll" tabindex="0" role="region" '
        'aria-label="Scrollable table 1 of 1"><table>\n'
        "<tbody><tr><td>a</td></tr></tbody>\n</table></div>\n<p>after</p>"
    )


def test_wrap_tables_for_scroll_is_a_no_op_when_there_are_no_tables():
    html = "<h1>X</h1>\n<p>no tables here</p>"
    assert legal._wrap_tables_for_scroll(html) == html


def test_wrap_tables_for_scroll_wraps_every_table_independently():
    html = (
        "<table>\n<tbody><tr><td>a</td></tr></tbody>\n</table>"
        "<p>mid</p>"
        "<table>\n<tbody><tr><td>b</td></tr></tbody>\n</table>"
    )
    wrapped = legal._wrap_tables_for_scroll(html)
    assert wrapped.count('class="table-scroll"') == 2
    assert wrapped.count("<table>") == 2
    assert wrapped.count("</table>") == 2


# ---------------------------------------------------------------------------
# Keyboard accessibility (issue #239, WCAG 2.1.1 + 2.4.7): the wrapper must
# be reachable and named without a mouse, and focus on it must be visible.
# ---------------------------------------------------------------------------


def test_wrap_tables_for_scroll_wrapper_is_keyboard_focusable_and_named():
    html = "<table>\n<tbody><tr><td>a</td></tr></tbody>\n</table>"
    wrapped = legal._wrap_tables_for_scroll(html)
    assert 'tabindex="0"' in wrapped, "wrapper is not in the Tab order (WCAG 2.1.1)"
    assert 'role="region"' in wrapped, "wrapper has no landmark role for assistive tech"
    assert 'aria-label="Scrollable table 1 of 1"' in wrapped, (
        "wrapper has no accessible name (WCAG 4.1.2) -- a bare tabindex "
        "announces as an unlabeled region"
    )


def test_wrap_tables_for_scroll_labels_are_indexed_and_unique_per_call():
    """Numbered by rendering order WITHIN one call -- not a hand-maintained
    per-table list, and trivially unique regardless of table content (see
    _wrap_tables_for_scroll's docstring for why a heading-derived label was
    rejected: two tables under the same subheading would collide)."""
    html = (
        "<table>\n<tbody><tr><td>a</td></tr></tbody>\n</table>"
        "<p>mid</p>"
        "<table>\n<tbody><tr><td>b</td></tr></tbody>\n</table>"
        "<table>\n<tbody><tr><td>c</td></tr></tbody>\n</table>"
    )
    wrapped = legal._wrap_tables_for_scroll(html)
    labels = re.findall(r'aria-label="([^"]*)"', wrapped)
    assert labels == [
        "Scrollable table 1 of 3",
        "Scrollable table 2 of 3",
        "Scrollable table 3 of 3",
    ]
    assert len(labels) == len(set(labels)), "labels must be unique within one page"


# ---------------------------------------------------------------------------
# Single point of enforcement: no per-table markup in the ratified .md
# files themselves -- the mechanism above (Python post-processing + one CSS
# rule) is the ONLY place table-scroll wrapping is expressed.
# jobcannon.web.legal_guard already rejects any raw HTML tag in the source
# markdown (tests/host/test_legal_pages.py's test_guard_fires_on_raw_html_tag),
# so this is a second, independent angle on the same invariant.
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
            "per-file scroll-wrapper markup found -- wrapping is supposed to "
            "happen ONLY in jobcannon/web/legal.py's _wrap_tables_for_scroll",
        )


# ---------------------------------------------------------------------------
# Behavioral: fetch every route in `legal._LEGAL_PAGES` (no hardcoded path
# list -- mirrors tests/host/test_touch_targets.py's
# test_every_served_legal_route_renders_inside_the_legal_prose_container)
# through a real Flask test client and parse the actual response body with
# a real HTML parser -- not a regex on template source or on
# legal._render()'s isolated return value. Proves the production
# request/response path, end to end.
# ---------------------------------------------------------------------------


def test_every_table_on_every_served_legal_route_is_wrapped_inside_legal_prose():
    client = _app().test_client()
    assert legal._LEGAL_PAGES, "legal._LEGAL_PAGES is empty -- nothing to cover"

    saw_at_least_one_table = False
    for path in legal._LEGAL_PAGES:
        resp = client.get(path)
        assert resp.status_code == 200, path
        soup = BeautifulSoup(resp.data.decode("utf-8"), "html.parser")

        for table in soup.find_all("table"):
            saw_at_least_one_table = True

            wrapper = table.parent
            assert (
                wrapper is not None
                and wrapper.name == "div"
                and "table-scroll" in (wrapper.get("class") or [])
            ), (path, 'a <table> is not a direct child of <div class="table-scroll">')

            assert wrapper.find_parent("div", class_="legal-prose") is not None, (
                path,
                "a table-scroll wrapper is not inside .legal-prose",
            )

    assert saw_at_least_one_table, (
        "no committed legal markdown file rendered any <table> at all -- "
        "this test would otherwise pass vacuously with nothing to cover "
        "(privacy.md is expected to contribute at least one)"
    )


# ---------------------------------------------------------------------------
# Keyboard accessibility, served page (issue #239, WCAG 2.1.1 + 2.4.7): the
# same wrapper the previous test checks structurally must also carry the
# attributes that make it reachable and named without a mouse -- checked on
# the real served response, not on _wrap_tables_for_scroll's return value in
# isolation, so this proves the production request/response path.
# ---------------------------------------------------------------------------


def test_every_table_scroll_wrapper_on_every_served_route_is_keyboard_focusable_and_named():
    client = _app().test_client()
    assert legal._LEGAL_PAGES, "legal._LEGAL_PAGES is empty -- nothing to cover"

    saw_at_least_one_wrapper = False
    for path in legal._LEGAL_PAGES:
        resp = client.get(path)
        assert resp.status_code == 200, path
        soup = BeautifulSoup(resp.data.decode("utf-8"), "html.parser")
        wrappers = soup.find_all("div", class_="table-scroll")
        tables = soup.find_all("table")
        assert len(wrappers) == len(tables), (
            path,
            "wrapper count must equal table count -- every table gets exactly one wrapper",
        )

        labels = []
        for wrapper in wrappers:
            saw_at_least_one_wrapper = True
            assert wrapper.get("tabindex") == "0", (
                path,
                'table-scroll wrapper has no tabindex="0" -- unreachable by keyboard (WCAG 2.1.1)',
            )
            assert wrapper.get("role") == "region", (
                path,
                'table-scroll wrapper has no role="region"',
            )
            label = wrapper.get("aria-label")
            assert label, (
                path,
                "table-scroll wrapper has no accessible name (WCAG 4.1.2)",
            )
            labels.append(label)

        assert len(labels) == len(set(labels)), (
            path,
            labels,
            "aria-labels must be unique within one served page",
        )

    assert saw_at_least_one_wrapper, (
        "no committed legal markdown file rendered any table-scroll wrapper "
        "at all -- this test would otherwise pass vacuously with nothing to cover"
    )


def test_served_legal_page_stylesheet_declares_table_scroll_overflow_x():
    """Checks the actual HTML each route serves (the <style> block
    legal_page.html emits inline), not the template file on disk --
    proving the rule that ships is the one a browser actually receives."""
    client = _app().test_client()
    assert legal._LEGAL_PAGES, "legal._LEGAL_PAGES is empty -- nothing to cover"

    for path in legal._LEGAL_PAGES:
        resp = client.get(path)
        assert resp.status_code == 200, path
        soup = BeautifulSoup(resp.data.decode("utf-8"), "html.parser")
        style_tag = soup.find("style")
        assert style_tag, (path, "no <style> tag in the served page")

        decls = _table_scroll_declarations(style_tag.get_text())
        assert decls is not None, (
            path,
            "no .legal-prose .table-scroll rule in the served stylesheet",
        )
        assert re.fullmatch(r"auto|scroll", decls.get("overflow-x", ""), re.IGNORECASE), (
            path,
            decls,
        )

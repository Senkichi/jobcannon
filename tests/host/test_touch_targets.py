"""jobcannon/web/templates/*.html — 44px touch-target floor (issue #179).

WCAG 2.5.5 / platform HIG: an interactive element's tap area needs to be
>=44px in the dimension that matters. Rather than trust a rendered-page
crawl (which only ever reaches ONE branch of each Jinja {% if %} per
request -- show_actions, `filters is defined`, g.clerk_user, `submitted`,
etc.), this scans template SOURCE, so every branch is covered regardless
of which route/auth-state/HX-Request combination would be needed to reach
it at runtime. The Playwright measurement in scratchpad/eng/fleet/bundle/
(and its predecessor, scratchpad/eng/fleet/mobile-targets/) covers the
reachable branches empirically; this test is what covers the rest and is
what keeps them covered as templates change.

No hardcoded element list: every <a>, <button>, non-hidden <input>,
<label>, <select>, and <textarea> opening tag in every
jobcannon/web/templates/*.html file must carry a `touch_target(...)` call
(jobcannon/web/template_globals.py, issue #207) inside its `class`
attribute:

  - `touch_target()` (default kind="block") for everything except
    checkbox/radio inputs. It renders `min-h-11` -- Tailwind's `min-h-*`
    scale gives a floor without clipping wrapped text (verified against
    the live cdn.tailwindcss.com build with Playwright: a `min-h-11` div
    renders at exactly 44px).
  - `touch_target('checkbox')` for `type="checkbox"`/`type="radio"`
    inputs. It renders `h-11 w-11` (both height AND width) --
    `min-height` alone doesn't touch a native checkbox's width, and its
    intrinsic box is otherwise ~16px square regardless of the wrapping
    <label>'s own size -- verified empirically (Playwright, both a
    Chromium and a WebKit engine) that a native checkbox DOES honor
    explicit `h-11 w-11` (renders 44x44), so the control's own rect
    clears the floor rather than relying solely on the label acting as a
    de facto larger target. Caveat: real desktop macOS Safari has a
    long-standing WebKit issue (webkit.org/b/148675) where native
    checkbox/radio chrome is AppKit-themed and ignores CSS width/height
    outright -- Playwright's WebKit build (used above) doesn't reproduce
    that native AppKit theming, so this isn't independently verified
    against real desktop Safari. iOS Safari is believed unaffected (its
    checkbox widget is vector-based, not the fixed-raster AppKit cell),
    but that's not independently confirmed either -- no real iOS device
    was available to test against.

Single-sourced via the Jinja global, not a literal Tailwind class string:
before #207, the floor was pasted as a literal `min-h-11`/`h-11 w-11`
token at each of 65 sites across 17 templates, with nothing catching a
site that silently reverted to a literal class (or never got the token in
the first place) except this test failing ONE parametrized case at a
time. The scan below now looks for the MARKER (a `touch_target(...)`
call), not the rendered Tailwind token, which also makes a future change
to the floor's actual pixel value a one-line edit in
jobcannon/web/template_globals.py instead of a 65-site find/replace.
`test_sabotage_a_real_template_site_and_confirm_the_guard_fails` (below)
verifies this the hard way: reverting one real template site from the
marker back to a literal `min-h-11` class -- exactly the regression #207
exists to prevent, and exactly what the OLD literal-substring guard would
have silently passed -- makes the corresponding parametrized case fail,
naming the exact file/tag/attrs.

`type="hidden"` inputs (CSRF tokens, carried-forward picker selections in
onboarding_picker.html) have no visual hit area and are excluded by their
`type` attribute, not by a filename allowlist.

Jinja `{# ... #}` comments are stripped before scanning: several templates
mention bare `<a href>` / etc. in prose inside comments (e.g.
_posting_row.html's Apply-control writeup), which would otherwise
false-positive as real, unstyled tags.

Attribute matching is quote-aware: `_TAG_RE`'s attribute span skips over
`"..."`- and `'...'`-quoted values rather than stopping at the first bare
`>`, so a quoted attribute value that itself contains a literal `>` (an
inline `hx-on:click="e => f(e)"` arrow function, for example) doesn't
truncate the tag match before a later `class`/`type` attribute is reached
(review-1 LOW #6c). `_CLASS_RE`/`_TYPE_RE` accept both single- and
double-quoted values for the same reason (review-1 LOW #6b). The scan
walks `jobcannon/web/templates/` recursively (review-1 LOW #6a) so a
future subdirectory isn't silently unscanned; there are no subdirectories
today. `_TOUCH_TARGET_RE` (below) is applied to the class attribute VALUE
after quote-extraction, so it works the same whether the call sits inside
a single Jinja expression on its own (`class="{{ touch_target() }} ..."`)
or alongside another one (postings_history.html's tab links:
`class="{{ 'a' if x else 'b' }} {{ touch_target() }} ..."` -- it's a
plain substring search over the raw, unrendered attribute text, so
neither Jinja expression needs to be evaluated or even be the only one
present).

Issue #222 adds two more tests below (not part of the tag/attribute scan
above): /terms's inline `[Privacy Policy](/privacy)` markdown link is
invisible to the source-scan approach entirely -- it's not written as an
HTML `<a>` tag in any *.html template, it's markdown body text rendered
by jobcannon/web/legal.py at import time. Those two tests close that
blind spot structurally instead: any `<a>` a committed
jobcannon/web/legal/*.md file's markdown renders into must land inside
`.legal-prose`, whose CSS (jobcannon/web/templates/legal_page.html) gives
every such link vertical padding that clears the 44px floor. See those
tests' own docstrings for why vertical (not horizontal) padding on an
inline (not inline-block) element is the right layer for this specific
case.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from jobcannon.web import legal
from jobcannon.web.template_globals import touch_target

_TEMPLATES_DIR = pathlib.Path("jobcannon/web/templates")
_LEGAL_PAGE_TEMPLATE = _TEMPLATES_DIR / "legal_page.html"
_LEGAL_MD_DIR = pathlib.Path("jobcannon/web/legal")

_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# The attribute span is quote-aware: a "..."/'...'-quoted attribute value may
# itself contain a literal '>' (e.g. an inline hx-on:click arrow function),
# which a bare `[^>]*` would truncate on. Alternate quoted-run | bare-char so
# a '>' inside quotes is consumed as part of the quoted run, not the closer.
_TAG_RE = re.compile(
    r"""<(a|button|input|label|select|textarea)\b((?:"[^"]*"|'[^']*'|[^>"'])*)>""",
    re.IGNORECASE,
)
# `(?<![\w-])` anchors each attribute name against a preceding word-char or
# hyphen (Devin LOW, review/review-devin.md finding 4): without it, a
# `data-class="..."`/`data-type="..."`-style attribute occurring BEFORE the
# real `class=`/`type=` on the same tag would satisfy `.search()` first (the
# unanchored pattern matches "class=" inside "data-class=" just as readily
# as a bare "class="), silently misclassifying or dropping the element. No
# template today has such a decoy attribute (grepped for `-class=`/`-type=`
# across jobcannon/web/templates/ -- zero hits), so this was a latent,
# fail-open gap rather than a live bug --
# test_class_and_type_regexes_are_anchored_against_decoy_dash_prefixed_attrs
# below proves the scan continues past a decoy to the real attribute.
_CLASS_RE = re.compile(r"""(?<![\w-])class\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_TYPE_RE = re.compile(r"""(?<![\w-])type\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
# Matches a `touch_target(...)` Jinja-global call inside a class attribute's
# raw source text -- `kind` is None for the bare/default-arg form
# (`touch_target()`), or the quoted argument's text for an explicit one
# (`touch_target('checkbox')` / `touch_target("checkbox")`).
_TOUCH_TARGET_RE = re.compile(
    r"""touch_target\(\s*(?:'([^']*)'|"([^"]*)")?\s*\)""",
)


def _attr_value(match: re.Match[str] | None) -> str | None:
    """First non-None capture group -- group(1) for a double-quoted match,
    group(2) for a single-quoted one (whichever alternative fired)."""
    if match is None:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def _class_value(attrs: str) -> str | None:
    return _attr_value(_CLASS_RE.search(attrs))


def _touch_target_kind(attrs: str) -> str | None:
    """Return the effective `touch_target(...)` kind found in `attrs`'s
    class attribute -- "block" for the bare/default-arg call, "checkbox"
    for an explicit `touch_target('checkbox')`, or None if no call is
    present at all (no marker -- the #207 regression this test exists to
    catch) or the call carries an argument that isn't a recognized kind
    (a typo'd kind name is a bug in the template, not a pass)."""
    value = _class_value(attrs)
    if value is None:
        return None
    match = _TOUCH_TARGET_RE.search(value)
    if match is None:
        return None
    arg = match.group(1) if match.group(1) is not None else match.group(2)
    if arg is None:
        return "block"
    if arg in ("block", "checkbox"):
        return arg
    return None


def _strip_comments(text: str) -> str:
    return _HTML_COMMENT_RE.sub("", _JINJA_COMMENT_RE.sub("", text))


def _iter_tags_in_text(filename: str, text: str):
    """Yield (filename, tag_name, attrs) for every matched opening tag in
    `text`. Takes source text directly (rather than reading from disk) so
    it's independently testable against a fixture string -- see
    test_collector_handles_quoted_arrow_fn_attrs_and_single_quotes below."""
    for match in _TAG_RE.finditer(_strip_comments(text)):
        yield filename, match.group(1).lower(), match.group(2)


def _iter_template_tags():
    for path in sorted(_TEMPLATES_DIR.rglob("*.html")):
        yield from _iter_tags_in_text(path.name, path.read_text(encoding="utf-8"))


def _collect_cases_from_tags(tags) -> list[tuple[str, str, str | None, str]]:
    cases = []
    for filename, tag_name, attrs in tags:
        input_type = None
        if tag_name == "input":
            input_type = (_attr_value(_TYPE_RE.search(attrs)) or "text").lower()
            if input_type == "hidden":
                continue
        cases.append((filename, tag_name, input_type, attrs))
    return cases


def _collect_cases():
    return _collect_cases_from_tags(_iter_template_tags())


_CASES = _collect_cases()
_CASE_IDS = [f"{i:03d}-{f}-{t}-{it or ''}" for i, (f, t, it, _a) in enumerate(_CASES)]


def test_touch_target_returns_the_documented_literal_tokens():
    """review-1 MED-1: the single-sourced floor itself had no return-value
    test -- every test below (and the whole rest of this suite) only checks
    that the `touch_target(...)` MARKER is present, never what it actually
    renders. Editing `_KIND_TOKENS["block"]` in template_globals.py to
    "min-h-10" would leave every marker-based case here green (65 sites
    still call the marker, still resolve to kind "block") while silently
    shipping a sub-44px floor at all 61 non-checkbox sites -- the exact
    #207 failure mode relocated one layer up to the single source this
    module's docstring already names but never tested. Literal expected
    values, never derived from `_KIND_TOKENS` itself, so an edit there
    can't drag its own test along with it."""
    assert touch_target() == "min-h-11"
    assert touch_target("block") == "min-h-11"
    assert touch_target("checkbox") == "h-11 w-11"
    with pytest.raises(ValueError):
        touch_target("radio")


def test_scan_found_a_plausible_number_of_interactive_elements():
    """Positive control for the scan itself (verification-ladder rule):
    if the glob or the tag regex silently broke, every parametrized
    assertion below would vacuously pass over zero cases. The true count
    as of #207 is exactly 65 (re-derive via the same `_collect_cases()`
    this module uses -- see the PR body for the derivation command); the
    floor is set at 60 rather than pinned to 65 so a template edit that
    adds or removes one or two elements doesn't require bumping this
    constant, while still catching a regression that silently dropped
    several elements, not just a scan finding nothing."""
    assert len(_CASES) >= 60, _CASES


@pytest.mark.parametrize("filename,tag_name,input_type,attrs", _CASES, ids=_CASE_IDS)
def test_interactive_element_meets_touch_target_floor(filename, tag_name, input_type, attrs):
    kind = _touch_target_kind(attrs)
    if tag_name == "input" and input_type in ("checkbox", "radio"):
        assert kind == "checkbox", (filename, tag_name, input_type, attrs)
    else:
        assert kind == "block", (filename, tag_name, input_type, attrs)


def test_collector_handles_quoted_arrow_fn_attrs_and_single_quotes():
    """Fixture-driven regression for review-1 LOW #6: a quoted attribute
    value containing a literal '>' (an inline hx-on:click arrow function --
    the shape feed-states' _posting_row.html is actively adding) must not
    truncate the tag match before a later `class` attribute is reached, and
    single-quoted `class`/`type` values must parse the same as
    double-quoted ones. Exercises the same `_iter_tags_in_text` /
    `_collect_cases_from_tags` collector the real scan uses, against a
    scratch string instead of the real templates.

    Also covers `<select>`/`<textarea>` positive-control: no `<textarea>`
    exists in the real templates today, so without this fixture the
    `textarea` arm of `_TAG_RE`'s alternation would be exercised by nothing
    at all -- a broken `textarea` match and a correct one are otherwise
    indistinguishable (both yield zero collected cases from the real scan)."""
    fixture = (
        '<a hx-on:click="e => f(e)" class="x">Link</a>\n'
        "<input type='checkbox' class='h-11 w-11'>\n"
        '<select class="min-h-11"><option>a</option></select>\n'
        '<textarea class="min-h-11"></textarea>\n'
    )
    tags = list(_iter_tags_in_text("fixture.html", fixture))
    assert [tag for _filename, tag, _attrs in tags] == [
        "a",
        "input",
        "select",
        "textarea",
    ]

    cases = _collect_cases_from_tags(tags)
    assert len(cases) == 4

    filename, tag_name, input_type, attrs = cases[0]
    assert (filename, tag_name, input_type) == ("fixture.html", "a", None)
    assert _class_value(attrs) == "x"

    filename, tag_name, input_type, attrs = cases[1]
    assert (filename, tag_name, input_type) == ("fixture.html", "input", "checkbox")
    assert _class_value(attrs) == "h-11 w-11"

    filename, tag_name, input_type, attrs = cases[2]
    assert (filename, tag_name, input_type) == ("fixture.html", "select", None)
    assert _class_value(attrs) == "min-h-11"

    filename, tag_name, input_type, attrs = cases[3]
    assert (filename, tag_name, input_type) == ("fixture.html", "textarea", None)
    assert _class_value(attrs) == "min-h-11"


def test_class_and_type_regexes_are_anchored_against_decoy_dash_prefixed_attrs():
    """Devin LOW (review/review-devin.md finding 4): `_CLASS_RE`/`_TYPE_RE`
    are `.search()`-based and, unanchored, would match "class="/"type=" as a
    substring of a preceding "data-class="/"data-type=" attribute on the
    same tag before ever reaching the real one -- a decoy attribute earlier
    on the tag could silently misclassify or drop an element from the scan.
    No template today has this shape, but the regex wasn't structurally
    hardened against it. Proves the fix: a `data-class="x"` (resp.
    `data-type="text"`) attribute placed BEFORE the real `class=`/`type=`
    must not be picked up -- the real attribute's value must still be the
    one returned."""
    decoy_class = 'data-class="x" class="{{ touch_target() }} shrink-0"'
    assert _class_value(decoy_class) == "{{ touch_target() }} shrink-0"

    decoy_type = 'data-type="text" type="checkbox" class="{{ touch_target(\'checkbox\') }}"'
    assert _attr_value(_TYPE_RE.search(decoy_type)) == "checkbox"


@pytest.mark.parametrize(
    "attrs,expected",
    [
        ('class="{{ touch_target() }} inline-flex items-center px-1"', "block"),
        ('class="{{ touch_target() }}"', "block"),
        ("class=\"{{ touch_target('checkbox') }} shrink-0\"", "checkbox"),
        # A double-quoted Jinja string argument needs a single-quoted HTML
        # class attribute to avoid ambiguity -- Jinja itself doesn't care
        # about HTML attribute quoting (it only parses {{ }} delimiters and
        # the Python-expression quoting inside them), but this test's
        # `_CLASS_RE` does, exactly like a real HTML/quote-aware scan would.
        # Every real template site in this repo uses the single-quoted form
        # above specifically to avoid this; this case exists to prove the
        # double-quoted spelling ALSO works once it's paired correctly, not
        # to claim `class="...touch_target("checkbox")..."` (mismatched
        # quotes) is something a template should ever write.
        ("class='{{ touch_target(\"checkbox\") }} shrink-0'", "checkbox"),
        (
            "class=\"{{ 'a' if token == view else 'b' }} {{ touch_target() }} inline-flex items-center px-1\"",
            "block",
        ),
        ('class="min-h-11 inline-flex items-center px-1"', None),  # the literal #207 regressed to
        ('class="h-11 w-11 shrink-0"', None),  # same, for the checkbox literal
        ('class=""', None),
        ("class=\"{{ touch_target('radio') }}\"", None),  # not a recognized kind
    ],
    ids=[
        "default-kind-with-siblings",
        "default-kind-alone",
        "checkbox-kind-single-quoted",
        "checkbox-kind-double-quoted-in-single-quoted-attr",
        "alongside-another-jinja-expression",
        "literal-min-h-11-no-marker",
        "literal-h-11-w-11-no-marker",
        "empty-class",
        "unrecognized-kind-argument",
    ],
)
def test_touch_target_kind_detection(attrs, expected):
    """Fixture-driven regression for `_touch_target_kind` itself, isolated
    from the real templates: covers the default-arg form, both quote
    styles for an explicit kind (correctly paired against the OTHER
    quote style on the enclosing class attribute -- see the comment above),
    a call sitting alongside another Jinja expression in the same class
    attribute (postings_history.html's tab links, the one real site with
    two `{{ }}` blocks in one `class="..."`), and the negative cases -- a
    literal Tailwind token with no `touch_target(...)` call at all (the
    exact shape a #207 regression, or the PRE-#207 guard's blind spot,
    would produce), and an argument that isn't a recognized kind name."""
    assert _touch_target_kind(attrs) == expected


def test_sabotage_a_real_template_site_and_confirm_the_guard_fails():
    """Sabotage-verify against a REAL template, not just the fixture above:
    reverts one real site (base.html's "Feed" nav link) from the
    `touch_target()` marker back to the literal `min-h-11` class it used to
    carry pre-#207 -- exactly the regression #207 exists to prevent, and
    exactly the shape the OLD literal-substring guard would have silently
    passed straight through. Runs the real collector against the sabotaged
    text (not the fixture regex in isolation) so it also proves the
    end-to-end wiring: `_iter_tags_in_text` -> `_collect_cases_from_tags` ->
    `_touch_target_kind` correctly flags it, not just that the kind-
    detection helper can be fooled in principle."""
    path = _TEMPLATES_DIR / "base.html"
    original = path.read_text(encoding="utf-8")
    marker = '<a href="/" class="hover:text-neutral-100 {{ touch_target() }} inline-flex items-center px-1">Feed</a>'
    literal = (
        '<a href="/" class="hover:text-neutral-100 min-h-11 inline-flex items-center px-1">Feed</a>'
    )
    assert marker in original, (
        "base.html's Feed link markup changed -- update this sabotage fixture"
    )
    sabotaged = original.replace(marker, literal, 1)
    assert sabotaged != original

    tags = list(_iter_tags_in_text("base.html", sabotaged))
    cases = _collect_cases_from_tags(tags)
    feed_cases = [
        (filename, tag_name, input_type, attrs)
        for (filename, tag_name, input_type, attrs) in cases
        if _class_value(attrs) == "hover:text-neutral-100 min-h-11 inline-flex items-center px-1"
    ]
    assert len(feed_cases) == 1, (
        "expected exactly the sabotaged Feed link to carry the literal class"
    )
    filename, tag_name, input_type, attrs = feed_cases[0]
    assert _touch_target_kind(attrs) is None, (
        "the sabotaged site must fail kind detection -- if this assertion "
        "fails, the guard would have silently passed a literal min-h-11 "
        "class with no touch_target() marker"
    )
    # And the real parametrized assertion shape, run inline here so a
    # break in test_interactive_element_meets_touch_target_floor's own
    # logic can't hide behind the sabotage never reaching it:
    with pytest.raises(AssertionError):
        assert _touch_target_kind(attrs) == "block", (filename, tag_name, input_type, attrs)


# ---------------------------------------------------------------------------
# Issue #222: /terms's inline markdown link ([Privacy Policy](/privacy)) is
# invisible to the source scan above -- it's not an HTML <a> tag in any
# *.html template at all, it's markdown body text rendered by
# jobcannon/web/legal.py at import time into `body_html`, which
# legal_page.html then drops in via `{{ body_html|safe }}`. These two tests
# close that blind spot structurally instead of by special-casing terms.md:
# every `<a>` any committed jobcannon/web/legal/*.md file renders into is
# proven to land inside `.legal-prose`, and `.legal-prose a`'s own CSS is
# proven to carry the vertical padding that clears the 44px floor for it.
# ---------------------------------------------------------------------------

_PROSE_A_RULE_RE = re.compile(r"\.legal-prose\s+a\s*\{([^}]*)\}")
_PADDING_TOP_RE = re.compile(r"padding-top\s*:\s*([0-9.]+)px", re.IGNORECASE)
_PADDING_BOTTOM_RE = re.compile(r"padding-bottom\s*:\s*([0-9.]+)px", re.IGNORECASE)


def _legal_prose_a_vertical_padding_px() -> tuple[float, float]:
    """Parse legal_page.html's `.legal-prose a { ... }` rule (NOT the
    sibling `.legal-prose a:hover { ... }` rule -- the trailing `\\s+a\\s*\\{`
    in `_PROSE_A_RULE_RE` requires the selector to end at `a`, so
    `.legal-prose a:hover`'s `a:hover` never matches it) and return its
    declared (padding-top, padding-bottom) in px. Raises (not returns 0.0)
    if either declaration is missing, so a CSS edit that silently drops one
    fails this test loudly instead of the guard quietly measuring 0px."""
    css = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    rule_match = _PROSE_A_RULE_RE.search(css)
    assert rule_match is not None, "legal_page.html has no `.legal-prose a { ... }` rule"
    body = rule_match.group(1)
    top_match = _PADDING_TOP_RE.search(body)
    bottom_match = _PADDING_BOTTOM_RE.search(body)
    assert top_match is not None, ".legal-prose a rule declares no padding-top"
    assert bottom_match is not None, ".legal-prose a rule declares no padding-bottom"
    return float(top_match.group(1)), float(bottom_match.group(1))


def test_legal_prose_link_padding_clears_the_touch_target_floor():
    """#222: `.legal-prose a` must declare BOTH padding-top and
    padding-bottom (a bare `padding: <n>px` shorthand, or only one of the
    two, would satisfy a weaker `"padding" in css` check while leaving the
    link's rendered height unchanged on one side) with a combined value
    that, added to the rendered line-box height a real browser measured
    (21px, Playwright/iPhone-13 viewport, /terms's Privacy Policy link --
    see the PR body), clears 44px with real margin rather than landing on
    the exact boundary. `>= 44` isn't used here specifically because
    `getBoundingClientRect` returns a float and 12px vertical padding
    (21 + 24 = 45) leaves under a pixel of slack against font-metric
    variance across browsers/engines -- the empirical Playwright
    measurement in scratchpad/eng/fleet/bundle/ is the actual floor proof
    for THIS browser's rendering; this static check guards the CSS
    declaration doesn't regress back toward that edge."""
    top_px, bottom_px = _legal_prose_a_vertical_padding_px()
    assert top_px > 0, "padding-top must be non-zero -- padding: 0 must not satisfy this guard"
    assert bottom_px > 0, (
        "padding-bottom must be non-zero -- padding: 0 must not satisfy this guard"
    )
    assert top_px + bottom_px >= 26, (
        top_px,
        bottom_px,
    )  # 21px content + this clears 44px with margin


def test_legal_page_wraps_body_html_in_the_padded_prose_container():
    """#222 structural check (1 of 2): legal_page.html's `{{ body_html|safe
    }}` -- the only place ANY legal .md file's rendered markdown reaches
    the page -- must be nested inside the SAME container the
    `.legal-prose a` CSS rule targets. Guards against the container div
    and the CSS class drifting apart (a rename of one without the other),
    which neither the CSS-declaration test above nor the render-coverage
    test below would catch on their own -- each only checks its own half."""
    html = _LEGAL_PAGE_TEMPLATE.read_text(encoding="utf-8")
    container_re = re.compile(
        r'<div\s+class="legal-prose">\s*\{\{\s*body_html\s*\|\s*safe\s*\}\}',
        re.DOTALL,
    )
    assert container_re.search(html), (
        "legal_page.html must render {{ body_html|safe }} directly inside "
        'a `<div class="legal-prose">` -- that\'s the container '
        ".legal-prose a's CSS rule targets"
    )


def test_every_legal_markdown_file_renders_links_covered_by_the_prose_container():
    """#222 structural check (2 of 2): renders every committed
    jobcannon/web/legal/*.md file through the SAME `legal._render()`
    function jobcannon/web/legal.py's routes call at import time (already
    used directly as a test seam by tests/host/test_legal_pages.py -- pure,
    no DB/Flask-app-context needed, matching this module's own
    zero-dependency philosophy), and asserts every `<a` the rendered HTML
    contains is a real markdown link (an `<a href=` tag, not a false
    positive from stray text). Combined with the two tests above -- the
    container test proving legal_page.html always wraps `body_html` in
    `.legal-prose`, and the CSS test proving that class's `a` rule clears
    the floor -- this means ANY future legal .md file that adds a link is
    covered by construction: nothing here hardcodes "terms.md" or
    "privacy.md" by name, so a third legal document added later is
    automatically in scope the moment it's glob-discovered.

    Also serves as the positive control for this whole #222 guard: if this
    found zero links, the two structural tests above would be proving
    something true about a container that never actually receives a real
    `<a>` tag from any legal document -- an empty result here would tell
    you nothing (verification-ladder rule). terms.md's
    `[Privacy Policy](/privacy)` line is expected to keep this non-empty;
    if a future edit removes it, this test failing is the signal to
    replace it with a different real link in a legal doc rather than
    silently letting the guard go vacuous."""
    md_files = sorted(_LEGAL_MD_DIR.glob("*.md"))
    assert md_files, "no jobcannon/web/legal/*.md files found -- scan itself is broken"

    total_links = 0
    for path in md_files:
        _title, html = legal._render(path.name)
        links = re.findall(r"<a\s+href=", html, re.IGNORECASE)
        total_links += len(links)

    assert total_links >= 1, (
        "expected at least one real <a href=...> link across all committed "
        "legal markdown files (terms.md's Privacy Policy link) -- if this "
        "is genuinely 0, the structural guard above has nothing to cover "
        "and #222's regression is no longer reachable, but that needs "
        "confirming, not assuming"
    )

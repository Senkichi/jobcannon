"""jobcannon/web/templates/*.html — 44px touch-target floor (issue #179).

WCAG 2.5.5 / platform HIG: an interactive element's tap area needs to be
>=44px in the dimension that matters. Rather than trust a rendered-page
crawl (which only ever reaches ONE branch of each Jinja {% if %} per
request -- show_actions, `filters is defined`, g.clerk_user, `submitted`,
etc.), this scans template SOURCE, so every branch is covered regardless
of which route/auth-state/HX-Request combination would be needed to reach
it at runtime. The Playwright measurement in
scratchpad/eng/fleet/mobile-targets/ covers the reachable branches
empirically; this test is what covers the rest and is what keeps them
covered as templates change.

No hardcoded element list: every <a>, <button>, non-hidden <input>,
<label>, <select>, and <textarea> opening tag in every
jobcannon/web/templates/*.html file must carry the shared marker class in
its `class` attribute:

  - `min-h-11` for everything except checkbox/radio inputs. Tailwind's
    `min-h-*` scale gives a floor without clipping wrapped text (verified
    against the live cdn.tailwindcss.com build with Playwright: a
    `min-h-11` div renders at exactly 44px).
  - `h-11` AND `w-11` (both) for `type="checkbox"`/`type="radio"` inputs.
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
today.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_TEMPLATES_DIR = pathlib.Path("jobcannon/web/templates")

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
_CLASS_RE = re.compile(r"""class\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_TYPE_RE = re.compile(r"""type\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)


def _attr_value(match: re.Match[str] | None) -> str | None:
    """First non-None capture group -- group(1) for a double-quoted match,
    group(2) for a single-quoted one (whichever alternative fired)."""
    if match is None:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def _class_tokens(attrs: str) -> set[str]:
    value = _attr_value(_CLASS_RE.search(attrs))
    if value is None:
        return set()
    return set(value.split())


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


def test_scan_found_a_plausible_number_of_interactive_elements():
    """Positive control for the scan itself (verification-ladder rule):
    if the glob or the tag regex silently broke, every parametrized
    assertion below would vacuously pass over zero cases. Tightened from
    the original `>= 30` (review-1 LOW #4/#204) to `>= 45` -- the current
    true count is 52 (the original 48 <a>/<button>/<input>/<label> cases
    plus the 4 <select> elements #204 added to the scan's tag inventory),
    so this still catches a regression that silently dropped several
    elements, not just a scan finding nothing."""
    assert len(_CASES) >= 45, _CASES


@pytest.mark.parametrize("filename,tag_name,input_type,attrs", _CASES, ids=_CASE_IDS)
def test_interactive_element_meets_touch_target_floor(filename, tag_name, input_type, attrs):
    tokens = _class_tokens(attrs)
    if tag_name == "input" and input_type in ("checkbox", "radio"):
        assert "h-11" in tokens and "w-11" in tokens, (filename, tag_name, input_type, attrs)
    else:
        assert "min-h-11" in tokens, (filename, tag_name, input_type, attrs)


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
    assert _class_tokens(attrs) == {"x"}

    filename, tag_name, input_type, attrs = cases[1]
    assert (filename, tag_name, input_type) == ("fixture.html", "input", "checkbox")
    assert _class_tokens(attrs) == {"h-11", "w-11"}

    filename, tag_name, input_type, attrs = cases[2]
    assert (filename, tag_name, input_type) == ("fixture.html", "select", None)
    assert _class_tokens(attrs) == {"min-h-11"}

    filename, tag_name, input_type, attrs = cases[3]
    assert (filename, tag_name, input_type) == ("fixture.html", "textarea", None)
    assert _class_tokens(attrs) == {"min-h-11"}

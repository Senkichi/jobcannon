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
"""

from __future__ import annotations

import pathlib
import re

import pytest

_TEMPLATES_DIR = pathlib.Path("jobcannon/web/templates")

_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<(a|button|input|label|select|textarea)\b([^>]*)>", re.IGNORECASE)
_CLASS_RE = re.compile(r'class\s*=\s*"([^"]*)"', re.IGNORECASE)
_TYPE_RE = re.compile(r'type\s*=\s*"([^"]*)"', re.IGNORECASE)


def _class_tokens(attrs: str) -> set[str]:
    match = _CLASS_RE.search(attrs)
    if not match:
        return set()
    return set(match.group(1).split())


def _strip_comments(text: str) -> str:
    return _HTML_COMMENT_RE.sub("", _JINJA_COMMENT_RE.sub("", text))


def _iter_template_tags():
    for path in sorted(_TEMPLATES_DIR.glob("*.html")):
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for match in _TAG_RE.finditer(text):
            yield path.name, match.group(1).lower(), match.group(2)


def _collect_cases():
    cases = []
    for filename, tag_name, attrs in _iter_template_tags():
        input_type = None
        if tag_name == "input":
            type_match = _TYPE_RE.search(attrs)
            input_type = type_match.group(1).lower() if type_match else "text"
            if input_type == "hidden":
                continue
        cases.append((filename, tag_name, input_type, attrs))
    return cases


_CASES = _collect_cases()
_CASE_IDS = [f"{i:03d}-{f}-{t}-{it or ''}" for i, (f, t, it, _a) in enumerate(_CASES)]


def test_scan_found_a_plausible_number_of_interactive_elements():
    """Positive control for the scan itself (verification-ladder rule):
    if the glob or the tag regex silently broke, every parametrized
    assertion below would vacuously pass over zero cases. Comfortably
    below the current true count (48 across the templates directory) but
    high enough to catch the scan finding nothing."""
    assert len(_CASES) >= 30, _CASES


@pytest.mark.parametrize("filename,tag_name,input_type,attrs", _CASES, ids=_CASE_IDS)
def test_interactive_element_meets_touch_target_floor(filename, tag_name, input_type, attrs):
    tokens = _class_tokens(attrs)
    if tag_name == "input" and input_type in ("checkbox", "radio"):
        assert "h-11" in tokens and "w-11" in tokens, (filename, tag_name, input_type, attrs)
    else:
        assert "min-h-11" in tokens, (filename, tag_name, input_type, attrs)

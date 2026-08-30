"""jobcannon/web/template_globals.py — Jinja globals shared across templates.

`touch_target()` (issue #207) single-sources the CSS class(es), defined in
jobcannon/web/static/jc.css (44px floor), that satisfy the touch-target
floor enforced by tests/host/test_touch_targets.py, so the floor itself
lives in exactly one place instead of being pasted as a literal string at
every interactive-element site (63 bare + 4 `'checkbox'` call sites across
20 templates as of the Living Journal adoption — a change to the floor
value previously meant editing all of them by hand, with nothing catching
a missed site except the guard test failing one-by-one).

Deliberately emits ONLY the floor token(s) — `jc-touch` for a normal
element, `jc-touch-box` for a checkbox/radio — not a wider class bundle:
the surrounding class(es) vary legitimately per site (a nav link wants
`jc-nav-link`, a submit button wants `jc-btn`, a `<select>`/bare `<input>`
wants `jc-input`, a `<label>` wrapping a checkbox wants none at all), and
forcing them into one shared bundle would either change real rendered
layout at several sites or require a second, hand-maintained
kind-name -> class-shape taxonomy that #207 exists to get away from — see
the PR body for the exact per-site class-shape breakdown that ruled this
out.

Each remaining literal class on a converted site is therefore an
intentional, site-specific choice, left as a plain sibling token next to
the `{{ touch_target() }}` call — not something this module should also
own.
"""

from __future__ import annotations

_KIND_TOKENS = {
    "block": "jc-touch",
    "checkbox": "jc-touch-box",
}


def touch_target(kind: str = "block") -> str:
    """Return the CSS class(es) (defined in jobcannon/web/static/jc.css)
    that satisfy the 44px touch-target floor for `kind`.

    - "block" (default): every interactive element except a checkbox/radio
      input — `<a>`, `<button>`, a non-checkbox `<input>`, `<label>`,
      `<select>`, `<textarea>`.
    - "checkbox": `type="checkbox"`/`type="radio"` inputs, which need
      explicit height AND width — a native checkbox's intrinsic box is
      ~16px square regardless of its wrapping `<label>`'s own size (see
      tests/host/test_touch_targets.py's module docstring for the
      Playwright verification behind that).

    Raises on any other `kind` rather than silently returning an empty
    string, so a typo'd call fails immediately (at template-render time in
    dev, and via tests/host/test_touch_targets.py at collection time)
    instead of quietly shipping a sub-44px element.
    """
    try:
        return _KIND_TOKENS[kind]
    except KeyError:
        raise ValueError(
            f"touch_target: unknown kind {kind!r} (expected one of {sorted(_KIND_TOKENS)})"
        ) from None

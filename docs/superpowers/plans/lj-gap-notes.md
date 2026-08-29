# Living Journal adoption — gap notes

Append one bullet per gap found during wave-1 restyle. Task 13 resolves each
and deletes this file.

- **Task 12 / legal_page.html**: the `<style>` block's own documentation
  comments reference GitHub issue numbers in `(#222)` / `(#207)` shorthand
  (pre-existing, predates this migration — see commit history for PR #227).
  `test_design_templates.py`'s `test_templates_have_no_color_literals` regex
  (`#[0-9a-fA-F]{3,8}\b`) matches `#222`/`#207` as false-positive hex-color
  literals since both are valid hex digit strings. Not a live color value —
  no CSS declaration uses these. Needs either a regex refinement (e.g.
  require the match to sit inside a CSS declaration, not a `/* ... */`
  comment) or rewording the two comments to a non-hex-shaped issue reference
  (e.g. "GH-222" — NOT "issue #222", which is a `legal_guard.FORBIDDEN_PHRASES`
  entry scanned by `test_legal_pages.py` against this same `<style>` block).

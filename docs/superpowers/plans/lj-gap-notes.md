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

- **Task 8 / demo.html, preview.html — `.jc-cta` has no nested-link treatment**:
  `data-demo-cta` and `data-signup-cta` are `<p>` elements containing plain
  text plus one or two `<a>` tags, not a single clickable element — the shape
  `.jc-cta` (full-width, `background: var(--lj-ink); color: var(--lj-page)`)
  was designed for doesn't match. Per the closed vocabulary's own "underline
  in-content link -> `jc-link`" mapping, the nested anchors got `jc-link`
  (`color: var(--lj-ink)`), which sits on `.jc-cta`'s ink background —
  ink-on-ink, effectively invisible. No existing class supplies
  `color: var(--lj-page)` on an anchor, and inventing one or editing jc.css is
  out of scope for Task 8. Task 13 should add a scoped override (e.g.
  `.jc-cta a { color: inherit; text-decoration: underline; }`) or a new
  page-colored link variant.

- **Task 8 / demo.html — no inline-emphasis class for numbers in `jc-lede` body
  text**: the populated guest-feed paragraph highlighted `{{ stats.postings }}`
  and `{{ stats.companies }}` via `<strong class="text-neutral-100">` (brighter
  than the surrounding `text-neutral-400` body). The closed vocabulary has no
  class for "emphasized inline span within lede/note text" — `jc-index` /
  `jc-meta-num` are tied to the feed-row numbering idiom, a different context.
  Left the `<strong>` tags classless (bold via the element itself, color
  inherits `jc-lede`'s gray-text) rather than inventing a class. Task 13 could
  add a small `jc-emphasis`-style token if this contrast loss matters.

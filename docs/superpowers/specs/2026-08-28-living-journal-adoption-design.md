# Living Journal adoption — UI rework design

**Date:** 2026-08-28
**Status:** Approved design, pending implementation plan
**Scope:** the entire `jobcannon/web/` template surface (~25 templates) and its styling stack

## 1. Summary

Jobcannon's web UI adopts **Living Journal** — swole's design system — as a
**full identity adoption**: paper/lamplit-espresso surfaces, Fraunces + Inter
typography, ink hairlines instead of shadows, strictly-semantic green, and the
ledger/stamp/masthead component grammar, adapted to job-feed content. The
Tailwind Play CDN (dev-only tooling currently shipped to production) and the
unpkg htmx CDN are replaced by a self-contained static asset layer generated
from vendored design tokens.

Living Journal's canonical sources live in the (private) swole repo at
`docs/design/living-journal/`: `DESIGN-SYSTEM.md` (spec), `tokens.json`
(W3C design-tokens format), `reference.html` (approved visual reference).
The system is explicitly project-agnostic; jobcannon is its second consumer
(after swole itself and the React `living-journal` package).

## 2. Decisions (settled during brainstorming)

| Question | Decision |
|---|---|
| Fidelity | **Full identity adoption** — the whole LJ grammar, not a palette reskin |
| Theming | **Both themes, follow `prefers-color-scheme`** — Paper (light) default, Lamplit Paper (dark) via media query; no toggle, no JS, no persistence |
| Semantic green means | **Honesty signals only** — verified-fresh stamps, provenance/dedup marks, the "why" rank marks, and the one accent rule per screen. Actions (save/dismiss/apply) are **ink**, never green |
| Private→public boundary | **Vendor tokens + CSS** into jobcannon (AGPL) with provenance headers and a drift check; no dependency on the private swole repo; re-sync is a deliberate manual act |
| CSS architecture | **Token-generated custom CSS, Tailwind dropped entirely** (Approach A) — LJ's own styling idiom (`var(--lj-*)` everywhere, no utility classes, no inline hex) |

## 3. Architecture — the token pipeline

New pieces in this repo:

```
jobcannon/web/design/tokens.json      # vendored from swole (see §8), provenance header
scripts/gen_design_css.py             # tokens.json → static/lj-tokens.css (deterministic)
jobcannon/web/static/lj-tokens.css    # GENERATED — committed, drift-checked (test §7.1)
jobcannon/web/static/jc.css           # hand-written: ported LJ primitives + jc-* components
jobcannon/web/static/fonts/           # Fraunces + Inter variable woff2 + their OFL licenses
jobcannon/web/static/htmx.min.js      # vendored htmx 2.0.4 + its license file (replaces the unpkg CDN tag)
docs/design/living-journal.md         # jobcannon's identity rules + re-sync procedure (§7.6, §8)
```

- **Generator contract.** `gen_design_css.py` reads the vendored W3C tokens
  (groups: `color.light` / `color.dark`, `typography`, `stroke`, `radius`,
  `motion`) and emits the `--lj-*` custom-property set using the **canonical
  names already established** by the LJ package's `src/theme/base.css`
  (`--lj-page`, `--lj-card`, `--lj-card2`, `--lj-ink`, `--lj-gray`,
  `--lj-gray-text`, `--lj-green`, `--lj-green-text`, `--lj-rule`,
  `--lj-rule-2`, `--lj-hair`, `--lj-hair-soft`, `--lj-equipment`,
  `--lj-serif`, `--lj-sans`, `--lj-spring`, …). Output is deterministic
  (stable ordering, fixed formatting) so the drift test can assert
  byte-equality. The token-path → variable-name mapping is **explicit
  config inside the generator** — it cannot be derived mechanically,
  because base.css's names diverge from the token tree (`inkSecondary` →
  `--lj-gray`, `hairline` → `--lj-hair`, …). The drift test makes that
  config self-verifying: every token consumed, every canonical name
  emitted. A few `--lj-*` variables exist only in base.css with no
  tokens.json source (`--lj-shade`, `--lj-sheen`, `--lj-tan`); whichever
  of these a ported primitive needs ride as a supplemental set in the same
  generator config (values ported from base.css, provenance-commented) so
  they land in the generated file and `jc.css` stays free of color
  literals (§7.2). The vendored tokens.json stays a verbatim copy.
- **Theme mechanism.** `lj-tokens.css` defines the light (Paper) values on
  `:root` and redefines the same set under
  `@media (prefers-color-scheme: dark)` (Lamplit Paper). Pure CSS: no
  theme JS, no FOUC, nothing to persist.
- **Contrast tiers ship with the tokens.** The paired `-text` tokens
  (`--lj-gray-text`, `--lj-green-text`) are the body-text-safe (4.5:1)
  variants; the base tokens are the 3:1 tier for large text/icons/strokes.
  The rule from swole's conventions applies as written: body-size secondary
  text always uses the `-text` variant.
- **Fonts self-hosted.** Fraunces (display + every numeral that matters,
  tabular figures) and Inter (body), vendored as variable woff2 with their
  SIL OFL license files alongside. No Google Fonts CDN call — jobcannon has
  a privacy/consent posture and a per-visitor third-party font fetch would
  undercut it. Fallback stacks per the LJ spec: Georgia/serif, system sans.
- **`base.html` asset swap.** The `cdn.tailwindcss.com` and unpkg htmx
  `<script>` tags are removed; in their place: two `<link>` tags
  (`lj-tokens.css`, `jc.css`) and a local `<script src>` for the vendored
  htmx. The Clerk script is untouched (it is functional, not styling).
- **`touch_target()` keeps its exact contract.** Only the `_KIND_TOKENS`
  map in `jobcannon/web/template_globals.py` changes:
  `"block": "min-h-11"` → `"block": "jc-touch"`,
  `"checkbox": "h-11 w-11"` → `"checkbox": "jc-touch-box"`, with the 44px
  floor implemented in `jc.css`. All 65+ call sites are untouched; the
  Playwright guard test (`tests/host/test_touch_targets.py`) remains the
  behavioral enforcement of the floor.

## 4. Identity mapping — LJ grammar → job-feed content

The current centered `max-w-3xl` single column **stays** — it is the journal
page. Cool neutrals are eliminated everywhere; surfaces become
Paper/Lamplit-espresso, 1.5px ink hairlines replace borders, no elevation
shadows. Layout rhythm per the LJ spec: gutters 16–20px, sections 20–28px,
right edge is the data edge, left edge is the naming edge.

- **Masthead.** Page titles ("Your preview feed", "My postings") in
  Fraunces. The feed masthead carries the screen's **one** green accent rule
  (4px, draws on in ~600ms). The header wordmark "Job Cannon" is Fraunces;
  nav links become small-caps Inter labels (`FEED · DEMO · MY POSTINGS`),
  as do footer links.
- **Feed rows = the ledger pattern.** Numbered index column (`01`–`NN`) —
  the rank, which in jobcannon is meaningful, not decoration. Job title +
  company/scheme line at the naming edge. The numeral that matters —
  posting age (`2d`) — right-aligned in Fraunces at the data edge with a
  small-caps unit line beneath. Rows separated by soft rules
  (`--lj-rule`), not card-per-row.
- **Stamp badges for provenance.** LJ's outlined small-caps pill with a
  leading dot, perfectly level: source marks (`LEVER · POSTED 2D`) in ink.
  A **verified-fresh stamp renders in green** — honesty family — via
  `--lj-green-text`: stamp text is small-caps *small* text, so the 4.5:1
  tier applies, not the 3:1 base token. Repost/dedup annotations stay ink.
- **The "why" chips.** Rank-explanation chips — the `data-why-chips` list
  that `why_chips()` (jobcannon/web/why.py) feeds into `_posting_row.html`;
  there is **no separate panel template** — are green (honesty family,
  `--lj-green-text` at chip size), restyled as small-caps marks under a
  small-caps `WHY THIS RANK` label.
- **Actions in ink.** Save/dismiss are ink-ringed round buttons (ported
  `.lj-rbtn` geometry); Apply and other primary CTAs are the LJ primary
  CTA: ink-filled bar with paper text in light, cream-filled with ink text
  in dark. Saved/applied state marks are **drawn ink checks — not green**
  (green ≠ actions in jobcannon). Nothing breathes in v1.
- **Errors have no red.** LJ's palette is ink, paper, green — and forbids a
  second accent. The error toast (`#jc-error-toast`) becomes an
  ink-outlined stamp-style banner (small-caps, e.g.
  `COULD NOT VERIFY · REFRESH AND RETRY`). Destructive flows (account
  delete) carry weight through ink-filled emphasis and copy, not color.
- **Forms** (onboarding picker, consent panel): box-header small-caps
  labels, hairline-bounded inputs, checkboxes as ink-ringed squares whose
  check strokes in on selection.
- **Empty states** speak journal — a quiet line of copy
  (`_corpus_empty.html` already almost is one).
- **Motion belongs to ink.** Surfaces never translate/scale. Permitted
  motion: the masthead accent rule's draw-on, drawn ink checks on
  save/dismiss state changes, all on `--lj-spring` / LJ timing.
  `prefers-reduced-motion` disables every draw-on animation.

### Class conventions

- Ported LJ primitives keep their shipped names where reused near-verbatim:
  `.lj-card`, `.lj-label`, `.lj-rbtn`, `.lj-step` (and a root class on
  `<body>` for base page styling). **No new `lj-`-prefixed names may be
  invented** — that prefix means "part of the design system."
- Everything jobcannon-specific is `jc-*` (`jc-ledger-row`, `jc-stamp`,
  `jc-masthead`, `jc-toast`, `jc-touch`, …), composed exclusively from
  `var(--lj-*)` tokens.

## 5. Template migration — restyle, don't rewire

The templates are dense with load-bearing behavior. The migration is a
**class/markup-layer change with hard invariants**:

**Preserved verbatim:** every `hx-*` attribute, `data-*` hook, element `id`,
ARIA attribute, `{{ touch_target() }}` call site, all Jinja logic and gating
(including the `visitor_is_authed`-vs-`g.clerk_user` distinctions, issue
#205), the CSRF meta-tag/hx-headers split, the `HX-CSRF-Error` toast path,
and every behavioral comment. Structural wrappers may be added only where a
component needs one (e.g. the ledger index column), never re-plumbed.

**Migration order** (shared chrome first, so every later page lands on
finished ground):

1. Token/asset layer + `base.html` — header, nav, footer, toast, fonts.
2. Feed family: `_feed_page` / `_feed_content` / `_feed_list`,
   `_posting_row` (which contains the why chips), `_corpus_empty`, `feed.html`,
   `preview.html`, `demo.html`, `postings_history.html` +
   `_postings_history_list.html`.
3. Onboarding picker: `onboarding_picker.html`, `_picker_options`,
   `_picker_error_fragment`, `_picker_submitted`.
4. Consent pair (`consent.html`, `consent_signed_out.html`,
   `_consent_panel.html`), `account_delete.html`, `account_deleted.html`.
   (`/account/export` has no template — it returns a raw JSON `Response` —
   so nothing to migrate there.)
5. Error pages (`error.html`, `error_401.html`, `error_csrf.html`,
   `_csrf_error_fragment.html`) and `legal_page.html` — whose one inline
   `<style>` block is rebuilt on tokens.

**One pass, whole surface.** Full identity adoption means no half-migrated
state ships: the Tailwind CDN tag is removed in the same change that
converts the last template. Work lands as a single PR (or stacked PRs that
only merge together).

## 6. Non-goals

- No route, endpoint, or behavior changes; no copy changes beyond
  label-casing where the design demands small-caps.
- No theme toggle or theme persistence (system preference only).
- No new pages — including no styleguide page; swole's `reference.html`
  plus visual review suffices.
- Clerk's hosted UI stays Clerk's.
- No React, no node toolchain, no Tailwind build.
- No changes to the swole repo or the living-journal package.

## 7. Enforcement & testing

Structural guards, all pytest unless noted:

1. **Token drift test** — regenerates `lj-tokens.css` from the vendored
   `tokens.json` via `gen_design_css.py` and asserts byte-equality with the
   committed file. A stale regen or hand-edit fails CI.
2. **No color literals** — asserts zero hex/`rgb()`/`hsl()`/`oklch()`
   literals in `jc.css` and in any template `class`/`style` attribute;
   every color arrives via `var(--lj-*)`. (`lj-tokens.css` is generated and
   exempt — it is where the literals are *supposed* to live.)
3. **Class-closure test** — the Tailwind-removal guard with no
   hand-maintained list: parse the class names **defined** in `jc.css`,
   parse every class **referenced** across templates (including
   `touch_target()`'s emissions), assert referenced ⊆ defined. Leftover
   Tailwind utilities and typo'd classes fail by construction; the allowed
   set derives from the stylesheet itself.
4. **Existing host tests** — `tests/host/test_touch_targets.py` runs
   unchanged and re-verifies rendered geometry post-migration. The rest of
   `tests/host/` is audited during planning for assertions on Tailwind
   class strings and adjusted alongside the templates they test.
5. **Visual verification** — Playwright screenshots of the key pages
   (feed/preview, demo, picker, postings history, consent, one error page)
   in both themes (via `prefers-color-scheme` emulation) produced as review
   artifacts and compared by eye against swole's `reference.html`. No
   pixel-diff CI (too flaky to earn its keep).
6. **The un-testable rules get a home** — `docs/design/living-journal.md`
   (plus a new row in the existing `docs/design/README.md` binding-docs
   index, which already serves exactly this purpose) records
   the jobcannon-specific identity decisions (green = honesty signals only;
   errors are ink, never red; Fraunces numerals = rank + posting age; one
   accent rule per screen; no new `lj-` class names) plus vendoring
   provenance and the re-sync procedure. Future sessions inherit the rules
   from the repo, not from memory.

Contrast (AA) is inherited from the LJ tokens, which encode both WCAG tiers
explicitly (§3); no separate contrast tooling is added.

## 8. Provenance & re-sync

- Vendored from swole (private, `github.com/Senkichi/swole`) at commit
  `163da4fd9c1d42dc57fe125e0165da9cec680ca8` (tokens.json last touched
  2026-07-30). The vendored file carries this provenance in a header
  comment; vendored files are the user's own work and are licensed under
  this repo's AGPL-3.0-or-later like everything else here (fonts keep
  their own OFL license files).
- **Re-sync procedure** (deliberate, manual): copy the new `tokens.json`
  from swole, update the provenance header, run
  `python scripts/gen_design_css.py`, review the diff, commit both files
  together. The drift test makes a partial sync unrepresentable.

## 9. Open items for the implementation plan

- Confirm the Flask app's static-file configuration (`jobcannon/web/`
  currently has no `static/` directory; verify `static_folder` /
  `static_url_path` and cache headers — note
  `tests/host/test_public_cache_headers.py` just gained assertions on main).
- Acquire and subset the Fraunces + Inter variable woff2 files; record
  exact versions and the subsetting command in `docs/design/living-journal.md`.
- Sweep `tests/host/` (and any template-string unit tests) for Tailwind
  class assertions.
- Decide the exact drawn-check/draw-on CSS technique (dash-offset on inline
  SVG per LJ convention) during the base-layer step.

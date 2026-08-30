# Living Journal design identity (binding)

jobcannon's web UI adopts the Living Journal design system as its full visual
identity (spec: `docs/superpowers/specs/2026-08-28-living-journal-adoption-design.md`).
This doc binds every future template or stylesheet change.

## Identity rules

1. **Green is an honesty signal, nothing else.** `--lj-green` /
   `--lj-green-text` may color only: verified-fresh stamps, why-rank chips, and
   the page masthead's accent rule. Actions (save/dismiss/apply), errors, links,
   and emphasis are ink. Small/small-caps text uses `--lj-green-text` (4.5:1
   tier), never `--lj-green` (3:1 tier — large text, icons, strokes only).
2. **One green accent per screen.** A page gets at most one accent rule.
3. **Paper never moves.** No transform/translate/scale animation on surfaces.
   Motion belongs to ink: draw-on via SVG `stroke-dashoffset` + the `lj-draw`
   keyframe (600ms–1s, `--lj-spring`). Nothing breathes (the reference CTA's
   `lj-breathe` is deliberately omitted). Under `prefers-reduced-motion`, every
   animation must resolve to its fully-drawn end state.
4. **Contrast tiers.** Body-size secondary text uses `--lj-gray-text`;
   `--lj-gray` is for large text/strokes only. Same split for green (above).
5. **Warm neutrals only.** No color literal may appear outside the generated
   `lj-tokens.css` — enforced by `tests/test_design_css.py` (stylesheets) and
   the template scan in `tests/test_design_templates.py`. The one exception is
   `legal_page.html`'s inline `<style>` block, which host tests require to stay
   inline; it composes from `var(--lj-*)`.
6. **Naming.** `lj-*` classes/variables are ported design-system primitives —
   never invent a new `lj-*` name. jobcannon-specific compositions are `jc-*`
   and compose exclusively from `var(--lj-*)`. The class vocabulary is closed:
   templates may only reference classes defined in `jc.css`
   (`tests/test_design_templates.py` enforces closure).
7. **Both themes ride `prefers-color-scheme`** — light (Paper) on `:root`, dark
   (Lamplit Paper) via media query. No theme toggle, no `data-lj-theme`
   attribute (that mechanism belongs to the source app).

## Token pipeline

`jobcannon/web/design/tokens.json` (vendored, verbatim + a `$vendored` key) →
`scripts/gen_design_css.py` → `jobcannon/web/static/lj-tokens.css` (generated;
custom properties only). The variable names come from the LJ package's
`base.css`, whose names diverge from the token tree (`inkSecondary` →
`--lj-gray`, `hairline` → `--lj-hair`) and which defines a few variables with
no token source; those are PORT entries in the generator's `VAR_MAP`. Every
token leaf must be consumed by `VAR_MAP` or listed in `REFERENCE_ONLY` —
generation fails loudly otherwise. `tests/test_design_tokens.py` asserts the
committed CSS is byte-identical to regeneration.

## Vendored assets and provenance

| Asset | Source | Pinned at |
|---|---|---|
| `jobcannon/web/design/tokens.json` | swole (private) `docs/design/living-journal/tokens.json` | commit `163da4fd9c1d42dc57fe125e0165da9cec680ca8` |
| `jc.css` ported recipes (`.lj-root`, `.lj-label`, card/stamp/CTA/accent-rule/check) | swole `packages/living-journal/src/theme/base.css`, `.../components/Stepper/Stepper.css`, `docs/design/living-journal/reference.html` | same commit |
| `jobcannon/web/static/fonts/fraunces-var.woff2`, `fraunces-italic-var.woff2` | google/fonts `ofl/fraunces/Fraunces[SOFT,WONK,opsz,wght].ttf` (+ Italic) | commit `ade3d1533e06b2b1462ffcde8e08b129627ca360` |
| `jobcannon/web/static/fonts/inter-var.woff2` | google/fonts `ofl/inter/Inter[opsz,wght].ttf` | same commit |
| `jobcannon/web/static/htmx.min.js` | unpkg `htmx.org@2.0.4/dist/htmx.min.js`, sha256 cross-checked against jsdelivr's copy | sha256 `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `jobcannon/web/static/htmx.LICENSE.txt` | github `bigskysoftware/htmx` tag `v2.0.4` `LICENSE` | — |

The authoritative swole pin is the one recorded in `tokens.json`'s `$vendored`
key and the generator header; the rows above must match it.

Font subset recipe (re-run to refresh):

```
uvx --from "fonttools[woff]" pyftsubset <family>.ttf --flavor=woff2 \
  --output-file=<family>.woff2 --layout-features='*' \
  --unicodes="U+0000-017F,U+2013-2014,U+2018-2019,U+201C-201D,U+2022,U+2026,U+20AC,U+2212"
```

`--layout-features='*'` keeps `tnum` (tabular numerals — load-bearing for
`.jc-meta-num`/`.jc-index`); verify `fvar`/`gvar` survive with
`uvx --from "fonttools[woff]" fonttools ttx -l <file>.woff2`.

## Deliberate divergences from the source system

- Dark `--lj-hair`: tokens.json's hairline `#EDE5D824` (α .14) supersedes
  base.css's `.17` — the token wins on regeneration; do not "fix" it back.
- Panel radius: `.jc-panel` uses the package card recipe's 19px, not
  `radius.card`'s 16 — the component recipe wins over the token constant.
- `lj-breathe` omitted everywhere (identity rule 3).
- Theme switching via `prefers-color-scheme` instead of `[data-lj-theme]`.

## Re-sync procedure

1. Re-copy swole's `tokens.json` verbatim over `jobcannon/web/design/tokens.json`,
   re-adding the `$vendored` key with the new commit sha.
2. `python scripts/gen_design_css.py` — it errors on any token path not covered
   by `VAR_MAP`/`REFERENCE_ONLY`; resolve additions explicitly there.
3. Run `uv run --no-sync --active pytest -q --tb=short tests/test_design_tokens.py tests/test_design_css.py`.
4. Update the provenance table above and the divergence list if it changed.

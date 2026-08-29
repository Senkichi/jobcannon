# Living Journal Adoption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **This plan is additionally structured for Workflow-tool fleet execution — see "Fleet execution protocol" below; it overrides the per-task review cadence (minimum gates by user directive).**

**Goal:** Replace jobcannon's Tailwind-CDN styling with a self-contained Living Journal token pipeline and restyle all ~25 templates as a full identity adoption, per the approved spec `docs/superpowers/specs/2026-08-28-living-journal-adoption-design.md`.

**Architecture:** A deterministic Python generator turns vendored W3C design tokens into `lj-tokens.css` (custom properties on `:root`, dark via `prefers-color-scheme`); a hand-written `jc.css` defines the complete, closed class vocabulary (ported LJ primitives + `jc-*` components); templates are then restyled in parallel by disjoint-file-ownership tasks that only *apply* that vocabulary. Structural tests (drift, no-literals, class-closure) make regressions unrepresentable.

**Tech Stack:** Flask + Jinja + htmx 2.0.4 (vendored), pure CSS custom properties (no Tailwind, no node), pytest + Playwright, fonttools for font subsetting.

## Global Constraints

*(Inject this entire section verbatim into every dispatched agent's prompt. It is part of every task's requirements.)*

- **Branch:** all work on `design/living-journal-adoption`. Never push; the PR is a user-gated action at the end.
- **Preserve verbatim in every template change:** every `hx-*` attribute, `data-*` attribute, element `id`, ARIA attribute, `{{ touch_target(...) }}` call site, all Jinja logic/conditionals/blocks (including `visitor_is_authed` vs `g.clerk_user` gating — issue #205), the CSRF meta-tag/hx-headers split, the `HX-CSRF-Error` toast path, and every behavioral Jinja comment. Restyle = change `class="..."` values and add structural wrappers only where a component needs one. Never re-plumb.
- **Closed class vocabulary:** templates may reference ONLY classes defined in `jobcannon/web/static/jc.css` (Task 2 lists them all). Never invent a class name — not even a plausible `jc-*` one. If a needed component is missing, compose from existing primitives; if that fails, write a gap note to `docs/superpowers/plans/lj-gap-notes.md` (append a bullet: template, what's missing, suggested shape) and leave that one element's styling minimal — the integration task resolves gaps. Never invent `lj-*` names (that prefix means "part of the design system").
- **No color literals** outside generated `lj-tokens.css`: no hex, `rgb()`/`rgba()` with numeric channels, `hsl()`, `oklch()`, or named colors in `jc.css`, templates, or inline styles. `rgba(var(--lj-shade), .42)` (var-composed) is allowed.
- **Green is honesty-only:** `--lj-green`/`--lj-green-text` may color ONLY verified-fresh stamps, why-chips, and the masthead accent rule. Actions (save/dismiss/apply), errors, and everything else are ink. Small-caps/small text uses `--lj-green-text`, never `--lj-green`.
- **Body-size secondary text** uses `--lj-gray-text`, never `--lj-gray` (3:1 vs 4.5:1 tiers).
- **Motion:** paper never moves — no transform/translate/scale on surfaces. Only ink draws on (`lj-draw`, 600ms–1s, `--lj-spring`). Nothing breathes in v1 (omit `lj-breathe` even though the reference CTA has it). Every animation must resolve to its fully-drawn end state under `prefers-reduced-motion`.
- **Copy changes:** none, except label casing where small-caps styling demands uppercase source text is NOT needed (CSS `text-transform: uppercase` does it — leave source copy as-is).
- **Testing:** `uv run --active pytest -q --tb=short` — never bare `pytest`. (In an isolated worktree with its own `.venv`, drop `--active`.)
- **Commits:** Conventional Commits (`feat:`, `test:`, `docs:`, `refactor:`); commit at every task's end, WIP-commit mid-task if a step is heavy. No attribution footers.
- **Windows repo:** write files UTF-8, LF line endings for new `.css`/`.py`/`.md` (pass `newline="\n"` in Python writes).

---

## Fleet execution protocol (parallelization map)

The user executes this plan with dynamic workflows / subagent fleets. Task
boundaries below are drawn so that **no two concurrently-dispatched tasks
write the same file**.

```
Wave 0a (parallel):        T1 tokens+generator     T2 jc.css        T5 provenance doc
Wave 0b (main session,     T3 fonts   →   T4 htmx      (sequential; network + supply-chain
        after T5):                                       verification stay in the main session)
Wave 0c (join, needs T1–T4): T6 base.html + chrome + template_globals + static config
Wave 1 (parallel, needs T6): T7 posting row    T8 feed pages     T9 history pair
                             T10 picker        T11 consent+account  T12 errors+legal
Wave 2 (join):             T13 closure tests + gap resolution + full suite
                           T14 screenshots (both themes) → USER GATE → PR (user-approved)
```

Rules for the workflow author (from the `parallelizing-with-workflow` skill; the
lint hook enforces model pins):

- Inject the **Global Constraints** section verbatim into every agent prompt; an
  agent sees only its own task otherwise.
- Wave-1 agents: sonnet-tier, one task each, pipeline each straight into its own
  test run (no barrier waiting on the slowest family). T13/T14 synthesis may be
  opus-tier.
- Pre-warm the workspace before dispatch (branch checked out, `uv sync` done,
  imports pass); agents' step 1 is verify-only. No destructive setup inside a
  retried prompt.
- Long investigation output goes to artifact files (`lj-gap-notes.md` is the
  designated one), not schema payloads.
- Each wave-1 task owns its listed templates AND its listed test files
  exclusively. If a test file spans families it is assigned to exactly one task
  below — check the Files list, not your intuition.
- Background any test run that can exceed ~2 min silent.

**Review gates (minimum, by user directive):** no per-task human review. The two
human gates are (1) eyeballing the Task 14 screenshot artifacts against swole's
`reference.html`, and (2) approving the push/PR.

---

### Task 1: Vendored tokens + generator + drift test

**Files:**
- Create: `jobcannon/web/design/tokens.json`
- Create: `scripts/gen_design_css.py`
- Create: `jobcannon/web/static/lj-tokens.css` (generated)
- Test: `tests/test_design_tokens.py`

**Interfaces:**
- Consumes: nothing (root task).
- Produces: `jobcannon/web/static/lj-tokens.css` defining exactly these custom
  properties on `:root` (light) and under `@media (prefers-color-scheme: dark)`:
  `--lj-page, --lj-card, --lj-card2, --lj-ink, --lj-green, --lj-green-text,
  --lj-gray, --lj-gray-text, --lj-equipment, --lj-rule, --lj-rule-2, --lj-hair,
  --lj-hair-soft, --lj-tan, --lj-serif, --lj-sans, --lj-spring, --lj-sheen,
  --lj-shade`. `--lj-shade` is an RGB triple (use as `rgba(var(--lj-shade), A)`);
  everything else is a complete CSS value.

- [ ] **Step 1: Vendor tokens.json**

Copy `C:\Users\senki\repos\swole\docs\design\living-journal\tokens.json`
verbatim to `jobcannon/web/design/tokens.json`, then add ONE key at the top of
the root object (JSON has no comments; provenance rides in a `$vendored` key,
which the generator ignores and the coverage check knows about):

```json
  "$vendored": "From swole (private, github.com/Senkichi/swole) docs/design/living-journal/tokens.json @ 163da4fd9c1d42dc57fe125e0165da9cec680ca8 (2026-07-30). Re-sync: docs/design/living-journal.md. Do not hand-edit values.",
```

The rest of the file must stay byte-identical to the source apart from this
insertion.

- [ ] **Step 2: Write the failing drift test**

```python
"""Guards the Living Journal token pipeline (spec §7.1).

lj-tokens.css is GENERATED from jobcannon/web/design/tokens.json by
scripts/gen_design_css.py. These tests make a stale regen, a hand-edit, or an
unmapped token unrepresentable on CI.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen_design_css.py"
COMMITTED = REPO_ROOT / "jobcannon" / "web" / "static" / "lj-tokens.css"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_design_css", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_css_matches_regeneration():
    gen = _load_generator()
    regenerated = gen.generate_css(gen.load_tokens())
    assert COMMITTED.read_bytes() == regenerated.encode("utf-8"), (
        "lj-tokens.css is stale or hand-edited. "
        "Run: python scripts/gen_design_css.py"
    )


def test_unmapped_token_fails_loudly():
    gen = _load_generator()
    tokens = copy.deepcopy(gen.load_tokens())
    tokens["color"]["light"]["mystery"] = {"$type": "color", "$value": "#123456"}
    with pytest.raises(ValueError, match="mystery"):
        gen.generate_css(tokens)


def test_missing_token_fails_loudly():
    gen = _load_generator()
    tokens = copy.deepcopy(gen.load_tokens())
    del tokens["color"]["light"]["page"]
    with pytest.raises(ValueError, match="color.light.page"):
        gen.generate_css(tokens)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run --active pytest -q --tb=short tests/test_design_tokens.py`
Expected: FAIL / ERROR — `scripts/gen_design_css.py` does not exist yet.

- [ ] **Step 4: Write the generator**

`scripts/gen_design_css.py` — complete file:

```python
"""tokens.json -> jobcannon/web/static/lj-tokens.css (Living Journal pipeline).

Deterministic by construction: VAR_MAP order is emission order, formatting is
fixed, output ends with exactly one trailing newline. The drift test
(tests/test_design_tokens.py) asserts byte-equality with the committed file.

The token-path -> variable-name mapping is EXPLICIT CONFIG (spec §3): the
canonical --lj-* names come from the LJ package's base.css, whose names
diverge from the token tree (inkSecondary -> --lj-gray, hairline -> --lj-hair)
and which defines a few variables with no tokens.json source at all. Those
ride here as PORT entries, values transcribed from base.css
(packages/living-journal/src/theme/base.css @ the vendored swole commit).

Coverage is self-verifying: every leaf token must be consumed by VAR_MAP or
listed in REFERENCE_ONLY, and every VAR_MAP token path must exist — a re-sync
that adds, renames, or removes tokens fails generation loudly instead of
drifting silently.

Run: python scripts/gen_design_css.py
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKENS_PATH = REPO_ROOT / "jobcannon" / "web" / "design" / "tokens.json"
OUT_PATH = REPO_ROOT / "jobcannon" / "web" / "static" / "lj-tokens.css"

# Each side of a var is one of:
#   ("token", "dotted.path")            -> emit the token's $value verbatim
#   ("port", "css value")               -> no tokens.json source; transcribed
#                                          from base.css (see module docstring)
#   ("stack", "dotted.path", "css stack")-> font stack; asserts every family in
#                                          the token's $value array appears in
#                                          the stack, then emits the stack
#   ("bezier", "dotted.path")           -> cubic-bezier() from a 4-number array
# fmt: off
VAR_MAP: list[tuple[str, tuple, tuple]] = [
    # name            light                                   dark
    ("--lj-page",     ("token", "color.light.page"),          ("token", "color.dark.page")),
    ("--lj-card",     ("token", "color.light.card"),          ("token", "color.dark.card")),
    ("--lj-card2",    ("port", "#FBF6EC"),                    ("token", "color.dark.raised")),
    ("--lj-ink",      ("token", "color.light.ink"),           ("token", "color.dark.ink")),
    ("--lj-green",    ("token", "color.light.semantic"),      ("token", "color.dark.semantic")),
    ("--lj-green-text", ("token", "color.light.semanticText"), ("token", "color.dark.semanticText")),
    ("--lj-gray",     ("token", "color.light.inkSecondary"),  ("token", "color.dark.inkSecondary")),
    ("--lj-gray-text", ("token", "color.light.inkSecondaryText"), ("token", "color.dark.inkSecondaryText")),
    ("--lj-equipment", ("port", "#7A7A85"),                   ("token", "color.dark.equipment")),
    ("--lj-rule",     ("token", "color.light.rule"),          ("token", "color.dark.dialTrack")),
    ("--lj-rule-2",   ("port", "#DED3C0"),                    ("port", "#453626")),
    ("--lj-hair",     ("token", "color.light.hairline"),      ("token", "color.dark.hairline")),
    ("--lj-hair-soft", ("port", "rgba(var(--lj-shade), .08)"), ("port", "rgba(237, 229, 216, .09)")),
    ("--lj-tan",      ("port", "#C9B79A"),                    ("port", "#6E5A3E")),
    ("--lj-serif",    ("stack", "typography.display.fontFamily",
                       "'Fraunces Variable', 'Fraunces', Georgia, 'Times New Roman', serif"),
                      ("same",)),
    ("--lj-sans",     ("stack", "typography.body.fontFamily",
                       "'Inter Variable', 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif"),
                      ("same",)),
    ("--lj-spring",   ("bezier", "motion.spring"),            ("same",)),
    ("--lj-sheen",    ("port", "rgba(255, 255, 255, .7)"),    ("port", "rgba(255, 244, 228, .06)")),
    ("--lj-shade",    ("port", "30, 22, 17"),                 ("port", "12, 8, 4")),
]
# fmt: on

# Leaf tokens that are real design decisions but are consumed as spec-level
# constants inside jc.css (radii, stroke weights, durations) or are
# platform guidance, not CSS variables. The coverage check requires every
# leaf to be here or in VAR_MAP — nothing falls through silently.
REFERENCE_ONLY: frozenset[str] = frozenset({
    "typography.display.numeralStyle",
    "typography.label.fontFamily",
    "typography.label.letterSpacing",
    "typography.label.textTransform",
    "stroke.figureBody",
    "stroke.cardHairline",
    "stroke.accentRule",
    "radius.card",
    "radius.tab",
    "radius.button",
    "radius.stepper",
    "motion.drawOn",
    "motion.drain",
    "motion.breathe",
    "motion.principles",
    "motion.reducedMotion",
    "iconography.navStyle",
})

HEADER = """\
/* GENERATED FILE — DO NOT EDIT.
   Source: jobcannon/web/design/tokens.json (vendored from swole @ 163da4fd9c1d42dc57fe125e0165da9cec680ca8)
   Generator: scripts/gen_design_css.py — regenerate with: python scripts/gen_design_css.py
   Drift-guarded by tests/test_design_tokens.py.
   Light = Paper on :root; Dark = Lamplit Paper via prefers-color-scheme. */
"""


def load_tokens() -> dict[str, Any]:
    return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))


def _leaf_paths(node: Any, prefix: str = "") -> set[str]:
    """Dotted paths of every token leaf (dict carrying a $value)."""
    leaves: set[str] = set()
    if not isinstance(node, dict):
        return leaves
    if "$value" in node:
        leaves.add(prefix)
        return leaves
    for key, child in node.items():
        if key.startswith("$"):
            continue  # $schema/$description/$vendored metadata
        child_prefix = f"{prefix}.{key}" if prefix else key
        leaves |= _leaf_paths(child, child_prefix)
    return leaves


def _lookup(tokens: dict[str, Any], path: str) -> Any:
    node: Any = tokens
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"VAR_MAP references missing token: {path}")
        node = node[part]
    if "$value" not in node:
        raise ValueError(f"VAR_MAP path is not a leaf token: {path}")
    return node["$value"]


def _resolve(tokens: dict[str, Any], spec: tuple, consumed: set[str]) -> str:
    kind = spec[0]
    if kind == "token":
        consumed.add(spec[1])
        return str(_lookup(tokens, spec[1]))
    if kind == "port":
        return spec[1]
    if kind == "stack":
        consumed.add(spec[1])
        families = _lookup(tokens, spec[1])
        for family in families:
            if family not in spec[2]:
                raise ValueError(
                    f"font stack for {spec[1]} no longer contains {family!r}: "
                    "re-sync changed token families; update VAR_MAP"
                )
        return spec[2]
    if kind == "bezier":
        consumed.add(spec[1])
        nums = _lookup(tokens, spec[1])
        return "cubic-bezier(" + ", ".join(str(n) for n in nums) + ")"
    raise ValueError(f"unknown VAR_MAP spec kind: {kind}")


def generate_css(tokens: dict[str, Any]) -> str:
    consumed: set[str] = set()
    light: list[str] = []
    dark: list[str] = []
    for name, light_spec, dark_spec in VAR_MAP:
        light_value = _resolve(tokens, light_spec, consumed)
        light.append(f"  {name}: {light_value};")
        if dark_spec != ("same",):
            dark_value = _resolve(tokens, dark_spec, consumed)
            if dark_value != light_value:
                dark.append(f"  {name}: {dark_value};")

    leaves = _leaf_paths(tokens)
    unmapped = leaves - consumed - REFERENCE_ONLY
    if unmapped:
        raise ValueError(f"tokens not consumed by VAR_MAP or REFERENCE_ONLY: {sorted(unmapped)}")
    missing = REFERENCE_ONLY - leaves
    if missing:
        raise ValueError(f"REFERENCE_ONLY names tokens that no longer exist: {sorted(missing)}")

    return (
        HEADER
        + "\n:root {\n" + "\n".join(light) + "\n}\n"
        + "\n@media (prefers-color-scheme: dark) {\n  :root {\n"
        + "\n".join(f"    {line.strip()}" for line in dark)
        + "\n  }\n}\n"
    )


def main() -> None:
    css = generate_css(load_tokens())
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(css)
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} ({len(css)} bytes)")


if __name__ == "__main__":
    main()
```

Note the dark-block emitter: the expression
`"\n".join("  " + line.strip() and "    " + line.strip() for line in dark)`
is wrong Python golf — replace it with the plain loop it means:

```python
        + "\n".join(f"    {line.strip()}" for line in dark)
```

(Kept as an explicit call-out so a fleet agent doesn't transcribe the broken
line: the dark block indents each `--lj-*: value;` by four spaces.)

- [ ] **Step 5: Generate and run the tests**

```bash
python scripts/gen_design_css.py
uv run --active pytest -q --tb=short tests/test_design_tokens.py
```

Expected: 3 passed. Open `jobcannon/web/static/lj-tokens.css` and confirm by
eye: 19 vars in `:root`, dark block re-declares every var except
`--lj-serif`, `--lj-sans`, `--lj-spring`.

- [ ] **Step 6: Commit**

```bash
git add jobcannon/web/design/tokens.json scripts/gen_design_css.py jobcannon/web/static/lj-tokens.css tests/test_design_tokens.py
git commit -m "feat: Living Journal token pipeline (vendored tokens, generator, drift test)"
```

---

## Survey addendum (repo facts every task must respect)

From the 2026-08-28 codebase survey; treat as authoritative.

1. **CSP** — `jobcannon/web/security_headers.py:118` hardcodes
   `script_hosts = ["https://cdn.tailwindcss.com", "https://unpkg.com"]` and the
   CSP includes `'unsafe-eval'` justified (module docstring :43-45) solely by the
   Tailwind Play CDN. Task 6 removes both hosts and `'unsafe-eval'`. `style-src`
   and `font-src` already include `'self'` — self-hosted CSS/fonts need no CSP
   change. `tests/host/test_security_headers.py` does not pin the CDN literals.
2. **Touch targets** — `tests/host/test_touch_targets.py` is a **static source
   scan** (no Playwright): every `<a|button|input|label|select|textarea>` in every
   template must keep `{{ touch_target(...) }}` inside its `class="..."`
   attribute (65+ sites, `>= 60` floor asserted). `:241-243` pins the literal
   returned tokens (Task 6 updates them to `jc-touch` / `jc-touch-box`), and
   `:396` hardcodes base.html's Feed-link line verbatim as a sabotage fixture
   (Task 6 updates the fixture string).
3. **legal_page.html's inline `<style>` MUST stay inline** —
   `test_legal_table_scroll.py:691-704` asserts a `<style>` tag in the served
   page; it and `test_touch_targets.py:447-494` regex-parse `.legal-prose` rules
   out of the block (a-padding sum >= 26px, `.table-scroll` overflow-x,
   `:focus-visible` outline + `outline-offset: 2px`, and NO `@media` rule), with
   sabotage tests string-matching exact declarations. Task 12 restyles the block
   to `var(--lj-*)` values in place and updates the pinned sabotage strings.
4. **~50 `data-*` hooks + ids `jc-error-toast`, `consent-panel`,
   `picker-options`, `feed-content` are the test contract** — never rename.
   `test_clerk_loader_template.py` pins inline-script substrings in `base.html`
   and `error_401.html` — those script bodies must survive byte-for-byte.
5. **No `static/` existed before this branch; zero `url_for('static', ...)`
   call sites.** Flask's default static route serves `jobcannon/web/static/` at
   `/static` with no config change. `/static` is NOT in `PUBLIC_PATHS` and must
   not be added to it (the cache-header test derives its `private` assertions
   from that set). No asset-caching policy work in this migration.
6. **CI** = ruff check + ruff format --check + pytest on Python 3.12; no node,
   no Playwright. All new Python must pass `uv run ruff check .` and
   `uv run ruff format .` (line-length 100) before commit.
7. `touch_target()` call sites: 67 total (63 bare + 4 `'checkbox'`) across 20
   of 25 templates. The function's docstring counts are stale; Task 6 fixes them.

## Restyle pattern reference (all wave-1 tasks)

Old Tailwind bundle → new class(es). The vocabulary is CLOSED: these plus the
full list in Task 2. Keep `{{ touch_target(...) }}` exactly where it appears.

| Old pattern | New |
|---|---|
| `text-2xl font-semibold mb-3` heading | `jc-title` |
| heading with `mb-6` | `jc-title jc-title--roomy` |
| intro/`text-sm text-neutral-400` paragraph | `jc-lede` |
| `text-xs`/`text-sm text-neutral-500` fine print | `jc-note` |
| `underline` in-content link | `jc-link` |
| footer/quiet nav link | `jc-quiet-link` |
| header nav link (small-caps) | `jc-nav-link` |
| button / `rounded px-4 py-2 font-medium` | `jc-btn` (secondary) / `jc-btn jc-btn--primary` (primary action) |
| full-width signup CTA | `jc-cta` |
| form control `border-neutral-700 bg-neutral-900 rounded px-2 py-1` | `jc-input` |
| filter bar row | `jc-filterbar` (controls inside get `jc-input`) |
| checkbox `h-11 w-11` | unchanged element; `touch_target('checkbox')` now emits `jc-touch-box`; theming is global (`accent-color`) |
| card/panel (`data-profile-card` etc.) | `jc-panel` |
| feed list container `space-y-4` | `jc-ledger` (hairline-ruled; no margins between rows) |
| feed row `<li>` | `jc-row`; internals compose `jc-row-title`, `jc-row-sub`, `jc-index`, `jc-meta`+`jc-meta-num`+`jc-meta-lab`, `jc-actions` |
| why-chips `<ul data-why-chips>` | `jc-chips`, items `jc-chip`; wrapper `jc-why` with an `lj-label` reading `Why this rank` |
| verified-fresh marker | `jc-stamp jc-stamp--green` + `jc-stamp-dot` |
| status/confirmation stamp (ink) | `jc-stamp` + `jc-stamp-dot` |
| `text-red-400` error text | `jc-error-note` (ink — NEVER red) |
| history tabs (`{{ 'x' if cond }}` in class) | `jc-tab` + conditional `jc-tab--active` |
| vertical spacing stacks (`space-y-*`, `mb-*` groups) | wrap in `jc-stack` |
| horizontal flex+gap rows | `jc-cluster` |
| empty-state block | `jc-empty` (title inside may use `jc-title`) |
| page masthead (feed/demo/preview h1) | `jc-title` followed by the accent-rule SVG (below) — the ONE green accent per screen |

Accent rule SVG (verbatim; only on the page masthead, one per page):

```html
<svg class="jc-accent-rule" viewBox="0 0 188 5" preserveAspectRatio="none" aria-hidden="true"><line x1="3" y1="2.5" x2="185" y2="2.5"/></svg>
```

Drawn check SVG (verbatim; for saved/applied confirmation marks — ink, not green):

```html
<svg class="jc-check" viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 8.5 6.2 12.2 13.5 4.5"/></svg>
```

---

### Task 2: jc.css — the complete closed class vocabulary

**Files:**
- Create: `jobcannon/web/static/jc.css`
- Test: `tests/test_design_css.py`

**Interfaces:**
- Consumes: the `--lj-*` custom properties from Task 1 (names listed in Task 1's
  Produces). Does NOT read tokens.json.
- Produces: the complete class vocabulary. Templates may use ONLY:
  `lj-root, lj-label, jc-page, jc-header, jc-wordmark, jc-nav, jc-nav--end,
  jc-nav-link, jc-quiet-link, jc-link, jc-footer, jc-touch, jc-touch-box,
  jc-toast, jc-title, jc-title--roomy, jc-lede, jc-note, jc-accent-rule,
  jc-stack, jc-cluster, jc-panel, jc-ledger, jc-row, jc-index, jc-row-title,
  jc-row-sub, jc-meta, jc-meta-num, jc-meta-lab, jc-stamp, jc-stamp--green,
  jc-stamp-dot, jc-why, jc-chips, jc-chip, jc-actions, jc-btn, jc-btn--primary,
  jc-cta, jc-check, jc-field, jc-input, jc-filterbar, jc-tab, jc-tab--active,
  jc-error-note, jc-empty` (48 classes; plus `legal-prose`/`table-scroll`,
  defined only in legal_page.html's inline style block).

- [ ] **Step 1: Write jc.css** — complete file:

```css
/* jobcannon component layer — Living Journal identity.
   Composes EXCLUSIVELY from the var(--lj-*) tokens in lj-tokens.css (generated;
   see scripts/gen_design_css.py). No color literals anywhere in this file —
   enforced by tests/test_design_css.py.
   Naming: lj-* = ported design-system primitives (near-verbatim from the LJ
   package's base.css); jc-* = jobcannon compositions. Never add a new lj-* name.
   Spec-level constants used here (from tokens.json, reference-only):
   radius.card 16 -> panels use the package's 19px card recipe; radius.tab 10px;
   radius.button 14px; stroke.cardHairline 1.5px; motion.drawOn 600ms. */

:root { color-scheme: light dark; }

/* ---- ported primitives ---- */

.lj-root {
  background: var(--lj-page);
  color: var(--lj-ink);
  font-family: var(--lj-sans);
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
  margin: 0;
}
.lj-root *,
.lj-root *::before,
.lj-root *::after { box-sizing: border-box; margin: 0; padding: 0; }

.lj-label {
  font-family: var(--lj-sans);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .2em;
  text-transform: uppercase;
  color: var(--lj-gray-text);
}

@keyframes lj-draw { to { stroke-dashoffset: 0; } }

/* ---- global element rules ---- */

.lj-root :focus-visible { outline: 2px solid var(--lj-ink); outline-offset: 2px; }
.lj-root input[type="checkbox"] { accent-color: var(--lj-ink); cursor: pointer; }

/* ---- chrome ---- */

.jc-page { max-width: 48rem; margin-inline: auto; padding: 64px 24px; }

.jc-header {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 16px 24px;
  border-bottom: 1px solid var(--lj-hair);
}

.jc-wordmark {
  font-family: var(--lj-serif);
  font-weight: 600;
  font-size: 19px;
  letter-spacing: -.01em;
  color: var(--lj-ink);
}

.jc-nav { display: flex; align-items: center; gap: 16px; }
.jc-nav--end { margin-left: auto; }

.jc-nav-link {
  display: inline-flex;
  align-items: center;
  padding-inline: 4px;
  font-family: var(--lj-sans);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--lj-gray-text);
  text-decoration: none;
  transition: color .18s ease;
}
.jc-nav-link:hover { color: var(--lj-ink); }

.jc-footer {
  border-top: 1px solid var(--lj-hair);
  padding: 16px 24px;
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 13px;
  color: var(--lj-gray-text);
}

.jc-quiet-link {
  display: inline-flex;
  align-items: center;
  padding-inline: 4px;
  color: var(--lj-gray-text);
  text-decoration: none;
  transition: color .18s ease;
}
.jc-quiet-link:hover { color: var(--lj-ink); }

.jc-link {
  color: var(--lj-ink);
  text-decoration: underline;
  text-decoration-color: var(--lj-tan);
  text-underline-offset: 3px;
}
.jc-link:hover { text-decoration-color: var(--lj-ink); }

/* touch-target floor (emitted by touch_target(); tests pin these names) */
.jc-touch { min-height: 44px; }
.jc-touch-box { width: 44px; height: 44px; }

.jc-toast {
  position: fixed;
  top: 16px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 50;
  max-width: 28rem;
  border: 1.5px solid var(--lj-ink);
  border-radius: 10px;
  background: var(--lj-card);
  color: var(--lj-ink);
  padding: 12px 16px;
  font-size: 13px;
  font-weight: 600;
  box-shadow: 2px 2px 0 -1px rgba(var(--lj-shade), .16);
}

/* ---- type ---- */

.jc-title {
  font-family: var(--lj-serif);
  font-weight: 600;
  font-size: 26px;
  line-height: 1.15;
  letter-spacing: -.01em;
  color: var(--lj-ink);
  margin-bottom: 12px;
}
.jc-title--roomy { margin-bottom: 24px; }

.jc-lede { font-size: 15px; color: var(--lj-gray-text); max-width: 62ch; margin-bottom: 10px; }
.jc-note { font-size: 13px; color: var(--lj-gray-text); }

.jc-accent-rule { display: block; width: 188px; height: 5px; margin-top: 10px; }
.jc-accent-rule line {
  stroke: var(--lj-green);
  stroke-width: 4.5;
  stroke-linecap: round;
  stroke-dasharray: 190;
  stroke-dashoffset: 190;
  animation: lj-draw 1s .2s var(--lj-spring) forwards;
}

/* ---- layout helpers ---- */

.jc-stack { display: grid; gap: 12px; }
.jc-cluster { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }

.jc-panel {
  background: var(--lj-card);
  border: 1.5px solid var(--lj-hair);
  border-radius: 19px;
  box-shadow: 0 1px 0 var(--lj-sheen) inset, 0 6px 16px -13px rgba(var(--lj-shade), .42);
  padding: 18px 20px;
}

/* ---- the ledger (feed rows) ---- */

.jc-ledger { list-style: none; margin-top: 16px; }
.jc-ledger > * + * { border-top: 1px solid var(--lj-hair); }

.jc-row { padding: 16px 0; }

.jc-index {
  font-family: var(--lj-serif);
  font-weight: 600;
  font-size: 15px;
  font-variant-numeric: tabular-nums;
  color: var(--lj-gray);
}

.jc-row-title {
  font-family: var(--lj-serif);
  font-weight: 600;
  font-size: 17px;
  line-height: 1.25;
  color: var(--lj-ink);
}

.jc-row-sub { font-size: 13px; color: var(--lj-gray-text); }

.jc-meta { display: inline-flex; flex-direction: column; align-items: flex-end; gap: 2px; }
.jc-meta-num {
  font-family: var(--lj-serif);
  font-weight: 600;
  font-size: 20px;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  color: var(--lj-ink);
}
.jc-meta-lab {
  font-size: 10px;
  color: var(--lj-gray-text);
  letter-spacing: .06em;
  text-transform: uppercase;
}

/* ---- stamps & honesty signals (the ONLY green consumers besides the accent rule) ---- */

.jc-stamp {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  border: 1.5px solid var(--lj-ink);
  border-radius: 6px;
  padding: 4px 9px;
  color: var(--lj-ink);
  font-size: 9.5px;
  letter-spacing: .15em;
  text-transform: uppercase;
  font-weight: 700;
  box-shadow: 2px 2px 0 -1px rgba(var(--lj-shade), .16);
}
.jc-stamp-dot { width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.jc-stamp--green { border-color: var(--lj-green-text); color: var(--lj-green-text); }

.jc-why { margin-top: 10px; }
.jc-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  list-style: none;
  margin-top: 6px;
}
.jc-chip {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--lj-green-text);
}

/* ---- actions ---- */

.jc-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 12px; }

.jc-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1.5px solid var(--lj-ink);
  border-radius: 14px;
  background: transparent;
  color: var(--lj-ink);
  padding: 8px 14px;
  font-family: var(--lj-sans);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
  transition: background .18s ease, color .18s ease;
}
.jc-btn:hover { background: var(--lj-card2); }
.jc-btn:active { background: var(--lj-ink); color: var(--lj-card); }
.jc-btn[disabled] { opacity: .45; cursor: not-allowed; }
.jc-btn--primary { background: var(--lj-ink); color: var(--lj-page); }
.jc-btn--primary:hover { background: var(--lj-ink); opacity: .92; }

/* full-width signup CTA. The reference recipe carries lj-breathe; deliberately
   omitted — nothing breathes in v1 (spec §4/§7.6). */
.jc-cta {
  width: 100%;
  border: none;
  border-radius: 17px;
  background: var(--lj-ink);
  color: var(--lj-page);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 11px;
  padding: 15px;
  font-family: var(--lj-sans);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: .01em;
  cursor: pointer;
  text-decoration: none;
}

.jc-check { display: inline-block; width: 16px; height: 16px; }
.jc-check path {
  fill: none;
  stroke: var(--lj-ink);
  stroke-width: 2.4;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-dasharray: 22;
  stroke-dashoffset: 22;
  animation: lj-draw .6s var(--lj-spring) forwards;
}

/* ---- forms ---- */

.jc-field { display: block; margin-bottom: 14px; }

.jc-input {
  background: var(--lj-card);
  border: 1px solid var(--lj-rule-2);
  border-radius: 10px;
  padding: 8px 10px;
  color: var(--lj-ink);
  font-family: var(--lj-sans);
  font-size: 14px;
}

.jc-filterbar {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 10px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--lj-hair);
}

/* ---- tabs / pagination ---- */

.jc-tab {
  display: inline-flex;
  align-items: center;
  padding: 6px 12px;
  border: 1px solid transparent;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--lj-gray-text);
  text-decoration: none;
}
.jc-tab--active { border-color: var(--lj-ink); color: var(--lj-ink); background: var(--lj-card); }

/* ---- states ---- */

.jc-error-note {
  font-size: 13px;
  font-weight: 600;
  color: var(--lj-ink);
  border-left: 3px solid var(--lj-ink);
  padding-left: 10px;
}

.jc-empty { padding: 28px 0; color: var(--lj-gray-text); font-size: 14px; }

/* ---- reduced motion: every animation resolves to its drawn end state ---- */

@media (prefers-reduced-motion: reduce) {
  .lj-root *,
  .lj-root *::before,
  .lj-root *::after {
    animation-duration: .001s !important;
    transition-duration: .001s !important;
  }
  .jc-accent-rule line,
  .jc-check path { animation: none !important; stroke-dashoffset: 0; }
}
```

- [ ] **Step 2: Write the no-color-literals test** — `tests/test_design_css.py`:

```python
"""No color literal may exist outside generated lj-tokens.css (spec §7.2).

Scans jc.css and fonts.css. var()-composed rgba(var(--lj-shade), .16) is legal;
numeric-channel rgb()/rgba(), hex, hsl(), oklch() are not.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "jobcannon" / "web" / "static"

_LITERAL_PATTERNS = (
    re.compile(r"#[0-9a-fA-F]{3,8}\b"),
    re.compile(r"\b(?:rgb|rgba|hsl|hsla|oklch|oklab)\(\s*\d"),
)


@pytest.mark.parametrize("name", ["jc.css", "fonts.css"])
def test_stylesheet_has_no_color_literals(name):
    text = (STATIC / name).read_text(encoding="utf-8")
    hits = [
        (i + 1, line.strip())
        for i, line in enumerate(text.splitlines())
        for pat in _LITERAL_PATTERNS
        if pat.search(line)
    ]
    assert not hits, f"color literals in {name}: {hits}"


def test_literal_detector_catches_a_real_literal():
    assert _LITERAL_PATTERNS[0].search("color: #FAF6EF;")
    assert _LITERAL_PATTERNS[1].search("background: rgba(30, 22, 17, .4);")
    assert not _LITERAL_PATTERNS[1].search("background: rgba(var(--lj-shade), .16);")
```

- [ ] **Step 3: Run** `uv run --active pytest -q --tb=short tests/test_design_css.py`
  — expected: pass (fonts.css exists from the pre-warm). Then
  `uv run ruff check tests/test_design_css.py && uv run ruff format tests/test_design_css.py`.

- [ ] **Step 4: Commit** —
  `git add jobcannon/web/static/jc.css tests/test_design_css.py && git commit -m "feat: jc.css Living Journal component vocabulary + no-literals guard"`

---

### Task 3: Fonts (EXECUTED in main session pre-warm — verify only)

Fetched `google/fonts@<recorded sha>` Fraunces Roman+Italic and Inter variable
TTFs, subsetted to Latin + punctuation via
`uvx --from "fonttools[woff]" pyftsubset --flavor=woff2 --layout-features='*'`
(keeps `fvar`/`gvar` — verified), OFL license files alongside. Files:
`jobcannon/web/static/fonts/{fraunces-var,fraunces-italic-var,inter-var}.woff2`,
`OFL-Fraunces.txt`, `OFL-Inter.txt`.

- [ ] **Step 1: Write** `jobcannon/web/static/fonts.css`:

```css
/* Self-hosted variable fonts (SIL OFL — license files in fonts/).
   Provenance + subset recipe: docs/design/living-journal.md. */
@font-face {
  font-family: 'Fraunces Variable';
  src: url('fonts/fraunces-var.woff2') format('woff2');
  font-weight: 100 900;
  font-style: normal;
  font-display: swap;
}
@font-face {
  font-family: 'Fraunces Variable';
  src: url('fonts/fraunces-italic-var.woff2') format('woff2');
  font-weight: 100 900;
  font-style: italic;
  font-display: swap;
}
@font-face {
  font-family: 'Inter Variable';
  src: url('fonts/inter-var.woff2') format('woff2');
  font-weight: 100 900;
  font-style: normal;
  font-display: swap;
}
```

- [ ] **Step 2: Commit** fonts + fonts.css (`feat: self-host Fraunces/Inter variable fonts (OFL)`)

### Task 4: htmx vendoring (EXECUTED in main session pre-warm — verify only)

`jobcannon/web/static/htmx.min.js` fetched from unpkg `htmx.org@2.0.4`,
sha256 cross-checked against jsdelivr's copy of the same version (supply-chain
double-source), license at `jobcannon/web/static/htmx.LICENSE.txt` from the
`v2.0.4` tag. Committed with the recorded hash in the message
(`feat: vendor htmx 2.0.4 (sha256 recorded)`).

### Task 5: Provenance + identity doc (main session; needs pre-warm values)

Create `docs/design/living-journal.md` — identity rules from spec §7.6 (green =
honesty only; paper never moves; one accent per screen; contrast tiers; never
invent lj-* names), token pipeline description, vendored-asset provenance table
(swole commit `163da4fd9c1d42dc57fe125e0165da9cec680ca8`, google/fonts sha +
htmx sha256 from the pre-warm record), the deliberate divergences (dark
`--lj-hair` token alpha .14 supersedes base.css .17; panel radius 19px vs
radius.card token 16px — package recipe wins; `lj-breathe` omitted), and the
re-sync procedure (re-copy tokens.json verbatim, re-run generator, generation
errors on unmapped paths). Add one row to the existing `docs/design/README.md`
binding-docs index table. Commit (`docs: Living Journal identity + provenance`).

---

### Task 6: base.html + chrome + CSP + touch_target tokens (join; needs T1–T4)

**Files:**
- Modify: `jobcannon/web/templates/base.html`
- Modify: `jobcannon/web/template_globals.py:33-36` (`_KIND_TOKENS`) + stale docstring counts
- Modify: `jobcannon/web/security_headers.py` (script_hosts, `'unsafe-eval'`, docstring)
- Modify: `tests/host/test_touch_targets.py:241-243` (token literals), `:396` (sabotage fixture string), module docstring counts
- Test (owned): `tests/host/test_touch_targets.py`, `tests/host/test_auth_nav.py`, `tests/host/test_clerk_loader_template.py`, `tests/host/test_security_headers.py`, `tests/host/test_public_cache_headers.py`, `tests/host/test_routing_errors.py`

**Interfaces:**
- Consumes: `lj-tokens.css`, `jc.css`, `fonts.css`, `htmx.min.js` in
  `jobcannon/web/static/` (Tasks 1–4).
- Produces: the restyled layout shell every page extends; `touch_target()` now
  returns `"jc-touch"` / `"jc-touch-box"`.

- [ ] **Step 1: template_globals.py** — replace the `_KIND_TOKENS` values:

```python
_KIND_TOKENS = {
    "block": "jc-touch",
    "checkbox": "jc-touch-box",
}
```

Update the function docstring: the classes are now defined in
`jobcannon/web/static/jc.css` (44px floor), not Tailwind; fix the stale call-site
count to "63 bare + 4 'checkbox' across 20 templates". Keep the ValueError
contract unchanged.

- [ ] **Step 2: security_headers.py** — delete
`"https://cdn.tailwindcss.com"` and `"https://unpkg.com"` from `script_hosts`
(:118) and remove `'unsafe-eval'` from the `script-src` list (:142). Update the
module docstring (:43-45) — the Play CDN justification is gone; scripts are now
`'self'` + the Clerk host. Touch nothing else in the CSP.

- [ ] **Step 3: Rewrite base.html.** Preservation contract (byte-for-byte):
the `{# ... #}` comments, `csrf_meta_tag()`, the Clerk conditional + loader
script block (:16-37), `hx-headers` on `<body>`, the `#jc-error-toast` div's
`id`/`role`/`hidden` and the entire toast script (:59-80), all hrefs,
`data-postings-history-nav-link`, `data-auth-nav`, both `visitor_is_authed`
gates with their comments, the footer's source link with `source_sha_short`
title. What changes: the `<head>` asset tags and every `class` attribute.

Head asset block replacing lines 12–13:

```html
  <link rel="stylesheet" href="{{ url_for('static', filename='lj-tokens.css') }}">
  <link rel="stylesheet" href="{{ url_for('static', filename='fonts.css') }}">
  <link rel="stylesheet" href="{{ url_for('static', filename='jc.css') }}">
  <script src="{{ url_for('static', filename='htmx.min.js') }}"></script>
```

Class replacements:

| Element | New class attribute |
|---|---|
| `<body>` | `class="lj-root"` (keep `hx-headers` attr) |
| `#jc-error-toast` div | `class="jc-toast"` (keep `role="alert" hidden`) |
| `<header>` | `class="jc-header"` |
| wordmark `<span>` | `class="jc-wordmark"` |
| main `<nav>` | `class="jc-nav"` |
| auth `<nav ... data-auth-nav>` | `class="jc-nav jc-nav--end"` |
| every header nav `<a>` | `class="jc-nav-link {{ touch_target() }}"` |
| `<main>` | `class="jc-page"` |
| `<footer>` | `class="jc-footer"` |
| every footer `<a>` | `class="jc-quiet-link {{ touch_target() }}"` |

- [ ] **Step 4: test_touch_targets.py** — update `:241-243` to:

```python
    assert touch_target() == "jc-touch"
    assert touch_target("block") == "jc-touch"
    assert touch_target("checkbox") == "jc-touch-box"
```

Update the `:396` sabotage fixture marker to the new Feed-link line exactly as
written in base.html:

```python
    marker = '<a href="/" class="jc-nav-link {{ touch_target() }}">Feed</a>'
```

(then make base.html's Feed link exactly that — nav links need no extra
utilities now). Update the module docstring's Tailwind references to name
jc.css instead.

- [ ] **Step 5: Run owned tests** (background if slow):
`uv run --active pytest -q --tb=short tests/host/test_touch_targets.py tests/host/test_auth_nav.py tests/host/test_clerk_loader_template.py tests/host/test_security_headers.py tests/host/test_public_cache_headers.py tests/host/test_routing_errors.py`
Expected: pass. Then ruff check/format on the two .py files.

- [ ] **Step 6: Commit** — `feat: Living Journal chrome (base.html, CSP self-host, jc-touch tokens)`

---

## Wave 1 — parallel template migration (Tasks 7–12)

Common rules for every wave-1 task (in addition to Global Constraints):

- Restyle = replace Tailwind `class` values using ONLY the Task 2 vocabulary via
  the Restyle pattern reference. Keep every `{{ touch_target(...) }}` inside the
  `class="..."` it lives in. Preserve all `hx-*`/`data-*`/`id`/ARIA/Jinja
  verbatim. Keep existing element types (don't swap `<div>`→`<li>` etc.).
- Missing component → compose from `jc-stack`/`jc-cluster`/primitives; if truly
  impossible, append a gap bullet to `docs/superpowers/plans/lj-gap-notes.md`
  and move on (Task 13 resolves gaps; only Task 13 may edit jc.css).
- You own ONLY your listed template + test files. Owned tests assert `data-*`
  hooks, not classes — they should pass unchanged; edit one only if it pins
  markup your restyle legitimately changed (sabotage fixtures fail loudly with
  update instructions).
- Stage explicitly (`git add <your files>`, never `-A`); if `index.lock` exists,
  wait 2s and retry (parallel agents share the checkout).
- Finish: run owned tests (`uv run --active pytest -q --tb=short <owned files>`,
  backgrounded if >2 min), then commit `feat: Living Journal restyle — <family>`.

### Task 7: `_posting_row.html` (the feed row)

**Files:** Modify `jobcannon/web/templates/_posting_row.html`. Owned tests:
`tests/host/test_feed_events.py`.

**Interfaces:** the row's root element gets `jc-row` (Task 8's list container
gets `jc-ledger`, which draws the hairline rules between rows — do not add
borders/margins to the row root). Keep `data-posting-row` and every other hook.

Mapping: posting title → `jc-row-title`; employer/location/secondary lines →
`jc-row-sub`; rank/index numeral → `jc-index`; age/score numerals →
`jc-meta` + `jc-meta-num` + `jc-meta-lab`; verified-fresh marker →
`jc-stamp jc-stamp--green` with `<span class="jc-stamp-dot"></span>` (green =
honesty; this and the why-chips are the ONLY green in this file); why section →
wrapper `jc-why` containing `<span class="lj-label">Why this rank</span>` and
the existing `<ul ... data-why-chips>` as `jc-chips` with `jc-chip` items;
actions container (`data-posting-actions`) → `jc-actions`; save/dismiss/apply/
undo buttons → `jc-btn` (ink — never green, never `--primary` for actions);
in-row signup prompt (`data-posting-signup`) → `jc-note` + `jc-btn jc-btn--primary`
link; applied state (`data-apply-applied`) → `jc-cluster` with the drawn-check
SVG (pattern reference) + `jc-note`; degraded notice (`data-apply-degraded`) →
`jc-error-note`; signals-pending (`data-signals-pending`) → `jc-note`.
The ~1200-char `hx-on:click` handler at :159 is untouchable — byte-for-byte.

### Task 8: Feed pages family

**Files:** Modify `demo.html`, `feed.html`, `preview.html`, `_feed_list.html`,
`_feed_page.html`, `_feed_content.html`, `_corpus_empty.html` (all under
`jobcannon/web/templates/`). Owned tests: `tests/host/test_demo_feed.py`,
`tests/host/test_preview.py`, `tests/host/test_pages.py`,
`tests/host/test_feed_pagination.py`, `tests/host/test_feed_clear_selection.py`.

**Interfaces:** the element wrapping the row includes (`data-feed-list` in
`_feed_page.html`) gets `jc-ledger`; rows style themselves (Task 7).

Mapping: each page's `<h1>` → `jc-title` + the accent-rule SVG directly after
it (ONE per page: feed.html, demo.html, preview.html each get exactly one; no
other green in this family); intro paragraphs → `jc-lede`; ordering label
(`data-ordering-label`) → `lj-label`; demo profile card (`data-profile-card`)
→ `jc-panel` with `jc-stack` internals; filter bar (`data-feed-filters`) →
`jc-filterbar`, its selects/inputs → `jc-input`, its labels → `lj-label`;
saved-selection indicator → `jc-note`; clear-selection control → `jc-btn`;
Load more (`data-load-more`) → `jc-btn`; signup CTAs: the full-width demo/
preview CTA (`data-demo-cta`, `data-signup-cta`) → `jc-cta`, inline ones
(`data-action-signup`) → `jc-btn jc-btn--primary`; empty states
(`data-feed-empty`, `data-feed-empty-collision`) → `jc-empty` (heading inside
may use `jc-title`); `_corpus_empty.html` heading → `jc-title`, body →
`jc-lede`; `_feed_content.html` has zero classes — leave untouched.

### Task 9: Postings history pair

**Files:** Modify `postings_history.html`, `_postings_history_list.html`.
Owned tests: `tests/host/test_postings_history.py`.

Mapping: page `<h1>` → `jc-title jc-title--roomy` (no accent rule — this page
has no green); tab strip (`data-postings-history-tabs`) → `jc-cluster`; each
tab link (`data-postings-history-tab`) → `jc-tab` with the existing Jinja
conditional emitting `jc-tab--active` for the active tab, e.g.
`class="jc-tab {{ 'jc-tab--active' if <existing condition> else '' }} {{ touch_target() }}"`
(keep the existing condition expression verbatim); list wrapper
(`data-postings-history-list`) → `jc-ledger`; pagination container → `jc-cluster`;
prev/next links → `jc-btn`; range text (`data-postings-history-range`) →
`jc-note`; empty state (`data-postings-history-empty`) → `jc-empty`.

### Task 10: Onboarding picker family

**Files:** Modify `onboarding_picker.html`, `_picker_options.html`,
`_picker_submitted.html`, `_picker_error_fragment.html`. Owned tests:
`tests/host/test_onboarding.py`.

Mapping: page heading → `jc-title`; explanatory copy → `jc-lede`/`jc-note`;
form field groups → `jc-field`, their labels → `lj-label`; text/select
controls incl. the search input (`data-picker-search`) → `jc-input`; the
option list (`id="picker-options"`) → `jc-stack`; each option row's `<label>`
→ `jc-cluster {{ touch_target() }}`; the checkbox `<input>` keeps
`{{ touch_target('checkbox') }}` and gains no class (theming is global);
option secondary text → `jc-note`; submit/continue → `jc-btn jc-btn--primary`;
other buttons/links → `jc-btn`/`jc-link`; `_picker_submitted.html` → `jc-stamp`
(ink) + `jc-note` + `jc-link` for the change link; `_picker_error_fragment.html`
→ `jc-error-note` (keep `data-picker-error`).

### Task 11: Consent + account family

**Files:** Modify `consent.html`, `consent_signed_out.html`,
`_consent_panel.html`, `account_delete.html`, `account_deleted.html`.
Owned tests: `tests/host/test_consent_route.py`,
`tests/host/test_account_route.py`, `tests/host/test_account_export.py`.

Mapping: headings → `jc-title`; body copy → `jc-lede`/`jc-note`; the consent
panel (`id="consent-panel"`) → `jc-panel` with `jc-stack` internals; accept →
`jc-btn jc-btn--primary`, decline → `jc-btn` (both ink — no green: consent is
an action, not an honesty signal); confirmation (`data-consent-confirmation`)
→ `jc-stamp` (ink) + `jc-note`; signed-out consent links → `jc-link`;
account_delete warning text (`text-red-400`) → `jc-error-note`; its confirm
checkbox keeps `touch_target('checkbox')` unchanged; the delete button →
`jc-btn` (ink — destructive is still ink); `account_deleted.html` heading →
`jc-title`, body → `jc-lede`.

### Task 12: Errors + legal family

**Files:** Modify `error.html`, `error_401.html`, `error_csrf.html`,
`_csrf_error_fragment.html`, `legal_page.html`. Owned tests:
`tests/host/test_routing_errors.py`, `tests/host/test_csrf.py`,
`tests/host/test_legal_pages.py`, `tests/host/test_legal_table_scroll.py`,
`tests/host/test_clerk_loader_template.py`, `tests/host/test_legal_list_looseness.py`.

Error pages: headings → `jc-title`; explanation → `jc-lede`; links → `jc-link`
or `jc-btn`; `error_401.html`'s inline `<script>` block is pinned by
`test_clerk_loader_template.py` — byte-for-byte untouchable;
`_csrf_error_fragment.html` + `error_csrf.html` error text → `jc-error-note`
(keep `data-csrf-error`).

`legal_page.html` — the `<style>` block STAYS INLINE (Survey addendum #3) and
is restyled in place: every color literal → the matching `var(--lj-*)`
(`#737373`-class grays → `var(--lj-gray)`; body text → `var(--lj-ink)`;
secondary → `var(--lj-gray-text)`; borders/rules → `var(--lj-hair)` /
`var(--lj-rule)`); h1/h2/h3 gain `font-family: var(--lj-serif)`. MUST keep:
the `.legal-prose a` padding-top/padding-bottom declarations with their current
px values (test-parsed, sum >= 26), `.table-scroll { overflow-x: auto; ... }`,
`.table-scroll:focus-visible { outline: 2px solid var(--lj-gray); outline-offset: 2px; }`
(same property shapes), and ZERO `@media` rules. Then update the sabotage
fixture strings in `test_legal_table_scroll.py` that string-match the old
declarations (they fail loudly naming themselves — e.g.
`"outline: 2px solid #737373;"` → `"outline: 2px solid var(--lj-gray);"`).
Classes `legal-prose`/`table-scroll` stay as-is.

---

### Task 13: Integration — closure tests + gap resolution + full suite

**Files:** Create `tests/test_design_templates.py`; resolve
`docs/superpowers/plans/lj-gap-notes.md` (may edit `jobcannon/web/static/jc.css`
— sole writer at this stage); fix any cross-family breakage
(`tests/host/test_empty_states.py`, `tests/host/test_day1_stranger_e2e.py` are
owned here).

- [ ] **Step 1:** `tests/test_design_templates.py`:

```python
"""Structural guards for the Living Journal template layer (spec §7.3–7.5).

1. Class closure: every class a template references must be defined in jc.css
   (or allowlisted) — catches invented names and Tailwind leftovers at once.
2. No color literals in templates (legal_page.html's inline style composes
   from var(--lj-*); literal channels are banned there too).
3. No CDN remnants.
"""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "jobcannon" / "web"
TEMPLATES = sorted(WEB.glob("templates/**/*.html"))

_DEFINED_RE = re.compile(r"\.([a-zA-Z][\w-]*)")
_CLASS_ATTR_RE = re.compile(r'class="([^"]*)"')
_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_ALLOWED_PREFIXES = ("htmx-",)  # htmx runtime classes
_ALLOWED = {"legal-prose", "table-scroll"}  # defined in legal_page.html's inline style


def _defined_classes() -> set[str]:
    css = (WEB / "static" / "jc.css").read_text(encoding="utf-8")
    return set(_DEFINED_RE.findall(css))


def _referenced() -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for path in TEMPLATES:
        text = _JINJA_RE.sub(" ", path.read_text(encoding="utf-8"))
        tokens = {
            tok
            for attr in _CLASS_ATTR_RE.findall(text)
            for tok in attr.split()
        }
        if tokens:
            refs[path.name] = tokens
    return refs


def test_template_classes_close_over_jc_css():
    defined = _defined_classes() | _ALLOWED
    problems = {
        name: sorted(
            tok
            for tok in tokens
            if tok not in defined and not tok.startswith(_ALLOWED_PREFIXES)
        )
        for name, tokens in _referenced().items()
    }
    problems = {k: v for k, v in problems.items() if v}
    assert not problems, f"classes referenced but not defined in jc.css: {problems}"


def test_scan_found_a_plausible_number_of_classes():
    total = sum(len(v) for v in _referenced().values())
    assert total >= 40, "class scan collapsed — check the regexes"


_LITERAL_PATTERNS = (
    re.compile(r"#[0-9a-fA-F]{3,8}\b"),
    re.compile(r"\b(?:rgb|rgba|hsl|hsla|oklch|oklab)\(\s*\d"),
)


def test_templates_have_no_color_literals():
    hits = []
    for path in TEMPLATES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(p.search(line) for p in _LITERAL_PATTERNS):
                hits.append((path.name, i, line.strip()))
    assert not hits, f"color literals in templates: {hits}"


def test_no_cdn_remnants():
    hits = [
        p.name
        for p in TEMPLATES
        if "cdn.tailwindcss.com" in (t := p.read_text(encoding="utf-8"))
        or "unpkg.com" in t
    ]
    assert not hits, f"CDN references remain: {hits}"
```

- [ ] **Step 2:** If `lj-gap-notes.md` exists, resolve each gap: add the
  minimal component to jc.css (respecting identity rules; then update the Task 2
  vocabulary list in this plan is NOT needed — the closure test is the source of
  truth) and apply it in the affected template; delete the notes file.
- [ ] **Step 3:** Run the FULL suite, backgrounded:
  `uv run --active pytest -q --tb=short` — fix every failure caused by this
  branch (class-closure catches undefined names; sabotage fixtures name their
  own updates). `uv run ruff check . && uv run ruff format .` for touched .py.
- [ ] **Step 4:** Commit — `test: Living Journal structural guards (closure, literals, CDN sweep)`

### Task 14: Visual review artifacts (main session) → USER GATE

Main session, after Task 13: boot the app with a throwaway DB (reuse the
`tests/host/conftest.py` approach) on `127.0.0.1:5001`, screenshot the public
routes (`/demo`, `/preview`, `/start`, `/privacy`, `/consent`, a 404) in BOTH
themes via Playwright `color_scheme` emulation
(`uv run --no-sync --with playwright python scripts/design_review_shots.py`
after `python -m playwright install chromium`), save to the scratchpad, and
send to the user beside swole's `reference.html`. **The user's eyeball pass +
push/PR approval are the only human gates.** Push and `gh pr create` happen
only after explicit user go-ahead.

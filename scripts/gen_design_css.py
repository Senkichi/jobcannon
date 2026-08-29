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
REFERENCE_ONLY: frozenset[str] = frozenset(
    {
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
    }
)

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
        + "\n:root {\n"
        + "\n".join(light)
        + "\n}\n"
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

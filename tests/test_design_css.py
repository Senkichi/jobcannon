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

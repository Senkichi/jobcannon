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
        "lj-tokens.css is stale or hand-edited. Run: python scripts/gen_design_css.py"
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

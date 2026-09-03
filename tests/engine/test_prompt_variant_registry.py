"""Tests: jobcannon.engine.scoring_prompts.registry resolvers.

Ledger L-0057 (ADAPT). Adapted from the private job-cannon
tests/test_prompt_variant_guard.py::TestValidatePromptVariant for the pure
resolvers ported here (variant_name / resolve_variant_module /
list_prompt_variants). The private file's other assertions -- on
validate_prompt_variant, create_app, the settings blueprint, and the CLI
entrypoint -- are NOT carried; see registry.py's PORT-SEAM comments and the
PR body for why each is host-only and has no engine-side landing site yet.
"""

from __future__ import annotations

import sys
import types

import pytest

from jobcannon.engine.scoring_prompts.registry import (
    BASELINE_VARIANT,
    list_prompt_variants,
    resolve_variant_module,
    variant_name,
)


def test_none_config_defaults_to_baseline():
    assert variant_name(None) == BASELINE_VARIANT


def test_empty_config_defaults_to_baseline():
    assert variant_name({}) == BASELINE_VARIANT


def test_missing_scoring_key_defaults_to_baseline():
    assert variant_name({"scoring": {}}) == BASELINE_VARIANT


def test_baseline_resolves_to_v3_scoring_prompt_module():
    from jobcannon.engine.scoring_prompts import v3_scoring_prompt as v3

    assert resolve_variant_module(BASELINE_VARIANT) is v3


def test_shipped_variant_is_resolvable():
    # Every name list_prompt_variants() discovers must itself resolve.
    for name in list_prompt_variants():
        resolve_variant_module(name)  # must not raise


def test_unknown_variant_raises_import_error_naming_value():
    with pytest.raises(ImportError, match="does_not_exist_v4xxx"):
        resolve_variant_module("does_not_exist_v4xxx")


def test_injected_variant_module_resolves_without_appearing_in_list(monkeypatch):
    """Resolution is import-based, not list-based.

    Mirrors the private guard test of the same name (ledger L-0057):
    planting a fake module in sys.modules under
    jobcannon.engine.scoring_prompts.variants.<name> makes
    resolve_variant_module accept that name via importlib.import_module
    even though it was never written to disk under variants/.
    list_prompt_variants() is pkgutil-based (disk discovery only), so the
    injected name deliberately does not show up there.
    """
    fixture_name = "fixture_variant_registry_injected"
    fake_mod = types.ModuleType(f"jobcannon.engine.scoring_prompts.variants.{fixture_name}")
    monkeypatch.setitem(
        sys.modules,
        f"jobcannon.engine.scoring_prompts.variants.{fixture_name}",
        fake_mod,
    )

    assert resolve_variant_module(fixture_name) is fake_mod
    assert fixture_name not in list_prompt_variants()


def test_list_prompt_variants_contains_baseline_and_excludes_private_helpers():
    names = list_prompt_variants()
    assert "baseline" in names
    assert not any(name.startswith("_") for name in names)

"""Sanity: every variant module exports the four required names.

This is a contract test for the variants subpackage. New variants
authored in scoring_prompts/variants/ are auto-discovered via
pkgutil and verified to expose the names the harness needs.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import jobcannon.engine.scoring_prompts.variants as variants_pkg

REQUIRED_NAMES: tuple[str, ...] = (
    "JOB_ASSESSMENT_SCHEMA",
    "V3_SCORING_PROMPT",
    "FIELD_REINFORCEMENT",
    "FEWSHOT_EXAMPLES",
)


def _discover_variant_names() -> list[str]:
    return [
        m.name for m in pkgutil.iter_modules(variants_pkg.__path__) if not m.name.startswith("_")
    ]


def test_variants_directory_is_non_empty():
    """At minimum, the baseline alias must be present."""
    names = _discover_variant_names()
    assert "baseline" in names, f"baseline alias missing from variants/, found: {names}"


@pytest.mark.parametrize("name", _discover_variant_names())
def test_variant_exports_required_names(name: str):
    """Every variant module exposes the four-name contract."""
    mod = importlib.import_module(f"jobcannon.engine.scoring_prompts.variants.{name}")
    for attr in REQUIRED_NAMES:
        assert hasattr(mod, attr), f"Variant {name!r} missing required name {attr!r}"


@pytest.mark.parametrize("name", _discover_variant_names())
def test_variant_resolves_through_job_scorer(name: str):
    """The job_scorer's resolver loads each variant without error."""
    from jobcannon.engine.job_scorer import _resolve_variant_module

    mod = _resolve_variant_module(name)
    assert hasattr(mod, "JOB_ASSESSMENT_SCHEMA")
    assert hasattr(mod, "V3_SCORING_PROMPT")

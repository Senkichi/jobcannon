# PORTED from tests/test_provider_cross_provider.py @ f456890cd4e05d7ab6398758d5f73064e3df6b63 (private job-cannon). Ledger L-0538.
"""Cross-provider integration & regression tests.

# PORT-SEAM: private locked a Phase-39 cross-cutting contract over six
# production providers (anthropic/gemini/ollama/claude_code_cli/gemini_cli/
# local_bundled), most of it via a parametrized `.call()` shape/behavior
# check over four fully-mocked adapter factories plus a source-grep of
# private provider files for shell=True / missing timeout=. All of that is
# dropped: test_call_returns_canonical_model_result_shape,
# test_call_returns_expected_provider_name, test_free_providers_record_zero_cost,
# test_no_schema_returns_schema_valid_true (parametrize exclusively over
# ollama/claude_code_cli/gemini_cli/local_bundled -- ollama is HOLD
# (jobcannon #338), the other three are DIES; this host's `_make_adapter`
# only builds gemini/groq/cerebras -- jobcannon/host/model_provider.py);
# test_no_shell_true_in_new_provider_files, test_every_subprocess_run_has_timeout_kwarg
# (grep private paths under job_finder/web/providers/, none of which exist
# on this host); test_make_adapter_dispatches_claude_code_cli,
# test_make_adapter_dispatches_gemini_cli,
# test_make_adapter_local_bundled_requires_model_path,
# test_make_adapter_local_bundled_with_model_path_constructs_provider,
# test_all_new_providers_are_base_provider_subclasses (each imports a DIES
# provider module directly).
#
# Survives: the roster-membership invariants over
# jobcannon.host.provider_catalog.PROVIDER_DEFAULTS, which still enumerates
# the full provider roster (including the DIES/HOLD names as data, not as
# dispatchable adapters) as PROVIDER_DEFAULTS' single source of truth (L-0051).

See per-provider files (tests/test_provider_claude_code_cli.py, etc.) for
detailed behavior coverage. This file deliberately keeps assertions thin
and focused on the cross-cutting invariants only.
"""

from __future__ import annotations

# PORT-SEAM: _PROVIDER_DEFAULTS relocated from
# job_finder.web.model_provider._PROVIDER_DEFAULTS to
# jobcannon.host.provider_catalog.PROVIDER_DEFAULTS (L-0051 / L-0036),
# aliased back to its original name here so every use site below carries
# unchanged. BaseProvider/ModelResult/_make_adapter imports dropped with the
# tests that used them; see module docstring PORT-SEAM note.
from jobcannon.host.provider_catalog import PROVIDER_DEFAULTS as _PROVIDER_DEFAULTS

# ---------------------------------------------------------------------------
# _PROVIDER_DEFAULTS membership invariant
# ---------------------------------------------------------------------------


def test_provider_defaults_is_superset_of_six_production_providers():
    expected = {
        "anthropic",
        "gemini",
        "ollama",
        "claude_code_cli",
        "gemini_cli",
        "local_bundled",
    }
    assert expected <= set(_PROVIDER_DEFAULTS)


def test_provider_defaults_includes_groq_and_cerebras():
    assert "groq" in _PROVIDER_DEFAULTS
    assert "cerebras" in _PROVIDER_DEFAULTS


def test_every_provider_default_has_quick_and_score_workloads():
    for name, mapping in _PROVIDER_DEFAULTS.items():
        assert "quick" in mapping, f"{name} missing 'quick' default"
        assert "score" in mapping, f"{name} missing 'score' default"


# PORT-SEAM: dropped from here to EOF (private-only surfaces) --
# test_no_shell_true_in_new_provider_files, test_every_subprocess_run_has_timeout_kwarg
# (source-grep private paths under job_finder/web/providers/, none of which
# exist on this host); test_make_adapter_dispatches_claude_code_cli,
# test_make_adapter_dispatches_gemini_cli,
# test_make_adapter_local_bundled_requires_model_path,
# test_make_adapter_local_bundled_with_model_path_constructs_provider,
# test_all_new_providers_are_base_provider_subclasses (each imports a DIES
# provider module directly, or calls this host's `_make_adapter`, whose
# signature and dispatch set are unrelated -- see module docstring).

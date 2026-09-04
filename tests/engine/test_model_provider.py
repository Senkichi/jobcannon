# PORTED from tests/test_model_provider.py @ 9678c44c5d667d8a1d587c2d1f92b9df4056ead9 (private job-cannon). Ledger L-0533.
# PORT-SEAM: private's job_finder.web.model_provider is a single-user,
# owner-config.yaml-driven dispatcher -- resolve_provider_config /
# resolve_workload_routing tier resolution, an adapter memoization cache,
# cost_gate/BudgetExceededError/FREE_PROVIDERS budget gating, per-day usage
# tracking, and a 6-provider cascade including ollama/claude_code_cli/
# anthropic. None of that carries: jobcannon.host.model_provider's module
# docstring says hosted routing is resolve_hosted_routing, driven by a
# tenant's byo_key_credentials rather than a config file, with no
# budget/cascade/adapter-cache surface at all, and an unrelated call_model
# signature; jobcannon.engine.model_types carries only the pure
# ModelResult/BaseProvider contract, unchanged. tests/host/test_model_provider.py
# (native, PR #337) already covers resolve_hosted_routing and real
# gemini/groq/cerebras adapter construction. This file carries only the two
# data/ABC contract tests below, which have no hosted analog to duplicate
# and are not private-only surfaces.
#
# Dropped (104 of 108): every test that parametrizes over or asserts
# resolve_provider_config, resolve_workload_routing, the old
# _make_adapter(name, config=...) signature, _ADAPTER_CACHE memoization,
# cost_gate/BudgetExceededError/FREE_PROVIDERS, daily-usage-limit tracking,
# or the ollama/claude_code_cli/anthropic dispatch branches -- all
# private-only surfaces per the module docstring above. See this PR's body
# for the itemized list.
import pytest

# PORT-SEAM: `import requests` dropped -- unused by any surviving test.
from jobcannon.engine.model_types import BaseProvider, ModelResult


def test_model_result_fields():
    result = ModelResult(
        data={"score": 75},
        cost_usd=0.01,
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4-6",
        provider="anthropic",
        schema_valid=True,
    )
    assert result.data == {"score": 75}
    assert result.cost_usd == 0.01
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.model == "claude-sonnet-4-6"
    assert result.provider == "anthropic"
    assert result.schema_valid is True


def test_model_result_is_frozen():
    from dataclasses import FrozenInstanceError

    result = ModelResult(
        data={"score": 75},
        cost_usd=0.01,
        input_tokens=100,
        output_tokens=50,
        model="claude-sonnet-4-6",
        provider="anthropic",
        schema_valid=True,
    )
    with pytest.raises(FrozenInstanceError):
        result.data = {"score": 99}


# PORT-SEAM: dropped the private file's `# --- ModelResult tests ---` /
# `# --- BaseProvider tests ---` section-separator comments (cosmetic, not
# semantic).
def test_base_provider_is_abstract():
    with pytest.raises(TypeError):
        BaseProvider()


def test_base_provider_subclass_must_implement_call():
    class IncompleteProvider(BaseProvider):
        pass

    with pytest.raises(TypeError):
        IncompleteProvider()


# PORT-SEAM: resolve_provider_config / resolve_workload_routing / call_model
# dispatcher tests (104 functions) dropped here -- private-only surfaces per
# the module header above; itemized in this PR's body.

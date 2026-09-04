# PORTED from tests/test_workload_routing.py @ 22edeca885161eb1b58b30f7a724861a7ded81cf (private job-cannon). Ledger L-0547.
# PORT-SEAM: private pins the owner-config.yaml-driven `resolve_workload_routing`
# (primary/fallback_chain/overrides parsed from a single-user config file) --
# that function does not carry hosted (jobcannon/host/model_provider.py's
# module docstring: hosted has no config.yaml at all; routing is
# `resolve_hosted_routing`, driven by a tenant's active byo_key_credentials
# instead, and is already covered by tests/host/test_model_provider.py).
# Dropped (assert exclusively on resolve_workload_routing, which does not
# exist on this host): test_resolve_routing_claude_code_cli_quick_returns_haiku,
# test_resolve_routing_claude_code_cli_score_returns_sonnet,
# test_resolve_routing_triage_uses_quick_model,
# test_resolve_routing_cascade_per_workload, test_resolve_routing_honors_overrides,
# test_unknown_workload_raises (the unknown-tier ValueError invariant this one
# checks is already covered on the hosted routing function by
# test_resolve_hosted_routing_unknown_tier_raises_value_error in
# tests/host/test_model_provider.py). `import pytest` drops with it -- no
# surviving test in this file raises.
#
# Survives: the workload-vocabulary and roster-shape invariants, which are
# data-level and hold unchanged against jobcannon.host.model_provider
# ._VALID_WORKLOADS and jobcannon.host.provider_catalog.PROVIDER_DEFAULTS
# (aliased back to _PROVIDER_DEFAULTS, L-0051, so every use site carries
# unchanged).
#
# Ledger L-0546 (tests/test_tier_rename_no_vestigial_labels.py) is a
# redundant carry of this file's first and last surviving assertion (its
# own adjudicator note authorizes dropping it as redundant rather than
# duplicating this file) and is not carried separately -- see this PR's body.
from jobcannon.host.model_provider import _VALID_WORKLOADS
from jobcannon.host.provider_catalog import PROVIDER_DEFAULTS as _PROVIDER_DEFAULTS


def test_valid_workloads_are_quick_score_triage():
    assert {"quick", "score", "triage", "craft"} == _VALID_WORKLOADS


def test_provider_defaults_cover_all_workloads_for_all_providers():
    expected = {"claude_code_cli", "gemini", "gemini_cli", "ollama", "anthropic", "local_bundled"}
    assert set(_PROVIDER_DEFAULTS) >= expected
    for provider, mapping in _PROVIDER_DEFAULTS.items():
        assert set(mapping) >= {"quick", "score", "craft"}, f"{provider} missing quick/score/craft"


# PORT-SEAM: test_resolve_routing_claude_code_cli_quick_returns_haiku,
# test_resolve_routing_claude_code_cli_score_returns_sonnet,
# test_resolve_routing_triage_uses_quick_model,
# test_resolve_routing_cascade_per_workload, test_resolve_routing_honors_overrides,
# and test_unknown_workload_raises dropped here -- see module docstring.
def test_legacy_tier_names_no_longer_in_defaults():
    # Sanity: the haiku/sonnet/opus/low/mid/high aliases are gone.
    flat_keys: set[str] = set()
    for mapping in _PROVIDER_DEFAULTS.values():
        flat_keys.update(mapping.keys())
    assert flat_keys.isdisjoint({"low", "mid", "high", "haiku", "sonnet", "opus"})

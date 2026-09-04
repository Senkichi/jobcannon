# PORTED from tests/test_provider_catalog_single_source.py @ 6b797a8bf492184243247376e9de18643570e146 (private job-cannon). Ledger L-0549.
"""Guard: the LLM provider roster has ONE source of truth (provider_catalog).

# PORT-SEAM: private's four derived-enumeration consumers were
# model_provider._SUPPORTED_PROVIDERS/_PROVIDER_DEFAULTS,
# claude_client.FREE_PROVIDERS, and settings._PROVIDER_KEY_FIELDS --
# the Issue-303 spend-accounting incident (a transport mis-flagged free) is
# why this guard exists. jobcannon.host.provider_catalog is a verbatim PORT
# of the catalog itself (L-0051); claude_client and blueprints.settings are
# DIES (owner-budget accounting / desktop settings UI have no hosted
# meaning), and jobcannon.host.model_provider does not re-export
# _SUPPORTED_PROVIDERS/_PROVIDER_DEFAULTS aliases the way private's
# model_provider.py does -- so test_consumers_derive_from_catalog, which
# imports all three, is dropped: no consumer-parity check is possible
# without inventing a fictional consumer. Every other test below is
# internal to the catalog module itself and holds unchanged against
# jobcannon.host.provider_catalog, whose roster and derivation logic are
# byte-identical to private's.
#
# PORT-SEAM (re-carry, round 2): private main advanced to 6b797a8b (#2085)
# after the first carry SHA e6c17c32, adding a `secret_key_name` field to
# ProviderSpec plus `secret_key_name_for()` and 3 new tests pinning them
# (test_secret_key_name_pins,
# test_secret_key_name_for_returns_value_for_keyed_providers,
# test_secret_key_name_for_returns_none_for_keyless_providers). That surface
# has NOT been ported to jobcannon.host.provider_catalog (L-0051 predates
# #2085) -- not a DIES verdict, just not-yet-ported; porting the feature
# itself is out of scope for this test-carry PR. Dropped here; flagged in
# this PR's body for a future L-0051 refresh.

(test_model_provider already pins _make_adapter <-> _SUPPORTED_PROVIDERS and
_PROVIDER_DEFAULTS <= _SUPPORTED_PROVIDERS; those become structural here.)
"""

from __future__ import annotations

import pytest

# PORT-SEAM: from jobcannon.host import provider_catalog as cat (relocated
# from job_finder.web.provider_catalog, L-0051); test_consumers_derive_from_catalog
# dropped here -- see module docstring PORT-SEAM note.
from jobcannon.host import provider_catalog as cat


def test_defaults_are_subset_of_roster():
    assert set(cat.PROVIDER_DEFAULTS) <= set(cat.SUPPORTED_PROVIDERS)


def test_key_fields_reference_roster_providers():
    names = {name for name, _label in cat.PROVIDER_KEY_FIELDS}
    assert names <= set(cat.SUPPORTED_PROVIDERS)


def test_free_minus_roster_is_exactly_the_nonadapter_labels():
    """The only FREE names that are not adapter providers are the documented
    non-adapter cost labels (claude_cli, google_cse)."""
    assert (cat.FREE_PROVIDER_NAMES - cat.SUPPORTED_PROVIDERS) == cat._EXTRA_FREE_LABELS


def test_cost_correctness_flag_pins():
    """Issue 303: a provider mis-flagged free/paid silently mis-accounts spend.
    Pin the dangerous flags explicitly so a careless edit trips a test."""
    is_free = {p.name: p.is_free for p in cat.PROVIDERS}
    assert is_free["anthropic"] is True  # subscription OAuth ($0)
    assert is_free["anthropic_api"] is False  # API-key transport (paid)
    assert is_free["groq"] is False
    assert is_free["cerebras"] is False
    assert is_free["openrouter"] is False
    assert is_free["ollama"] is True
    assert is_free["gemini"] is True


def test_openrouter_dispatchable_but_not_a_cascade_default():
    """openrouter is in the roster (eval-judge dispatch) but intentionally has
    no production default (defaults=None -> absent from PROVIDER_DEFAULTS)."""
    assert "openrouter" in cat.SUPPORTED_PROVIDERS
    assert "openrouter" not in cat.PROVIDER_DEFAULTS


def test_all_providers_with_defaults_have_craft_entry():
    """Issue #796: every provider with non-None defaults must have a 'craft' entry."""
    for provider_name, defaults in cat.PROVIDER_DEFAULTS.items():
        assert "craft" in defaults, f"Provider {provider_name} is missing 'craft' entry in defaults"


def test_known_cli_providers_have_cli_binary_pinned():
    """Issue #1437: cli_binary is the single source
    tests/test_subprocess_lockdown_adoption.py's CLI-binary-spawn guard
    derives its deny-list from (via cat.cli_binaries()). A CLI provider
    silently missing cli_binary would make that guard blind to direct spawns
    of its binary under tests/ -- pin the known CLI-shelling providers
    explicitly, mirroring test_cost_correctness_flag_pins' rationale for the
    Issue 303 free/paid mis-flag."""
    cli_binary = {p.name: p.cli_binary for p in cat.PROVIDERS}
    assert cli_binary["claude_code_cli"] == "claude"
    assert cli_binary["gemini_cli"] == "gemini"
    assert cli_binary["ollama"] == "ollama"
    # "anthropic"/"anthropic_api" ALSO shell out to the "claude" binary (via
    # claude_client._run_oneshot, the same transport claude_code_cli uses) --
    # both must set cli_binary too, or the derived deny-list would lose
    # "claude" coverage the moment claude_code_cli alone were ever removed or
    # renamed. See ProviderSpec.cli_binary's docstring.
    assert cli_binary["anthropic"] == "claude"
    assert cli_binary["anthropic_api"] == "claude"
    # HTTP/API-key transports that never shell out to any binary must stay
    # None -- a "helpful" binary name here would falsely widen the CLI-binary
    # guard's deny-list.
    assert cli_binary["gemini"] is None
    assert cli_binary["groq"] is None
    assert cli_binary["cerebras"] is None
    assert cli_binary["openrouter"] is None
    assert cli_binary["local_bundled"] is None


def test_cli_binaries_derived_view_matches_pinned_spec_fields():
    """cat.cli_binaries() must equal exactly the non-None cli_binary values
    across PROVIDERS -- pins the derivation itself, not just the individual
    ProviderSpec fields test_known_cli_providers_have_cli_binary_pinned checks."""
    expected = {p.cli_binary for p in cat.PROVIDERS if p.cli_binary is not None}
    assert cat.cli_binaries() == expected == {"claude", "gemini", "ollama"}


def test_require_cli_binary_returns_value_for_configured_providers():
    """require_cli_binary() is the single enforcement point every CLI-shelling
    call site (claude_code_cli.py, gemini_cli.py, detection.py,
    claude_client.py) uses instead of a local `assert ... is not None` --
    issue #1437 review follow-up. Confirm it returns the same values
    cli_binary_for() does for every provider known to carry one."""
    for provider_name, expected_binary in (
        ("claude_code_cli", "claude"),
        ("gemini_cli", "gemini"),
        ("ollama", "ollama"),
        ("anthropic", "claude"),
        ("anthropic_api", "claude"),
    ):
        assert cat.require_cli_binary(provider_name) == expected_binary


def test_require_cli_binary_raises_for_provider_with_no_cli_binary():
    """A provider with cli_binary=None (or an unknown name) must raise, never
    silently return None into a shutil.which(None) call three frames up."""
    with pytest.raises(ValueError, match=r"gemini.*cli_binary"):
        cat.require_cli_binary("gemini")
    with pytest.raises(ValueError, match="cli_binary"):
        cat.require_cli_binary("not_a_real_provider")


# PORT-SEAM: dropped from here to EOF (private #2085, carry SHA 6b797a8b) --
# test_secret_key_name_pins,
# test_secret_key_name_for_returns_value_for_keyed_providers,
# test_secret_key_name_for_returns_none_for_keyless_providers (all assert on
# ProviderSpec.secret_key_name / cat.secret_key_name_for(), a surface
# jobcannon.host.provider_catalog does not yet carry -- see module docstring
# PORT-SEAM note).

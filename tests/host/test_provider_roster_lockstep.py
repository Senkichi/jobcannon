"""Lockstep guard: provider_catalog's roster vs. model_provider's
HOSTED_ELIGIBLE_PROVIDERS + _make_adapter dispatch branches (issue #331).

Native (not ported) -- the private repo had the same two-source split with
no guard; this file is the follow-up the L-0036 port's Modularity note
item 2 deferred.

``jobcannon.host.provider_catalog.PROVIDERS`` is the single source of truth
for which providers exist and their properties; ``model_provider`` reads
its ``PROVIDER_DEFAULTS`` derived view but keeps TWO hand-maintained
rosters of its own: the ``HOSTED_ELIGIBLE_PROVIDERS`` preference tuple and
``_make_adapter``'s per-provider construction branches. A provider added to
one side and not the other half-registers silently:

- eligible-but-uncataloged / eligible-but-no-defaults: the name survives
  call_model's HOSTED_ELIGIBLE_PROVIDERS intersection yet
  resolve_hosted_routing can never select it (it filters on
  ``PROVIDER_DEFAULTS[p][tier]``);
- eligible-but-undispatchable: ``_make_adapter``'s fallthrough
  "No adapter dispatch branch" ValueError fires only at call time, where
  call_model's ``except (ValueError, RuntimeError, ImportError)`` catches
  it and degrades to a silent cascade skip;
- cataloged-but-ineligible: a BYO-keyable, pure-REST provider with cascade
  defaults that nobody adds to HOSTED_ELIGIBLE_PROVIDERS lets a tenant
  register a credential the cascade will never use.

The set that *should* be hosted-eligible is derivable from the catalog's
own spec fields -- a provider is hosted-eligible iff it is BYO-keyable
(``key_label`` set: byo_key_credentials API keys are the only hosted
credential mechanism), reachable by the cascade (``defaults`` non-None:
ProviderSpec's documented marker for "part of the scoring cascade"), and
pure-REST (no ``cli_binary`` and not ``is_local`` -- the host cannot spawn
a CLI binary or run a local model). Deriving it keeps this guard free of a
second hand-maintained provider list: today it evaluates to exactly
{gemini, groq, cerebras} (anthropic/anthropic_api excluded via
cli_binary="claude", openrouter via defaults=None, ollama/local_bundled/
gemini_cli/claude_code_cli via key_label/is_local/cli_binary).
"""

from __future__ import annotations

import pytest

from jobcannon.engine.model_types import BaseProvider
from jobcannon.host import model_provider as mp
from jobcannon.host import provider_catalog as cat


def _derived_hosted_eligible() -> set[str]:
    """Catalog providers that satisfy every hosted-eligibility requirement."""
    return {
        p.name
        for p in cat.PROVIDERS
        if p.key_label is not None
        and p.defaults is not None
        and p.cli_binary is None
        and not p.is_local
    }


def test_hosted_eligible_providers_have_no_duplicates():
    """HOSTED_ELIGIBLE_PROVIDERS is an ordered preference tuple; a duplicated
    name would put the same provider twice into one fallback chain."""
    assert len(mp.HOSTED_ELIGIBLE_PROVIDERS) == len(set(mp.HOSTED_ELIGIBLE_PROVIDERS))


def test_hosted_eligible_providers_equal_derived_catalog_set():
    """The lockstep invariant, both directions at once:

    - a name in HOSTED_ELIGIBLE_PROVIDERS that fails any catalog-side
      requirement (not registered, not BYO-keyable, no cascade defaults, or
      a CLI/local transport the host cannot run) is unreachable dead
      config;
    - a catalog provider meeting every requirement but absent from the
      tuple is BYO-keyable with cascade defaults yet never dispatchable --
      the settings-listing/cascade-reachability split this guard exists to
      close.

    A provider deliberately excluded must fail one conjunct in its own
    ProviderSpec row (e.g. openrouter's defaults=None marks it
    dispatch-only), not be silently dropped from the tuple.
    """
    assert set(mp.HOSTED_ELIGIBLE_PROVIDERS) == _derived_hosted_eligible()


def test_every_hosted_eligible_provider_has_models_for_each_hosted_tier():
    """defaults non-None admits a spec like {"quick": None} that is still
    unreachable per-tier -- pin a non-None model for every tier the hosted
    cascade serves. "triage" is deliberately unhosted today (pinned by
    test_model_provider.test_resolve_hosted_routing_triage_has_no_hosted_defaults)."""
    for name in mp.HOSTED_ELIGIBLE_PROVIDERS:
        defaults = cat.PROVIDER_DEFAULTS.get(name)
        assert defaults is not None, f"{name} is hosted-eligible but has defaults=None"
        for tier in ("quick", "score", "craft"):
            assert defaults.get(tier), f"{name} is hosted-eligible with no {tier!r} model"


@pytest.mark.parametrize("provider_name", mp.HOSTED_ELIGIBLE_PROVIDERS)
def test_make_adapter_dispatches_every_hosted_eligible_provider(provider_name):
    """Parametrized over HOSTED_ELIGIBLE_PROVIDERS itself -- not a
    hand-listed subset -- so adding a name to the tuple without a matching
    _make_adapter branch fails here instead of surfacing as a silent
    cascade skip at call time."""
    adapter = mp._make_adapter(provider_name, {}, lambda provider: "test-key")
    assert isinstance(adapter, BaseProvider)

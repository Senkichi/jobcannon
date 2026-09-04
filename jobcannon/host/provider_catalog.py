"""PORTED from job_finder/web/provider_catalog.py @ e6c17c32051602be4b565becb71486f2a4c10ed1
(private job-cannon). Ledger L-0051.
# PORT-SEAM: verbatim PORT (not ADAPT) -- ledger L-0051's own adjudication:
# "desktop CLI providers in the roster? Enumerating a provider is config
# data, not a coupling -- hosted config just omits them from
# fallback_chain." Hosted-eligibility ({gemini, groq, cerebras}) is
# enforced where design-providers-byokey.md §1c puts it: the HOST's chain
# builder (jobcannon/host/model_provider.py's ``resolve_hosted_routing``),
# which is the roster's only hosted consumer -- not by trimming this file.
# Trimming would also break this module's own single-source-of-truth
# contract (FREE_PROVIDER_NAMES / PROVIDER_KEY_FIELDS / cli_binaries() all
# derive from the full PROVIDERS tuple) for zero behavioral benefit.

Single source of truth for the LLM provider roster + per-provider properties.

The provider roster used to be re-enumerated in five places that had to be kept
in sync by hand: ``model_provider._SUPPORTED_PROVIDERS``,
``model_provider._PROVIDER_DEFAULTS``, the ``_make_adapter`` dispatch chain,
``claude_client.FREE_PROVIDERS``, and ``settings._PROVIDER_KEY_FIELDS``. Adding a
provider meant editing all of them; forgetting one failed silently — most
dangerously ``FREE_PROVIDERS`` (the Issue-303 under-reported-spend incident:
a paid provider mis-tagged free, or vice versa, with no error).

This module is the one table. It sits BELOW ``claude_client`` /
``model_provider`` / ``settings`` (it imports nothing from them) so all three
can derive their enumerations from here without an import cycle. Adding a
provider is one ``ProviderSpec`` row. The ``_make_adapter`` construction chain
stays hand-written (each provider's instantiation genuinely differs) but is
pinned to ``SUPPORTED_PROVIDERS`` by an existing guard test
(test_model_provider.test_supported_providers_all_wired_in_make_adapter), and
the derivations here are pinned by test_provider_catalog_single_source.
"""

from __future__ import annotations

from typing import NamedTuple


class ProviderSpec(NamedTuple):
    """One provider's roster-level facts.

    Attributes:
        name: Provider key used across config, cost rows, and dispatch.
        is_free: True if calls incur no per-call cost (subscription / local /
            CLI). Members become part of the budget-exclusion FREE set.
        is_local: True if the provider runs entirely locally (no data leaves
            the machine). Used for privacy consent gating — only these providers
            are allowed when the user has not consented to remote processing for
            resume/craft workloads.
        defaults: Per-workload model defaults ({"quick": ..., "score": ...},
            a value may be None when a workload is unsupported). None means the
            provider has NO production default and is omitted from
            PROVIDER_DEFAULTS — e.g. ``openrouter`` is dispatchable for the
            eval judge but is intentionally NOT part of the scoring cascade.
        key_label: Settings UI label for the BYO API-key field
            (providers.api_keys.<name>), or None for providers with no
            user-entered key (CLI / local / subscription-OAuth transports).
        cli_binary: Executable name this provider shells out to (the
            ``shutil.which()`` target), or None for providers that never spawn
            a CLI subprocess (HTTP/API-key transports). This is the single
            place "which binaries does a registered provider invoke" can be
            answered without a second, hand-maintained copy of the list --
            see issue #1437. ``claude_code_cli.py``, ``gemini_cli.py``, and
            ``detection.py`` read this field instead of repeating the binary
            name as a literal; the test-network-lockdown CLI-binary guard
            (``tests/test_subprocess_lockdown_adoption.py``) derives its
            deny-list from ``cli_binaries()`` below for the same reason.
            ``anthropic``/``anthropic_api`` ALSO set this to ``"claude"``:
            both dispatch through ``claude_client._run_oneshot`` (the same
            ``claude -p`` CLI transport ``claude_code_cli`` uses), so from the
            deny-list's point of view they are additional owners of the same
            binary, not a redundant duplicate. Leaving them unset would make
            ``cli_binaries()`` lose ``"claude"`` coverage the moment
            ``claude_code_cli`` alone were ever removed or renamed, even
            though ``anthropic``/``anthropic_api`` would still be shelling out
            to it -- exactly the kind of go-stale-on-edit gap issue #1437
            exists to close. Set this field for every registered provider
            that shells out to a CLI binary through ANY code path, not only
            providers whose own name matches the binary.
    """

    name: str
    is_free: bool
    is_local: bool = False
    defaults: dict[str, str | None] | None = None
    key_label: str | None = None
    cli_binary: str | None = None


# THE roster. Order matters only for PROVIDER_KEY_FIELDS (rendered in the
# Settings UI in this relative order): anthropic, gemini, groq, cerebras,
# openrouter. SUPPORTED_PROVIDERS / FREE set are unordered; PROVIDER_DEFAULTS
# is keyed.
PROVIDERS: tuple[ProviderSpec, ...] = (
    # subscription OAuth transport ($0) — API-key transport is the separate
    # "anthropic_api" row below; both share the one "Anthropic API key" field.
    ProviderSpec(
        "anthropic",
        is_free=True,
        defaults={
            "quick": "claude-haiku-4-5",
            "score": "claude-sonnet-4-6",
            "craft": "claude-opus-4-6",
        },
        key_label="Anthropic API key",
        # Dispatches through claude_client._run_oneshot -- the same `claude -p`
        # CLI transport claude_code_cli uses. See ProviderSpec.cli_binary's
        # docstring for why this is set even though claude_code_cli already
        # contributes "claude" to the derived set.
        cli_binary="claude",
    ),
    # Issue 303: API-key transport (billed per token). Same model defaults as
    # "anthropic"; NOT free, so cost_gate / budget accounting apply.
    ProviderSpec(
        "anthropic_api",
        is_free=False,
        defaults={
            "quick": "claude-haiku-4-5",
            "score": "claude-sonnet-4-6",
            "craft": "claude-opus-4-6",
        },
        # Same claude_client._run_oneshot transport as "anthropic" above.
        cli_binary="claude",
    ),
    ProviderSpec(
        "gemini",
        is_free=True,
        defaults={
            "quick": "gemini-2.5-flash",
            "score": "gemini-2.5-flash",
            "craft": "gemini-2.5-flash",
        },
        key_label="Gemini API key",
    ),
    ProviderSpec(
        "gemini_cli",
        is_free=True,
        defaults={
            "quick": "gemini-2.5-flash",
            "score": "gemini-2.5-flash",
            "craft": "gemini-2.5-flash",
        },
        cli_binary="gemini",
    ),
    ProviderSpec(
        "ollama",
        is_free=True,
        is_local=True,
        defaults={"quick": "qwen2.5:14b", "score": "qwen2.5:14b", "craft": "qwen2.5:14b"},
        cli_binary="ollama",
    ),
    ProviderSpec(
        "local_bundled",
        is_free=True,
        is_local=True,
        defaults={"quick": "Qwen2.5-3B-Instruct-Q4_K_M", "score": None, "craft": None},
    ),
    ProviderSpec(
        "claude_code_cli",
        is_free=True,
        defaults={
            "quick": "claude-haiku-4-5",
            "score": "claude-sonnet-4-6",
            "craft": "claude-opus-4-6",
        },
        cli_binary="claude",
    ),
    ProviderSpec(
        "groq",
        is_free=False,
        defaults={
            "quick": "llama-3.1-8b-instant",
            "score": "llama-3.3-70b-versatile",
            "craft": "llama-3.3-70b-versatile",
        },
        key_label="Groq API key",
    ),
    ProviderSpec(
        "cerebras",
        is_free=False,
        defaults={"quick": "llama3.1-8b", "score": "llama-3.3-70b", "craft": "llama-3.3-70b"},
        key_label="Cerebras API key",
    ),
    # Dispatchable (eval judge) but NOT in the scoring cascade → defaults=None
    # so it is excluded from PROVIDER_DEFAULTS. Adding a defaults dict here would
    # silently enable it as a cascade fallback.
    ProviderSpec("openrouter", is_free=False, key_label="OpenRouter API key"),
)

# Free cost-attribution labels that are NOT adapter-dispatchable providers, so
# they have no ProviderSpec row but must still be excluded from the budget:
#   - "claude_cli"  — legacy call_claude() internal path (back-compat label).
#   - "google_cse"  — Google Programmable Search source (Stage 3), a search
#                     provider, not an LLM provider.
_EXTRA_FREE_LABELS: frozenset[str] = frozenset({"claude_cli", "google_cse"})


# ── Derived views — every consumer imports one of these instead of re-listing ──

SUPPORTED_PROVIDERS: frozenset[str] = frozenset(p.name for p in PROVIDERS)

PROVIDER_DEFAULTS: dict[str, dict[str, str | None]] = {
    p.name: dict(p.defaults) for p in PROVIDERS if p.defaults is not None
}

# Budget-exclusion set: free adapter providers PLUS the non-adapter free labels.
FREE_PROVIDER_NAMES: frozenset[str] = (
    frozenset(p.name for p in PROVIDERS if p.is_free) | _EXTRA_FREE_LABELS
)

# Settings BYO-key fields, in roster order: (provider_name, ui_label).
PROVIDER_KEY_FIELDS: tuple[tuple[str, str], ...] = tuple(
    (p.name, p.key_label) for p in PROVIDERS if p.key_label is not None
)


def is_local_provider(provider_name: str) -> bool:
    """Return True if the provider runs entirely locally (no data leaves the machine).

    Args:
        provider_name: Provider name to check.

    Returns:
        True if the provider's is_local attribute is True, False otherwise.
    """
    for provider in PROVIDERS:
        if provider.name == provider_name:
            return provider.is_local
    return False


def cli_binary_for(provider_name: str) -> str | None:
    """Return the CLI binary a registered provider shells out to, or None.

    The single read path for "what binary does this provider invoke" --
    ``claude_code_cli.py``, ``gemini_cli.py``, ``detection.py``, and
    ``claude_client.py`` call this (or ``require_cli_binary`` below) instead
    of repeating the binary name as a literal (issue #1437). A provider name
    with no matching row, or a matching row with ``cli_binary=None``
    (HTTP/API-key transports), both return None.

    Args:
        provider_name: Provider key (e.g. "claude_code_cli").

    Returns:
        The `cli_binary` value from the matching ProviderSpec, or None.
    """
    for provider in PROVIDERS:
        if provider.name == provider_name:
            return provider.cli_binary
    return None


def require_cli_binary(provider_name: str) -> str:
    """``cli_binary_for()`` that raises instead of returning None.

    The single enforcement point for "this provider's row must carry a
    cli_binary" -- every CLI-shelling call site (``claude_code_cli.py``,
    ``gemini_cli.py``, ``detection.py``, ``claude_client.py``) calls this
    instead of each keeping its own local ``assert binary is not None`` (which
    also silently compiles out under ``python -O``, turning a misconfigured
    catalog row into a bare ``shutil.which(None)`` TypeError three calls deep
    instead of a clear error here).

    Args:
        provider_name: Provider key expected to carry a cli_binary (e.g.
            "claude_code_cli", "gemini_cli", "ollama", "anthropic").

    Returns:
        The `cli_binary` value from the matching ProviderSpec.

    Raises:
        ValueError: If *provider_name* has no row, or its row's `cli_binary`
            is None -- a misconfigured catalog, not a runtime input error.
    """
    binary = cli_binary_for(provider_name)
    if binary is None:
        raise ValueError(f"{provider_name} ProviderSpec must set cli_binary")
    return binary


def cli_binaries() -> frozenset[str]:
    """Return every CLI binary name any registered provider shells out to.

    Deliberately a FUNCTION, computed fresh from PROVIDERS on every call --
    NOT a module-level constant snapshotted at import time the way
    SUPPORTED_PROVIDERS / PROVIDER_DEFAULTS / FREE_PROVIDER_NAMES are. Those
    three are read-only production enumerations that never need to change
    mid-process. This one is also the deny-list the CLI-binary-spawn guard
    (tests/test_subprocess_lockdown_adoption.py) derives from, and that
    guard's own sabotage test (#1437's acceptance criteria: "remove
    cli_binary from one spec, confirm the guard stops catching that binary")
    monkeypatches `provider_catalog.PROVIDERS` mid-test to prove the
    derivation is real, not a frozen copy wearing a derived-looking
    interface. A cached/frozen constant here would make that monkeypatch
    silently ineffective -- the whole point of the sabotage check.

    Returns:
        Frozenset of every non-None `cli_binary` across PROVIDERS.
    """
    return frozenset(p.cli_binary for p in PROVIDERS if p.cli_binary is not None)

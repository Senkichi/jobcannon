"""PORTED from job_finder/web/model_provider.py @ 9678c44c5d667d8a1d587c2d1f92b9df4056ead9
(private job-cannon). Ledger L-0036.

# PORT-SEAM: split per docs/superpowers design note design-providers-byokey.md
# §1a. Private's model_provider.py mixed a pure contract (ModelResult,
# BaseProvider, adapter-raised exceptions) with a host-only cascade
# dispatcher (call_model, routing, adapter construction, deadline
# machinery, cost/rate accounting) in one module. Hosted, the dispatcher
# needs a tenant id, a DB connection, and platform spend accounting -- it
# cannot live in the engine (engine must never import host). This module
# is the ENGINE half: the pure contract both the host dispatcher
# (jobcannon/host/model_provider.py) and the provider adapters
# (jobcannon/engine/providers/*) import from, with zero host dependencies.
#
# ``ProviderCascadeExhaustedError`` and ``ProviderCascadeTimeoutError`` are
# NOT here -- those are raised by the cascade LOOP itself (host-side), not
# by an adapter. ``ProviderTruncationExhaustedError`` and
# ``ProviderUnavailable`` stay here because private raises both from inside
# adapter ``call()`` bodies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol


class CredentialResolver(Protocol):
    """Bound-per-tenant API-key lookup, replacing private's
    ``get_secret("providers.api_keys.<name>", config=config)`` call in each
    adapter constructor (design §1b).

    Arity is ``(provider) -> str | None`` -- bound to ONE user by closure,
    not ``(user_id, provider) -> str | None``. This makes cross-tenant key
    leakage structurally unrepresentable: no call site can pass the wrong
    ``user_id`` because the resolver never accepts one. The host builds one
    resolver per tenant (``jobcannon.host.credentials.build_credential_resolver``)
    and threads it into the adapter it constructs for that tenant's call.
    """

    def __call__(self, provider: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ModelResult:
    """Result from a provider adapter call.

    ``degenerate`` (issue #227): True when a *scoring* result passed schema
    validation but carries no real signal -- a uniform six-axis vector with
    every rationale array empty. The cascade rejects such results and
    advances to the next provider; only when EVERY provider returns
    degenerate is one accepted with this flag set, so downstream
    classification can route it to low-signal instead of fabricating an
    apply verdict. Defaults False for all non-scoring tiers and for genuine
    results.
    """

    data: dict
    cost_usd: float
    input_tokens: int
    output_tokens: int
    model: str
    provider: str
    schema_valid: bool
    degenerate: bool = False
    # Ollama API ``prompt_eval_duration`` in milliseconds (issue #1560).
    # Hosted has no Ollama branch, so no adapter populates this today; kept
    # for shape-compatibility with private's ModelResult (a field no
    # hosted adapter sets is harmless, and re-adding a local/Ollama-style
    # provider later would not need a ModelResult shape change).
    prompt_eval_duration_ms: float | None = None


class BaseProvider(ABC):
    """Abstract base for provider adapters."""

    @abstractmethod
    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a model call and return structured result."""
        ...


class ProviderTruncationExhaustedError(ValueError):
    """Raised by a provider adapter when it already retried its own internal
    truncation-recovery attempt (bumping max_tokens) and still could not
    obtain parseable output.

    Distinct from a generic malformed-output ``ValueError`` so the cascade's
    same-provider malformed-output retry does NOT re-invoke the adapter a
    second time on top of a retry the adapter already exhausted internally
    -- see the host dispatcher's malformed-output handling for the
    compounding-retry math this prevents (ported from private issue #1787).
    """


class ProviderUnavailable(RuntimeError):
    """Raised when a provider is marked unavailable for reasons the
    dispatcher's normal ``ValueError``/missing-credential path does not
    cover (e.g. a runtime-detected local-prerequisite failure). Caught by
    the cascade's existing ``RuntimeError`` catch tuple -- no catch-tuple
    changes needed.
    """

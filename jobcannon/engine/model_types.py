"""PORTED from job_finder/web/model_provider.py @ 9678c44c5d667d8a1d587c2d1f92b9df4056ead9
(private job-cannon). Ledger L-0036.

# PORT-SEAM: split so the pure contract half can live in the engine layer.
# Private's model_provider.py mixed a pure contract (ModelResult,
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
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol


class CredentialResolver(Protocol):
    """Bound-per-tenant API-key lookup, replacing private's
    ``get_secret("providers.api_keys.<name>", config=config)`` call in each
    adapter constructor.

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


# --- IMAP intake contracts (NOT part of the ported model_provider.py
# lineage above) -----------------------------------------------------------
#
# Ledger L-0115. Design note ("design-aggregators-imap.md") §1.3-1.4:
# MailboxCredentialResolver mirrors CredentialResolver's bound-per-tenant,
# arity-enforced shape exactly (same cross-tenant-leakage argument: the
# resolver's own signature makes passing the wrong user_id unrepresentable)
# and MailboxConnectionFactory is the injection seam that keeps a concrete
# IMAP client library out of jobcannon/engine entirely -- engine must never
# import psycopg, flask, apscheduler, or (new to this port) imapclient;
# jobcannon/host/ingestion/imap_intake.py is the only module that imports
# imapclient, and only inside a function body (see that module's docstring),
# never at module scope.


@dataclass(frozen=True, slots=True)
class MailboxCredential:
    """Decrypted, in-memory-only mailbox credential for one tenant.

    Never persisted in this shape -- jobcannon/db/_mailbox_credentials.py
    stores only the encrypted envelope; this dataclass exists solely to
    cross the host/engine seam (MailboxConnectionFactory's argument) without
    re-decrypting or re-touching the DB inside the connection factory.
    ``address`` (the mailbox's own email address) and ``secret`` (the
    app-password) are the two values combined into ONE ciphertext at rest
    (design note §1.1) -- both are PII and neither is logged or repr'd
    beyond this frozen dataclass's default repr, which callers must not
    pass to a logger.
    """

    address: str
    secret: str
    imap_host: str
    imap_port: int
    folder: str


class MailboxCredentialResolver(Protocol):
    """Bound-per-tenant mailbox credential lookup.

    Arity is ``() -> MailboxCredential | None`` -- zero arguments, bound to
    ONE user by closure (mirrors CredentialResolver above, one tighter: a
    tenant has at most one mailbox credential, so there is no provider-name
    parameter to thread through at all). Returns None whenever the tenant
    has no usable mailbox: no active credential row, an inactive row, a
    decrypt failure, OR (fail-closed, checked first) consent not granted --
    jobcannon.host.credentials.build_mailbox_resolver is the sole producer
    and folds all four cases into the same None, by design (a caller
    dispatching only on "do I have a credential" has no reason to
    distinguish "declined consent" from "not connected yet").
    """

    def __call__(self) -> MailboxCredential | None: ...


class MailboxConnectionFactory(Protocol):
    """``(MailboxCredential) -> context-managed IMAP client``.

    The injection seam that keeps host/ingestion/imap_intake.py's fetch
    logic testable without a real mailbox: tests inject a fake factory
    returning a fake client (no `imapclient` import needed in the test
    process at all). The host's default binding
    (``host/ingestion/imap_intake.py::_default_connection_factory``) opens
    an ``IMAPClient`` connection in readonly mode and closes it on context
    exit; the return type is intentionally the broad
    ``AbstractContextManager[Any]`` rather than a protocol naming
    IMAPClient-specific methods, so a fake test double only needs to
    implement the handful of methods imap_intake.py actually calls.
    """

    def __call__(self, credential: MailboxCredential) -> AbstractContextManager[Any]: ...

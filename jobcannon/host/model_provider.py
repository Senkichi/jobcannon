"""ADAPTED from job_finder/web/model_provider.py @ 9678c44c5d667d8a1d587c2d1f92b9df4056ead9
(private job-cannon). Ledger L-0036.

# PORT-SEAM: this is an ADAPT extraction, not a verbatim port -- design note
# design-providers-byokey.md §6 explicitly exempts this file (and
# jobcannon/engine/model_types.py) from fidelity-diff verbatim comparison:
# "they are ADAPT extractions (split across two files + owner-budget->host-DB
# seam). Verify by behavior/tests, and by a structural diff showing the
# deadline machinery carried unchanged." See the module-level PORT-SEAM notes
# below for what carries byte-identical vs. what is deliberately rewritten,
# and docs/superpowers/plans/2026-09-02-migration-completeness-audit.md's PR
# body for the full Design conformance mapping.
#
# What carries BYTE-IDENTICAL (design §1d): the single-monotonic-deadline
# machinery (_deadline / _remaining_or_raise / _TIMEOUT_EPSILON / the
# _UnsetTimeout sentinel / _TIER_DEFAULT_TIMEOUTS), schema validation/retry
# (_validate_schema / _sanitize_output / _coerce_enum), and degenerate-vector
# detection (is_degenerate_assessment, issue #227) -- these are pure
# functions/constants with zero owner-config or host coupling, ported as-is.
#
# What is REWRITTEN, and why:
#   - sqlite3.Connection -> Any (psycopg-pooled connection; this host is
#     Postgres-only, matching jobcannon/db/_direct_link.py's convention).
#   - resolve_workload_routing / resolve_provider_config (owner config.yaml
#     parsing: providers.primary/overrides/consented_providers, craft-tier
#     pin to claude_code_cli) do NOT carry: hosted has no config.yaml at all
#     (jobcannon/host/config.py's HostConfig docstring), and claude_code_cli
#     is not hosted-eligible (design §5, Gate-2 DIE/HOLD), so the pin has no
#     hosted meaning. Replaced by resolve_hosted_routing(), which implements
#     design §1c's stated mechanism directly: intersect the tenant's active
#     byo_key_credentials providers with HOSTED_ELIGIBLE_PROVIDERS and feed
#     that as the ordered chain -- there is no owner-config branch that would
#     ever fire hosted, so parameterizing the private function with a
#     synthetic config dict would be a larger, less honest diff for no
#     behavioral gain.
#   - _make_adapter carries the gemini/groq/cerebras construction branches
#     only (ollama/anthropic/claude_code_cli/gemini_cli/openrouter/
#     local_bundled are absent -- Gate-2 DIE/HOLD) and drops the
#     (provider_name, base_url, keep_alive, num_ctx) adapter-memoization
#     cache ENTIRELY (explicit instruction; design §4 modularity note #1,
#     HIGH: the cache keys three of its four dimensions None for every
#     non-Ollama provider, so it would hand tenant B's adapter -- holding
#     tenant A's decrypted API key in a closure/attribute -- to tenant B's
#     call). A fresh adapter is constructed every call, bound to a
#     freshly-built per-call CredentialResolver.
#   - cost_gate / BudgetExceededError / FREE_PROVIDERS (private:
#     claude_client.py) do not carry: those gate an OWNER's daily spend
#     budget, which has no meaning under BYO-key (every REST call bills the
#     TENANT's own key -- design §5's semantics-inversion note). Dropped
#     entirely rather than ported-and-unused.
#   - _daily_usage / _check_daily_limit / _increment_usage /
#     _init_usage_from_db / _ensure_usage_current (private's in-process
#     daily-request-cap tracker) do not carry: private bootstraps from a
#     `scoring_costs` table this host does not have (grepped: no migration
#     creates it), so a verbatim port would crash on first call. daily_limits
#     is always {} hosted (no owner config.yaml source for it), which made
#     private's own _check_daily_limit a no-op in that case anyway. Rate-limit
#     ownership is an open question this design defers (§5, §4 modularity
#     note #4: per-(user_id, provider) limits from a real per-tenant ledger,
#     filed as a follow-up, not invented here).
#   - _maybe_record_cost is replaced by the module-level record_cost() below:
#     same free/paid cost_usd-zeroing semantics, but a structured-log sink
#     instead of an INSERT into `scoring_costs` (which does not exist on this
#     host -- see the modularity note on record_cost's own docstring).
#   - privacy_sensitive / consented_providers filtering does not carry:
#     that gates the OWNER's single-user config.yaml consent list; a hosted,
#     per-tenant consent model is a separate, undesigned feature.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any

from jsonschema import ValidationError, validate

from jobcannon.engine.constants import SUB_SCORE_KEYS
from jobcannon.engine.model_types import (
    BaseProvider,
    CredentialResolver,
    ModelResult,
    ProviderTruncationExhaustedError,
)
from jobcannon.host import credentials as _credentials
from jobcannon.host.provider_catalog import PROVIDER_DEFAULTS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Workload / hosted-eligibility constants
# ---------------------------------------------------------------------------

# PORT-SEAM: byte-identical to private's _VALID_WORKLOADS.
_VALID_WORKLOADS = frozenset({"quick", "score", "triage", "craft"})

# design §5: "Recommend {gemini, groq, cerebras} only -- all three are pure
# REST, no CLI/local-binary dep." Order is the default preference when a
# tenant has multiple active credentials: gemini first (is_free=True on the
# tenant's own Google quota), then groq/cerebras (billed).
HOSTED_ELIGIBLE_PROVIDERS: tuple[str, ...] = ("gemini", "groq", "cerebras")


class ProviderCascadeExhaustedError(RuntimeError):
    """Raised when every provider in the resolved chain has been tried and
    skipped/failed, OR when the tenant has no hosted-eligible active
    byo_key_credentials row supporting the requested tier at all.
    """


# PORT-SEAM: byte-identical to private's _TIMEOUT_EPSILON.
_TIMEOUT_EPSILON = 0.05

# PORT-SEAM: byte-identical to private's _TIER_DEFAULT_TIMEOUTS (issue #1435)
# -- see that constant's private docstring for the anchoring rationale per
# tier; unchanged hosted, since the cascade-wide-deadline contract itself
# (docs/design/provider-cascade-constraints.md, binding) does not vary by
# deployment.
_TIER_DEFAULT_TIMEOUTS: dict[str, float] = {
    "quick": 90.0,
    "score": 120.0,
    "craft": 180.0,
    "triage": 90.0,
}
_TIER_DEFAULT_TIMEOUT_FALLBACK: float = 120.0


class _UnsetTimeout:
    """Sentinel: ``timeout`` not provided by the caller. PORT-SEAM:
    byte-identical to private -- see call_model's ``timeout`` docstring."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<timeout unset — per-tier default will apply>"


_TIMEOUT_UNSET: float | None = _UnsetTimeout()  # type: ignore[assignment]


class ProviderCascadeTimeoutError(Exception):
    """Raised when call_model's caller-supplied ``timeout`` budget is
    exceeded. PORT-SEAM: byte-identical to private -- an ordinary
    ``Exception`` subclass (not ``BaseException``) so every existing
    ``except Exception`` degrade path can still catch a blown cascade
    deadline; see private's docstring (issue #1436) for the full rationale.
    """


# ---------------------------------------------------------------------------
# Schema validation / sanitization / degenerate-vector detection
# PORT-SEAM: byte-identical to private (design §1d).
# ---------------------------------------------------------------------------


def _validate_schema(data: dict, schema: dict | None) -> list[str]:
    if schema is None:
        return []
    try:
        validate(instance=data, schema=schema)
        return []
    except ValidationError as exc:
        return [exc.message]


def _coerce_enum(value: str, enum_values: list[str]) -> str:
    lower = value.lower().strip()
    for ev in enum_values:
        if lower == ev.lower():
            return ev
    for ev in sorted(enum_values, key=len, reverse=True):
        if lower.startswith(ev.lower()):
            return ev
    for ev in sorted(enum_values, key=len, reverse=True):
        if ev.lower() in lower:
            return ev
    return value


def _sanitize_output(data: dict, schema: dict | None) -> dict:
    if schema is None or not isinstance(data, dict):
        return data

    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    additional = schema.get("additionalProperties", True)

    result = {}
    for key, value in data.items():
        if not additional and key not in props:
            continue
        spec = props.get(key, {})
        if spec.get("type") == "integer" and isinstance(value, str):
            try:
                value = int(float(value))
            except (ValueError, TypeError):
                pass
        if "enum" in spec and isinstance(value, str) and value not in spec["enum"]:
            value = _coerce_enum(value, spec["enum"])
        if spec.get("type") == "object" and isinstance(value, dict):
            value = _sanitize_output(value, spec)
        result[key] = value

    for key in required:
        if key not in result:
            spec = props.get(key, {})
            if spec.get("type") == "array":
                result[key] = []
            elif spec.get("type") == "object":
                result[key] = {}

    return result


def _sanitized_result(result: ModelResult, schema: dict | None, provider_name: str) -> ModelResult:
    if not isinstance(result.data, dict):
        return result
    sanitized = _sanitize_output(result.data, schema)
    if sanitized is result.data:
        return result
    return replace(result, data=sanitized)


def _is_malformed_output_error(exc: BaseException) -> bool:
    if isinstance(exc, ProviderTruncationExhaustedError):
        return False
    import json

    return isinstance(exc, json.JSONDecodeError) or isinstance(exc.__cause__, json.JSONDecodeError)


# Issue #227 quality floor. PORT-SEAM: byte-identical to private, except
# SUB_SCORE_KEYS is imported from jobcannon.engine.constants (this host's
# canonical location) rather than job_finder.constants.
_SCORING_AXIS_KEYS: frozenset[str] = frozenset(SUB_SCORE_KEYS)
_RATIONALE_ARRAY_KEYS: tuple[str, ...] = (
    "strengths",
    "gaps",
    "talking_points",
    "resume_priority_skills",
)


def _axis_score(raw: object) -> int | None:
    if isinstance(raw, dict) and "score" in raw:
        raw = raw["score"]
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def is_degenerate_assessment(data: dict | None) -> bool:
    """PORT-SEAM: byte-identical to private (issue #227) -- see private's
    docstring for the full no-signal-vector rationale."""
    if not isinstance(data, dict):
        return False
    if not _SCORING_AXIS_KEYS.issubset(data.keys()):
        return False

    axis_values = [_axis_score(data.get(k)) for k in _SCORING_AXIS_KEYS]
    if any(v is None for v in axis_values):
        return False
    if len(set(axis_values)) != 1:
        return False

    rationale = data.get("rationale")
    if not isinstance(rationale, dict):
        return True
    return all(not rationale.get(k) for k in _RATIONALE_ARRAY_KEYS)


def _augment_with_errors(messages: list[dict], errors: list[str]) -> list[dict]:
    error_text = "\n\nSchema validation errors from previous attempt:\n" + "\n".join(
        f"- {e}" for e in errors
    )
    return messages[:-1] + [{**messages[-1], "content": messages[-1]["content"] + error_text}]


# ---------------------------------------------------------------------------
# Hosted routing -- design §1c (replaces private's resolve_workload_routing /
# resolve_provider_config; see module docstring for why).
# ---------------------------------------------------------------------------


def resolve_hosted_routing(tier: str, available_providers: list[str]) -> dict:
    """Build the ordered {provider, model} cascade chain for one tenant.

    Args:
        tier: Workload name, validated against _VALID_WORKLOADS.
        available_providers: The tenant's active byo_key_credentials
            providers, already intersected with HOSTED_ELIGIBLE_PROVIDERS
            by the caller (call_model).

    Returns:
        {"provider": str, "model": str, "prompt_variant": None,
         "fallback_chain": [{"provider": str, "model": str}, ...]}

    Raises:
        ValueError: Unknown tier.
        ProviderCascadeExhaustedError: No candidate in available_providers
            has a PROVIDER_DEFAULTS entry for this tier (either the tenant
            configured no hosted-eligible key, or none of their configured
            providers support this workload).
    """
    if tier not in _VALID_WORKLOADS:
        raise ValueError(f"Unknown workload tier: {tier!r}")

    available = set(available_providers)
    candidates = [
        p
        for p in HOSTED_ELIGIBLE_PROVIDERS
        if p in available and PROVIDER_DEFAULTS.get(p, {}).get(tier)
    ]
    if not candidates:
        raise ProviderCascadeExhaustedError(
            f"No hosted-eligible provider available for tier={tier!r} -- tenant "
            f"has no active byo_key_credentials row for gemini/groq/cerebras "
            f"supporting this workload"
        )

    return {
        "provider": candidates[0],
        "model": PROVIDER_DEFAULTS[candidates[0]][tier],
        "prompt_variant": None,
        "fallback_chain": [
            {"provider": p, "model": PROVIDER_DEFAULTS[p][tier]} for p in candidates[1:]
        ],
    }


# ---------------------------------------------------------------------------
# Adapter construction -- gemini/groq/cerebras only, NO memoization cache
# (design §4 modularity note #1, HIGH -- see module docstring).
# ---------------------------------------------------------------------------


def _make_adapter(
    provider_name: str,
    config: dict,
    resolve_credential: CredentialResolver,
) -> BaseProvider:
    """Instantiate a fresh adapter bound to this call's CredentialResolver.

    Never cached: caching by provider name alone (private's behavior) would
    hand one tenant's adapter -- and the API key closed over inside it -- to
    the next tenant's call. A fresh instance every call is the fix (design
    §4 modularity note #1).
    """
    if provider_name not in HOSTED_ELIGIBLE_PROVIDERS:
        raise ValueError(f"Not a hosted-eligible provider: {provider_name!r}")

    if provider_name == "gemini":
        from jobcannon.engine.providers.gemini_provider import GeminiProvider

        return GeminiProvider(config=config, resolve_credential=resolve_credential)
    if provider_name == "groq":
        from jobcannon.engine.providers.groq_provider import GroqProvider

        return GroqProvider(config=config, resolve_credential=resolve_credential)
    if provider_name == "cerebras":
        from jobcannon.engine.providers.cerebras_provider import CerebrasProvider

        return CerebrasProvider(config=config, resolve_credential=resolve_credential)
    raise ValueError(f"No adapter dispatch branch for provider: {provider_name!r}")


# ---------------------------------------------------------------------------
# Cost/usage recording -- exposed as its own ScanServices field (per
# design-nightly-flywheel.md §4 item 2) so callers OTHER than call_model's
# own cascade loop (e.g. a future SerpAPI-enrichment quota counter) share
# one seam instead of hand-rolling their own cost-ledger writes.
# ---------------------------------------------------------------------------


def record_cost(
    *,
    provider: str,
    model: str,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    job_id: str | None = None,
    purpose: str = "",
    user_id: str | None = None,
    schema_valid: bool | None = None,
) -> None:
    """Record one cost/usage event.

    ADAPTED from private's _maybe_record_cost: this host has no
    `scoring_costs` table (grepped: no migration creates it; the two
    references in jobcannon/engine/data_enricher.py -- _serpapi_daily_calls_used
    / _record_serpapi_call -- query one that does not exist on this host and
    are themselves pre-existing, out-of-scope dead code, unrelated to this
    port). Rather than invent a table this design's own open questions (§5)
    have not resolved the shape of (does it need a user_id column? per-tenant
    or per-provider granularity?), this is a structured-log sink only.

    Modularity follow-up (design §4 item 4, HIGH-ish): factor a real
    per-tenant spend/rate-accounting module once that shape is decided, and
    wire it in here without changing this function's signature or any
    caller.

    Raises:
        ValueError: provider is empty (U6 guard, ported from private --
            loud failure beats a cost event silently attributed nowhere).
    """
    if not provider:
        raise ValueError(
            f"record_cost: provider must be non-empty (job_id={job_id}, purpose={purpose}, model={model})"
        )
    logger.info(
        "record_cost: provider=%s model=%s cost_usd=%.6f input_tokens=%d "
        "output_tokens=%d job_id=%s purpose=%s user_id=%s schema_valid=%s",
        provider,
        model,
        cost_usd,
        input_tokens,
        output_tokens,
        job_id,
        purpose,
        user_id,
        schema_valid,
    )


# ---------------------------------------------------------------------------
# call_model() dispatcher
# ---------------------------------------------------------------------------


def call_model(
    tier: str,
    system: str,
    messages: list[dict],
    conn: Any,
    config: dict,
    output_schema: dict | None = None,
    job_id: str | None = None,
    purpose: str = "",
    max_tokens: int = 1024,
    timeout: float | None | _UnsetTimeout = _TIMEOUT_UNSET,
    user_id: str | None = None,
) -> ModelResult:
    """Dispatch a model call to a hosted-eligible provider for one tenant.

    Signature is call-site-compatible with the ALREADY-LANDED
    ``jobcannon.engine.job_scorer.score_job(..., call_model=...)`` seam,
    which invokes ``call_model(tier=, system=, messages=, conn=, config=,
    output_schema=, job_id=, purpose=, max_tokens=, timeout=)`` with no
    ``user_id`` -- so ``user_id`` is added here as an OPTIONAL trailing
    parameter (default None), not a new required positional/keyword, and
    that existing call site is unaffected. No caller passes ``user_id``
    today (design's own load-bearing fact: "not wired to any scoring call
    site" -- hosted scoring has no live caller yet); when ``user_id`` is
    None, no tenant is identified, so the tenant's available-provider set is
    empty and the call fails closed via ProviderCascadeExhaustedError below.

    Args:
        tier: Workload class: "quick", "score", "triage", or "craft".
        system: System prompt string.
        messages: List of message dicts [{role, content}].
        conn: Pooled Postgres connection (``.raw`` unwrapped internally,
            matching jobcannon/db/_direct_link.py's dispatch).
        config: Reserved for adapter-level config (e.g. base URLs); no
            owner-config routing fields are read from it hosted (see module
            docstring).
        output_schema: JSON schema dict for structured output (or None).
        job_id: Job dedup_key for cost attribution (nullable).
        purpose: Feature attribution label for cost rows.
        max_tokens: Maximum output tokens. Defaults to 1024.
        timeout: Cascade-wide wall-clock budget in seconds, tracked as a
            single monotonic deadline (docs/design/provider-cascade-
            constraints.md, binding). Omitted -> per-tier default
            (_TIER_DEFAULT_TIMEOUTS). Explicit None -> unbounded opt-in.
        user_id: The tenant whose byo_key_credentials govern this call.

    Returns:
        ModelResult from the successful adapter call.

    Raises:
        ProviderCascadeExhaustedError: No hosted-eligible provider is
            available for this tenant/tier, or every attempted provider
            failed.
        ProviderCascadeTimeoutError: The cascade's monotonic deadline was
            exceeded before an attempt, sleep, or retry that would
            otherwise proceed.
    """
    available_providers: list[str] = []
    if user_id:
        from jobcannon.db._byo_key_credentials import get_active_providers

        available_providers = [
            p for p in get_active_providers(conn, user_id) if p in HOSTED_ELIGIBLE_PROVIDERS
        ]

    routing = resolve_hosted_routing(tier, available_providers)
    provider_name: str = routing["provider"]
    model: str = routing["model"]
    fallback_chain: list[dict] = routing["fallback_chain"]

    if isinstance(timeout, _UnsetTimeout):
        timeout = _TIER_DEFAULT_TIMEOUTS.get(tier, _TIER_DEFAULT_TIMEOUT_FALLBACK)

    # PORT-SEAM: byte-identical deadline machinery to private (design §1d /
    # docs/design/provider-cascade-constraints.md, binding constraint 1).
    _deadline: float | None = time.monotonic() + timeout if timeout is not None else None

    def _remaining_or_raise(entry_provider: str) -> float | None:
        if _deadline is None:
            return None
        remaining = _deadline - time.monotonic()
        if remaining <= _TIMEOUT_EPSILON:
            raise ProviderCascadeTimeoutError(
                f"call_model: timeout={timeout}s budget exhausted before "
                f"{entry_provider} attempt (tier={tier!r}, purpose={purpose!r})"
            )
        return remaining

    # resolve_hosted_routing above already raised ProviderCascadeExhaustedError
    # if available_providers was empty, and available_providers is only ever
    # populated when user_id is truthy -- so user_id is guaranteed truthy here.
    resolve_credential = _credentials.build_credential_resolver(conn, user_id)

    chain: list[dict] = [
        {"provider": provider_name, "model": model}
    ] + list(fallback_chain)

    logger.info(
        "call_model CASCADE: tier=%s chain=[%s] purpose=%s job_id=%s user_id=%s",
        tier,
        ", ".join(f"{e['provider']}:{e['model']}" for e in chain),
        purpose,
        job_id,
        user_id,
    )

    degenerate_fallback: ModelResult | None = None
    _degenerate_rejections: dict[str, int] = {}

    for entry in chain:
        entry_provider = entry["provider"]
        entry_model = entry["model"]

        if resolve_credential is None:
            break  # no tenant identified -- nothing to try

        try:
            adapter = _make_adapter(entry_provider, config, resolve_credential)
        except (ValueError, RuntimeError, ImportError) as exc:
            logger.warning("Cascade: %s unavailable: %s", entry_provider, exc)
            continue

        max_retries = 2
        attempt = 0
        malformed_attempt = 0
        while attempt <= max_retries:
            _call_timeout = _remaining_or_raise(entry_provider)
            try:
                result = adapter.call(
                    entry_model,
                    system,
                    messages,
                    output_schema,
                    max_tokens,
                    _call_timeout,
                )
                result = _sanitized_result(result, output_schema, entry_provider)
                errors = _validate_schema(result.data, output_schema)
                if errors:
                    augmented = _augment_with_errors(messages, errors)
                    _call_timeout = _remaining_or_raise(entry_provider)
                    result = adapter.call(
                        entry_model,
                        system,
                        augmented,
                        output_schema,
                        max_tokens,
                        _call_timeout,
                    )
                    result = _sanitized_result(result, output_schema, entry_provider)
                    errors = _validate_schema(result.data, output_schema)
                if not errors:
                    if is_degenerate_assessment(result.data):
                        _degenerate_rejections[entry_provider] = (
                            _degenerate_rejections.get(entry_provider, 0) + 1
                        )
                        logger.warning(
                            "Cascade: %s returned degenerate (no-signal) assessment "
                            "for tier=%s purpose=%s job_id=%s -- rejecting, advancing cascade",
                            entry_provider,
                            tier,
                            purpose,
                            job_id,
                        )
                        degenerate_fallback = replace(result, degenerate=True)
                        break
                    try:
                        record_cost(
                            provider=result.provider,
                            model=result.model,
                            cost_usd=result.cost_usd,
                            input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens,
                            job_id=job_id,
                            purpose=purpose,
                            user_id=user_id,
                            schema_valid=result.schema_valid,
                        )
                    except Exception as cost_exc:
                        logger.warning(
                            "Cascade: %s cost recording failed (non-fatal): %s",
                            entry_provider,
                            cost_exc,
                        )
                    logger.info(
                        "call_model ROUTED: tier=%s provider=%s model=%s purpose=%s job_id=%s",
                        tier,
                        result.provider,
                        result.model,
                        purpose,
                        job_id,
                    )
                    return result
                logger.warning("Cascade: %s schema invalid after retry, skipping", entry_provider)
                break
            except ProviderCascadeTimeoutError:
                raise
            except Exception as exc:
                if _is_malformed_output_error(exc) and malformed_attempt < 1:
                    logger.warning(
                        "Cascade: %s malformed output, retrying same provider once: %s",
                        entry_provider,
                        exc,
                    )
                    malformed_attempt += 1
                    continue
                if isinstance(exc, ProviderTruncationExhaustedError):
                    logger.warning(
                        "Cascade: %s malformed output after adapter's own internal "
                        "truncation retry, skipping: %s",
                        entry_provider,
                        exc,
                    )
                else:
                    logger.warning("Cascade: %s error: %s", entry_provider, exc)
                break

    if degenerate_fallback is not None:
        logger.warning(
            "call_model: ALL providers returned degenerate scoring output "
            "(tier=%s purpose=%s job_id=%s, rejections=%s); accepting "
            "provider=%s flagged degenerate=True",
            tier,
            purpose,
            job_id,
            _degenerate_rejections,
            degenerate_fallback.provider,
        )
        try:
            record_cost(
                provider=degenerate_fallback.provider,
                model=degenerate_fallback.model,
                cost_usd=degenerate_fallback.cost_usd,
                input_tokens=degenerate_fallback.input_tokens,
                output_tokens=degenerate_fallback.output_tokens,
                job_id=job_id,
                purpose=purpose,
                user_id=user_id,
                schema_valid=degenerate_fallback.schema_valid,
            )
        except Exception as cost_exc:
            logger.warning(
                "Cascade: %s cost recording failed (non-fatal): %s",
                degenerate_fallback.provider,
                cost_exc,
            )
        return degenerate_fallback

    raise ProviderCascadeExhaustedError(
        f"All providers in cascade exhausted or unavailable for tier: {tier!r}. "
        f"Providers tried: {[e['provider'] for e in chain]}"
    )

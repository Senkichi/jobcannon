"""PORTED from job_finder/web/providers/gemini_provider.py @ 9678c44c5d667d8a1d587c2d1f92b9df4056ead9
(private job-cannon). Ledger L-0239.
# PORT-SEAM: __init__ resolves its API key via a per-tenant CredentialResolver
# instead of the api_key_env / get_secret precedence stack -- see the
# constructor below for the full seam, including the dropped `import os`.

Gemini provider adapter -- google-genai SDK (v1+).

Ports the provider from the legacy google-generativeai SDK
(google.generativeai) to the shipped google-genai>=1.0.0 SDK
(google.genai).  The old SDK is not installed in this project; its
import google.generativeai raised ModuleNotFoundError at construction
time, silently killing the provider in the cascade.

API used:
- genai.Client(api_key=...)  -- pure-local construction, no network call.
- client.models.generate_content(model, contents, config)
- types.GenerateContentConfig(system_instruction, max_output_tokens,
  response_mime_type, response_json_schema)
- response.usage_metadata.prompt_token_count /
  response.usage_metadata.candidates_token_count
- genai_errors.APIError -- base class for all SDK-level API errors.

Issue 292 (2026-06-10): compute real ``cost_usd`` from usage tokens.
``"gemini"`` is in ``FREE_PROVIDERS``, so ``_maybe_record_cost`` in the cascade
forces the recorded value to 0.0 regardless of what the adapter returns.
We still compute a notional cost here so the ``ModelResult`` is truthful (and
if ``"gemini"`` is ever reclassified as a paid provider, the meter is already
wired).  Standard tier pricing (text / non-audio) is used for simplicity.
"""

from __future__ import annotations

import json
import logging

# PORT-SEAM: `import os` dropped here -- it backed the os.environ.get(api_key_env)
# fallback, which is not carried hosted (see __init__'s PORT-SEAM note below).
import time
from typing import Any

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

from jobcannon.engine.model_types import (
    BaseProvider,
    CredentialResolver,  # PORT-SEAM: per-tenant credential resolution, see __init__
    ModelResult,
    ProviderTruncationExhaustedError,
)

logger = logging.getLogger(__name__)


def _is_permanent_zero_quota(exc: Exception) -> bool:
    """Check if a Gemini exception indicates permanent-zero quota (free tier exhausted).

    Google's free-tier quota exhaustion signal is a RESOURCE_EXHAUSTED 429 with
    a QuotaFailure detail containing a violation with quotaValue == "0". This is
    distinct from transient rate-limit 429s (which carry RetryInfo or lack zero quota).

    Args:
        exc: The exception from a Gemini API call.

    Returns:
        True if the exception indicates permanent-zero quota (should not retry),
        False otherwise (transient or unrecognized error).
    """
    # Only check structured genai_errors; fall back to False for other types
    if not isinstance(exc, genai_errors.APIError):
        return False

    # Must be HTTP 429 with RESOURCE_EXHAUSTED status
    if exc.code != 429 or exc.status != "RESOURCE_EXHAUSTED":
        return False

    # Inspect structured details for QuotaFailure with quotaValue == "0"
    details = exc.details
    if not isinstance(details, dict):
        return False

    # details may be nested under 'error' key
    error_body = details.get("error", details)

    # Look for QuotaFailure in the details list
    error_details = error_body.get("details", [])
    if not isinstance(error_details, list):
        return False

    for detail in error_details:
        if not isinstance(detail, dict):
            continue
        # Check if this is a QuotaFailure detail
        detail_type = detail.get("@type", "")
        if "QuotaFailure" not in detail_type:
            continue
        # Check for quotaValue == "0" in violations
        violations = detail.get("violations", [])
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            quota_value = violation.get("quotaValue")
            if quota_value == "0":  # String zero, as per Google's API
                return True

    # No zero quota found — treat as transient
    return False


# ---------------------------------------------------------------------------
# Pricing table — price per million tokens (USD), standard tier, text input
# Source: https://ai.google.dev/pricing  last_verified: 2026-07-02
#
# NOTE: Gemini is in FREE_PROVIDERS — _maybe_record_cost forces cost_usd=0.0
# when recording to scoring_costs. The notional cost is computed here so the
# ModelResult is truthful; it will matter if "gemini" is ever reclassified.
# ---------------------------------------------------------------------------
_GEMINI_PRICING: dict[str, dict[str, float]] = {
    # Default for both quick and score workloads (gemini-2.5-pro free tier is dead)
    # Standard tier, text/image/video input
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    # Legacy entry — no longer a default, kept for config override compatibility
    # Standard tier, prompts ≤200k tokens (conservative: use ≤200k pricing)
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
}


# Truncation-aware retry (T2.8/D28). A share of ai_nav/crawler Gemini calls
# came back with a JSON parse failure whose signature matched
# max_output_tokens being too low for the payload rather than a genuine
# malformed-output error -- Gemini fills the budget mid-object and stops, so
# the response is a syntactically incomplete JSON document. Those were
# previously indistinguishable from a real malformed response and counted as
# a hard failure (surfacing upstream as zero_jobs in ai_nav). Detected two
# ways -- either is sufficient to trigger the retry:
#   1. response.candidates[0].finish_reason == "MAX_TOKENS" (the SDK's own
#      truncation signal, present even when the truncated text happens to
#      still parse as valid JSON up to that point -- rare but possible for
#      a schema with optional trailing fields).
#   2. A JSONDecodeError on the returned text (catches SDKs/mocks that don't
#      populate finish_reason, and catches any other truncation shape).
# One retry only, at TRUNCATION_RETRY_TOKEN_MULTIPLIER times the caller's
# original max_tokens -- a single named constant so the budget bump isn't
# hardcoded independently at each call site that might need it.
#
# _TRUNCATION_RETRY_TOKEN_CEILING bounds the escalated budget independent of
# the caller's max_tokens. This retry is internal to a single GeminiProvider
# .call() -- but call_model()'s own schema-validation retry (model_provider.py)
# can invoke adapter.call() a second time on top of this one when the parsed
# result is schema-invalid rather than truncated, so an unbounded multiplier
# would let one logical request escalate to 4x, then again to 16x, a caller's
# original budget. The ceiling caps worst-case single-response output
# regardless of how many layers stack, without touching the cascade's retry
# logic itself.
_TRUNCATION_FINISH_REASON = "MAX_TOKENS"
TRUNCATION_RETRY_TOKEN_MULTIPLIER = 4
_TRUNCATION_RETRY_TOKEN_CEILING = 8192


def _finish_reason(response: Any) -> str | None:
    """Return the first candidate's finish_reason as a plain string, or None.

    Defensive against mocked/partial response objects in tests and against
    an empty ``candidates`` list (observed for safety-blocked responses),
    since this is a detection helper, not a hard SDK-shape assumption.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    finish_reason = getattr(candidates[0], "finish_reason", None)
    if finish_reason is None:
        return None
    # google-genai's FinishReason is a str-valued enum; str() also degrades
    # cleanly for a plain string stand-in used by tests/mocks.
    value = getattr(finish_reason, "value", finish_reason)
    return str(value)


def _response_was_truncated(response: Any) -> bool:
    """True when the SDK itself reports the response was cut off by the
    max_output_tokens budget (as opposed to a parse-time JSON error, which
    is checked separately by the caller)."""
    return _finish_reason(response) == _TRUNCATION_FINISH_REASON


def _gemini_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return notional cost in USD for a Gemini API call.

    Falls back to the most expensive known entry for unrecognised model IDs
    (conservative — gate trips early rather than never, should "gemini" ever
    be removed from FREE_PROVIDERS).
    """
    pricing = _GEMINI_PRICING.get(model)
    if pricing is None:
        logger.warning(
            "Unknown Gemini model '%s' in _gemini_cost — using highest known pricing as fallback",
            model,
        )
        pricing = max(_GEMINI_PRICING.values(), key=lambda p: p["input"] + p["output"])
    return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing[
        "output"
    ]


class GeminiProvider(BaseProvider):
    """Provider adapter for Google Gemini via the google-genai SDK (v1+).

    Uses response_json_schema inside GenerateContentConfig for structured
    output.  Automatically retries once on transient errors (HTTP 429 /
    rate-limit / timeout) with a configurable sleep duration (default 15 s
    for the free-tier 5 RPM limit).

    Args:
        config: Application config dict. Reads providers.gemini.*.
        client: Optional pre-built genai.Client for testing.
                When provided, skips API key resolution entirely.

    Raises:
        ImportError: When google-genai is not installed.
        ValueError: When no Gemini API key can be resolved.
    """

    # PORT-SEAM: new required-keyword resolve_credential param (per-tenant
    # credential resolution) -- see the body below.
    def __init__(
        self, config: dict, *, client: Any | None = None, resolve_credential: CredentialResolver
    ) -> None:
        if not _GENAI_AVAILABLE:
            raise ImportError(
                "google-genai is required for GeminiProvider. "
                "Install with: pip install google-genai>=1.0.0"
            )
        provider_cfg = config.get("providers", {}).get("gemini", {})
        self._retry_sleep: float = provider_cfg.get("retry_sleep_seconds", 15.0)

        if client is not None:
            self._client: Any = client
        else:
            # PORT-SEAM: per-tenant credential lookup replacing the api_key_env / get_secret
            # precedence stack. The deleted `os.environ.get(api_key_env)` fallback is NOT
            # carried: hosted, an operator-level env var would resolve one key for every
            # tenant, defeating BYO-key isolation.
            api_key = resolve_credential("gemini")
            if not api_key:
                # PORT-SEAM: message rewritten for hosted -- no env var / keyring /
                # config.yaml precedence to describe; direct the tenant to Settings.
                raise ValueError("Gemini API key not set. Add it in your account settings.")
            # Client construction is pure-local; no network call at this point.
            self._client = genai.Client(api_key=api_key)

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a Gemini model call and return a ModelResult.

        Args:
            model: Gemini model identifier, e.g. "gemini-2.5-flash".
            system: System prompt (passed as system_instruction).
            messages: List of message dicts [{role, content}].
            output_schema: JSON schema dict for structured output. When
                provided, sets response_mime_type="application/json" and
                passes the schema via response_json_schema.  When None,
                raw text is returned wrapped as {"text": ...}.
            max_tokens: Maximum output tokens. Defaults to 1024.
            timeout: Per-call deadline in seconds (#1436). Threaded into the
                SDK via ``GenerateContentConfig(http_options=HttpOptions(
                timeout=...))`` -- verified against the installed
                ``google-genai`` package that ``HttpOptions.timeout`` (in
                milliseconds) is accepted per-call on ``GenerateContentConfig``,
                not just at ``genai.Client`` construction time.
                ``genai.Client(api_key=...)`` (see ``__init__``) sets no
                client-level deadline, so this must be threaded per call or
                the timeout contract is silently false. None means no
                deadline -- the SDK's own defaults apply.

        Returns:
            ModelResult with provider="gemini", notional ``cost_usd``
            computed from ``_gemini_cost()`` (Gemini is in FREE_PROVIDERS so
            ``_maybe_record_cost`` forces the recorded value to 0.0, but the
            ModelResult carries the truthful amount), and schema_valid=True.

        Raises:
            ProviderTruncationExhaustedError: (a ValueError subclass) If the
                response body is still not valid JSON after the internal
                truncation retry below has already been exhausted.
            genai_errors.APIError: On non-transient API errors.
        """
        contents = self._build_contents(messages)

        response: Any = None
        data: Any = None
        parse_error: json.JSONDecodeError | None = None
        current_max_tokens = max_tokens

        # Truncation-aware retry (T2.8/D28): up to one retry, at
        # TRUNCATION_RETRY_TOKEN_MULTIPLIER x the original budget, when the
        # response was cut off mid-generation. See the constant's docstring
        # above for the detection rule and rationale.
        for truncation_attempt in range(2):
            gen_config = self._build_generate_config(
                system, current_max_tokens, output_schema, timeout
            )
            response = self._generate_with_transient_retry(model, contents, gen_config, timeout)

            parse_error = None
            if output_schema is not None:
                try:
                    data = json.loads(response.text)
                except json.JSONDecodeError as exc:
                    parse_error = exc
                    data = None
            else:
                data = {"text": response.text}

            truncated = parse_error is not None or _response_was_truncated(response)
            if not truncated or truncation_attempt > 0:
                break

            current_max_tokens = min(
                max_tokens * TRUNCATION_RETRY_TOKEN_MULTIPLIER,
                _TRUNCATION_RETRY_TOKEN_CEILING,
            )
            logger.warning(
                "Gemini response truncated (finish_reason=%s, json_error=%s) at "
                "max_tokens=%d, retrying once at max_tokens=%d",
                _finish_reason(response),
                parse_error is not None,
                max_tokens,
                current_max_tokens,
            )

        if parse_error is not None:
            logger.error("Gemini returned invalid JSON: %s", (response.text or "")[:200])
            # By construction, this branch is only reached after the
            # truncation-retry loop above has already run its second (and
            # final) attempt -- a parse failure on attempt 0 always sets
            # `truncated=True` and loops again, so a parse_error surviving
            # to here always means the internal retry was already spent.
            # Raise the distinct ProviderTruncationExhaustedError (not a
            # bare ValueError) so the cascade's own malformed-output retry
            # (model_provider._is_malformed_output_error) does not invoke
            # this adapter a second time on top of the retry already done
            # here -- see that exception's docstring for the compounding
            # network-call math this avoids (T2.8/D28 review, #1787).
            raise ProviderTruncationExhaustedError(
                f"Invalid JSON from Gemini: {parse_error}"
            ) from parse_error

        usage = response.usage_metadata
        input_tokens = (usage.prompt_token_count or 0) if usage else 0
        output_tokens = (usage.candidates_token_count or 0) if usage else 0
        cost_usd = _gemini_cost(model, input_tokens, output_tokens)

        return ModelResult(
            data=data,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="gemini",
            schema_valid=True,
        )

    def _build_generate_config(
        self,
        system: str,
        max_tokens: int,
        output_schema: dict | None,
        timeout: float | None,
    ) -> genai_types.GenerateContentConfig:
        """Build a GenerateContentConfig for one attempt (own max_output_tokens)."""
        config_kwargs: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
        }
        if output_schema is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_json_schema"] = output_schema
        if timeout is not None:
            config_kwargs["http_options"] = genai_types.HttpOptions(timeout=int(timeout * 1000))
        return genai_types.GenerateContentConfig(**config_kwargs)

    def _generate_with_transient_retry(
        self,
        model: str,
        contents: list[genai_types.Content],
        gen_config: genai_types.GenerateContentConfig,
        timeout: float | None,
    ) -> Any:
        """Call generate_content, retrying once on a transient error.

        Split out from ``call()`` so the outer truncation-retry loop can
        invoke it once per max_tokens budget without duplicating the
        transient-error handling (429 / rate-limit / timeout).
        """
        response = None
        last_exception: Exception | None = None

        for attempt in range(2):  # one retry on transient errors
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=gen_config,
                )
                break
            except Exception as exc:
                last_exception = exc
                # Permanent-zero quota (free tier exhausted) is not transient — retrying will never succeed
                is_permanent_zero = _is_permanent_zero_quota(exc)
                error_str = str(exc).lower()
                is_transient = not is_permanent_zero and (
                    "429" in error_str or "rate" in error_str or "timeout" in error_str
                )
                if attempt == 0 and is_transient:
                    # Clamp the transient-error retry sleep to the caller's
                    # own timeout budget (#1436) -- otherwise a tight
                    # `timeout` bounding the HTTP call above still lets this
                    # adapter sleep past it before the SECOND attempt, which
                    # would silently reopen the gap the http_options fix
                    # above just closed. No-op for the common case (retry
                    # sleep 15s < typical 60-300s tier timeouts).
                    retry_sleep = self._retry_sleep
                    if timeout is not None:
                        retry_sleep = min(retry_sleep, timeout)
                    logger.warning(
                        "Gemini transient error on attempt 1, retrying in %.1fs: %s",
                        retry_sleep,
                        exc,
                    )
                    time.sleep(retry_sleep)
                    continue
                if is_permanent_zero:
                    logger.warning(
                        "Gemini permanent-zero quota detected (QuotaFailure with quotaValue=0), skipping retry: %s",
                        exc,
                    )
                raise

        if response is None:
            raise last_exception or Exception("Gemini API returned no response")
        return response

    def _build_contents(self, messages: list[dict]) -> list[genai_types.Content]:
        """Translate adapter-style messages to genai.types.Content objects.

        The new SDK requires role to be "user" or "model" (not
        "assistant").  Map "assistant" -> "model" for callers that pass
        OpenAI-style role names.
        """
        role_map = {"assistant": "model"}
        return [
            genai_types.Content(
                role=role_map.get(msg.get("role", "user"), msg.get("role", "user")),
                parts=[genai_types.Part.from_text(text=msg["content"])],
            )
            for msg in messages
        ]

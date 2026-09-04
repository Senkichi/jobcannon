"""PORTED from job_finder/web/providers/groq_provider.py @ 9d57537f2f7100239181d34d0515cba342c83237
(private job-cannon). Ledger L-0240.
# PORT-SEAM: __init__ resolves its API key via a per-tenant CredentialResolver
# instead of get_secret() -- see the constructor below for the full seam.

Groq provider adapter — OpenAI-compatible chat completions.

Groq hosts open-weight models (Llama, Mixtral, etc.) via an OpenAI-compatible
REST API at https://api.groq.com/openai/v1. Default models are configured via
``_PROVIDER_DEFAULTS`` in ``model_provider.py`` (quick: ``llama-3.1-8b-instant``,
score: ``llama-3.3-70b-versatile``).

Phase 153 deliverable: re-implements the Groq adapter (previously built in
commit 2585f43, deleted in 38ea791) using the openrouter_provider template.

Issue 292 (2026-06-10): compute real ``cost_usd`` from usage tokens so that
``_maybe_record_cost`` in the cascade records truthful per-call spend.  Groq
has a free tier — a free-tier key bills nothing even though the meter records
a notional cost (conservative: gate trips early rather than never).
"""

from __future__ import annotations

import json
import logging

import requests

from jobcannon.engine.model_types import BaseProvider, CredentialResolver, ModelResult

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_TIMEOUT = 300.0

# ---------------------------------------------------------------------------
# Pricing table — price per million tokens (USD)
# Source: https://groq.com/pricing  last_verified: 2026-06-10
# ---------------------------------------------------------------------------
_GROQ_PRICING: dict[str, dict[str, float]] = {
    # quick-tier default (model_provider._PROVIDER_DEFAULTS groq.quick)
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
    # score-tier default (model_provider._PROVIDER_DEFAULTS groq.score)
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    # other production models available on Groq cloud
    "llama-4-scout-17b-16e-instruct": {"input": 0.11, "output": 0.34},
    "qwen-3-32b": {"input": 0.29, "output": 0.59},
}


def _groq_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return cost in USD for a Groq API call.

    Falls back to the most expensive known entry for unrecognised model IDs
    (conservative — gate trips early rather than never).
    """
    pricing = _GROQ_PRICING.get(model)
    if pricing is None:
        logger.warning(
            "Unknown Groq model '%s' in _groq_cost — using highest known pricing as fallback",
            model,
        )
        pricing = max(_GROQ_PRICING.values(), key=lambda p: p["input"] + p["output"])
    return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing[
        "output"
    ]


class GroqProvider(BaseProvider):
    """Provider adapter for Groq API (OpenAI-compatible).

    Resolves the API key via the standard secret-precedence stack
    (``GROQ_API_KEY`` env var → OS keyring → ``providers.api_keys.groq``
    in config.yaml). Raises ``ValueError`` immediately when no key is found
    so the cascade can skip the provider gracefully.

    Computes real ``cost_usd`` from usage tokens (Issue 292, 2026-06-10).
    Groq has a free tier — a free-tier key bills nothing even though the
    meter records a notional cost (conservative).  ``"groq"`` is intentionally
    kept out of ``FREE_PROVIDERS`` so the budget gate applies when a paid-tier
    key is present.

    Args:
        config: Application config dict.

    Raises:
        ValueError: If no Groq API key is available.
    """

    def __init__(self, config: dict, *, resolve_credential: CredentialResolver) -> None:
        # PORT-SEAM: per-tenant credential lookup replacing
        # get_secret("providers.api_keys.groq", config=config)
        self._api_key = resolve_credential("groq")
        if not self._api_key:
            raise ValueError(
                "Groq API key not set. Set GROQ_API_KEY env var, "
                "store it in the OS keyring, or add providers.api_keys.groq "
                "to config.yaml."
            )
        self._base_url = _DEFAULT_BASE_URL

    def call(
        self,
        model: str,
        system: str,
        messages: list[dict],
        output_schema: dict | None = None,
        max_tokens: int = 1024,
        timeout: float | None = None,
    ) -> ModelResult:
        """Make a chat completion call to Groq /openai/v1/chat/completions.

        Args:
            model: Groq model identifier, e.g. ``"llama-3.1-8b-instant"``.
            system: System prompt string.
            messages: List of message dicts ``[{role, content}]``.
            output_schema: JSON schema dict for structured output (or None).
                When provided, requests JSON output via
                ``response_format={"type": "json_object"}``.  Groq does not
                enforce the schema at the token level — application-side
                validation still applies.
            max_tokens: Maximum output tokens.  Defaults to 1024.
            timeout: Request timeout in seconds.  Defaults to 300.0.

        Returns:
            ``ModelResult`` with ``provider="groq"``, real ``cost_usd``
            computed from ``_groq_cost()``, and ``schema_valid=True``.

        Raises:
            requests.HTTPError: On non-2xx response from the Groq API.
            json.JSONDecodeError: If the response content is not valid JSON.
        """
        effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT

        payload: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }

        if output_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        resp = requests.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=effective_timeout,
        )
        resp.raise_for_status()

        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)

        usage = body.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        cost_usd = _groq_cost(model, input_tokens, output_tokens)

        return ModelResult(
            data=data,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            provider="groq",
            schema_valid=True,
        )

"""PORTED from tests/test_provider_gemini.py @ 9678c44c5d667d8a1d587c2d1f92b9df4056ead9
(private job-cannon). Ledger L-0239.

# PORT-SEAM: GeminiProvider.__init__ gained a required keyword-only
# `resolve_credential` param (see gemini_provider.py's own PORT-SEAM note),
# so every construction call site below threads one through -- almost all
# tests build the provider with an injected `client=`, so resolve_credential
# is never actually invoked in those and is passed as an unused stub via
# `_make_provider`'s new default.
#
# Dropped (private-only surface): test_init_builds_genai_client_from_env_var
# asserted the `os.environ.get(api_key_env)` fallback, which is deleted
# entirely hosted (an operator-level env var would resolve one key for
# every tenant). Replaced by
# test_init_builds_genai_client_from_resolved_credential, the same
# assertion against the CredentialResolver seam that replaces it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from jobcannon.engine.model_types import BaseProvider, ModelResult
from jobcannon.engine.providers.gemini_provider import (
    _GEMINI_PRICING,
    GeminiProvider,
    _gemini_cost,
    _is_permanent_zero_quota,
)


def _make_mock_client() -> MagicMock:
    return MagicMock()


def _make_provider(client=None, resolve_credential=None):
    if client is None:
        client = _make_mock_client()
    if resolve_credential is None:
        resolve_credential = lambda provider: None  # noqa: E731 -- unused, client provided
    return GeminiProvider(config={}, client=client, resolve_credential=resolve_credential)


def _make_response(text="hello", prompt_token_count=10, candidates_token_count=5):
    resp = MagicMock()
    resp.text = text
    resp.usage_metadata = MagicMock()
    resp.usage_metadata.prompt_token_count = prompt_token_count
    resp.usage_metadata.candidates_token_count = candidates_token_count
    return resp


def _make_truncated_response(text, prompt_token_count=10, candidates_token_count=5):
    """A response whose candidate reports finish_reason=MAX_TOKENS (T2.8/D28)."""
    resp = _make_response(
        text=text,
        prompt_token_count=prompt_token_count,
        candidates_token_count=candidates_token_count,
    )
    candidate = MagicMock()
    candidate.finish_reason = "MAX_TOKENS"
    resp.candidates = [candidate]
    return resp


_SCHEMA: dict = {
    "type": "object",
    "required": ["score"],
    "properties": {"score": {"type": "integer"}},
}


def test_gemini_provider_is_base_provider_subclass():
    assert issubclass(GeminiProvider, BaseProvider)


def test_init_raises_import_error_when_sdk_unavailable():
    with patch("jobcannon.engine.providers.gemini_provider._GENAI_AVAILABLE", False):
        with pytest.raises(ImportError, match="google-genai"):
            GeminiProvider(config={}, resolve_credential=lambda provider: None)


def test_init_raises_value_error_when_no_api_key():
    with pytest.raises(ValueError, match="Gemini API key not set"):
        GeminiProvider(config={}, resolve_credential=lambda provider: None)


def test_init_with_injected_client_skips_key_resolution():
    mock_client = _make_mock_client()
    provider = GeminiProvider(
        config={}, client=mock_client, resolve_credential=lambda provider: None
    )
    assert provider._client is mock_client


def test_init_builds_genai_client_from_resolved_credential():
    with patch("jobcannon.engine.providers.gemini_provider.genai") as mock_genai:
        mock_genai.Client.return_value = MagicMock()
        provider = GeminiProvider(config={}, resolve_credential=lambda provider: "test-key-xyz")
    mock_genai.Client.assert_called_once_with(api_key="test-key-xyz")
    assert provider._client is mock_genai.Client.return_value


def test_init_reads_retry_sleep_from_config():
    cfg = {"providers": {"gemini": {"retry_sleep_seconds": 3.0}}}
    provider = GeminiProvider(
        config=cfg, client=_make_mock_client(), resolve_credential=lambda provider: None
    )
    assert provider._retry_sleep == 3.0


def test_init_default_retry_sleep():
    assert _make_provider()._retry_sleep == 15.0


def test_call_freeform_returns_model_result_shape():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(text="some text")
    result = _make_provider(client).call(
        "gemini-2.5-flash", "sys", [{"role": "user", "content": "hi"}]
    )
    assert isinstance(result, ModelResult)
    assert result.provider == "gemini"
    assert result.model == "gemini-2.5-flash"
    # Gemini is is_free -- the host's record_cost zeroes it at record time.
    # The adapter computes a notional cost so the ModelResult is truthful.
    expected_cost = _gemini_cost("gemini-2.5-flash", 10, 5)
    assert abs(result.cost_usd - expected_cost) < 1e-9
    assert result.schema_valid is True
    assert result.data == {"text": "some text"}


def test_call_freeform_token_counts():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(
        prompt_token_count=42, candidates_token_count=17
    )
    result = _make_provider(client).call(
        "gemini-2.5-flash", "sys", [{"role": "user", "content": "hi"}]
    )
    assert result.input_tokens == 42
    assert result.output_tokens == 17
    # Notional cost computed from token counts
    assert result.cost_usd == _gemini_cost("gemini-2.5-flash", 42, 17)


def test_call_handles_none_usage_metadata():
    resp = _make_response()
    resp.usage_metadata = None
    client = _make_mock_client()
    client.models.generate_content.return_value = resp
    result = _make_provider(client).call(
        "gemini-2.5-flash", "sys", [{"role": "user", "content": "q"}]
    )
    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_call_with_schema_parses_json_response():
    payload = {"score": 85}
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(text=json.dumps(payload))
    result = _make_provider(client).call(
        "gemini-2.5-flash",
        "sys",
        [{"role": "user", "content": "score this"}],
        output_schema=_SCHEMA,
    )
    assert result.data == payload
    assert result.schema_valid is True


def test_call_with_schema_raises_value_error_on_invalid_json():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(text="NOT JSON")
    with pytest.raises(ValueError, match="Invalid JSON from Gemini"):
        _make_provider(client).call(
            "gemini-2.5-flash",
            "sys",
            [{"role": "user", "content": "q"}],
            output_schema=_SCHEMA,
        )


def test_call_passes_system_and_max_tokens_in_config():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response()
    provider = _make_provider(client)

    with patch(
        "jobcannon.engine.providers.gemini_provider.genai_types.GenerateContentConfig"
    ) as mock_cfg_cls:
        mock_cfg_cls.return_value = MagicMock()
        provider.call(
            "gemini-2.5-flash",
            "my system",
            [{"role": "user", "content": "hi"}],
            max_tokens=512,
        )

    kwargs = mock_cfg_cls.call_args.kwargs
    assert kwargs["system_instruction"] == "my system"
    assert kwargs["max_output_tokens"] == 512
    assert "response_mime_type" not in kwargs


def test_call_passes_schema_fields_in_config():
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(text=json.dumps({"score": 1}))

    with patch(
        "jobcannon.engine.providers.gemini_provider.genai_types.GenerateContentConfig"
    ) as mock_cfg_cls:
        mock_cfg_cls.return_value = MagicMock()
        _make_provider(client).call(
            "gemini-2.5-flash",
            "sys",
            [{"role": "user", "content": "q"}],
            output_schema=_SCHEMA,
        )

    kwargs = mock_cfg_cls.call_args.kwargs
    assert kwargs["response_mime_type"] == "application/json"
    assert kwargs["response_json_schema"] == _SCHEMA


def test_call_retries_once_on_429_api_error_then_succeeds():
    """Transient 429 (rate limit) should retry once then succeed."""
    from google.genai import errors as genai_errors

    # Faithful transient error structure -- no QuotaFailure, just a rate limit
    transient_body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "Rate limit exceeded",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "15s",
                }
            ],
        }
    }
    transient_exc = genai_errors.APIError(429, transient_body)
    good_resp = _make_response(text="ok")
    client = _make_mock_client()
    client.models.generate_content.side_effect = [transient_exc, good_resp]

    provider = GeminiProvider(
        config={"providers": {"gemini": {"retry_sleep_seconds": 0}}},
        client=client,
        resolve_credential=lambda provider: None,
    )
    with patch("jobcannon.engine.providers.gemini_provider.time.sleep") as mock_sleep:
        result = provider.call("gemini-2.5-flash", "sys", [{"role": "user", "content": "hi"}])

    assert result.data == {"text": "ok"}
    mock_sleep.assert_called_once_with(0)
    assert client.models.generate_content.call_count == 2


def test_call_raises_immediately_on_non_transient_error():
    from google.genai import errors as genai_errors

    hard_exc = genai_errors.APIError(400, "bad request", {"error": {}})
    client = _make_mock_client()
    client.models.generate_content.side_effect = hard_exc

    with patch("jobcannon.engine.providers.gemini_provider.time.sleep") as mock_sleep:
        with pytest.raises(genai_errors.APIError):
            _make_provider(client).call(
                "gemini-2.5-flash", "sys", [{"role": "user", "content": "q"}]
            )

    mock_sleep.assert_not_called()
    assert client.models.generate_content.call_count == 1


def test_call_retries_on_string_429_in_generic_exception():
    good_resp = _make_response(text="retry worked")
    generic_exc = RuntimeError("HTTP 429 Too Many Requests")
    client = _make_mock_client()
    client.models.generate_content.side_effect = [generic_exc, good_resp]

    provider = GeminiProvider(
        config={"providers": {"gemini": {"retry_sleep_seconds": 0}}},
        client=client,
        resolve_credential=lambda provider: None,
    )
    with patch("jobcannon.engine.providers.gemini_provider.time.sleep"):
        result = provider.call("gemini-2.5-flash", "sys", [{"role": "user", "content": "q"}])
    assert result.data == {"text": "retry worked"}


def test_call_no_retry_on_permanent_zero_quota():
    """Permanent-zero quota (QuotaFailure with quotaValue=0) should not retry -- it will never succeed."""
    from google.genai import errors as genai_errors

    # Faithful error structure matching real google-genai SDK RESOURCE_EXHAUSTED 429
    permanent_zero_body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "Quota exceeded",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaValue": "0",  # String zero, as per Google's API
                            "metric": "generativelanguage.googleapis.com/generate_content_requests",
                        }
                    ],
                }
            ],
        }
    }
    permanent_zero_exc = genai_errors.APIError(429, permanent_zero_body)
    client = _make_mock_client()
    client.models.generate_content.side_effect = permanent_zero_exc

    provider = GeminiProvider(
        config={"providers": {"gemini": {"retry_sleep_seconds": 0}}},
        client=client,
        resolve_credential=lambda provider: None,
    )
    with patch("jobcannon.engine.providers.gemini_provider.time.sleep") as mock_sleep:
        with pytest.raises(genai_errors.APIError):
            provider.call("gemini-2.5-flash", "sys", [{"role": "user", "content": "q"}])

    mock_sleep.assert_not_called()
    assert client.models.generate_content.call_count == 1


def test_is_permanent_zero_quota_detects_quota_failure_with_zero():
    """_is_permanent_zero_quota returns True for QuotaFailure with quotaValue=0."""
    from google.genai import errors as genai_errors

    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaValue": "0"}],
                }
            ],
        }
    }
    exc = genai_errors.APIError(429, body)
    assert _is_permanent_zero_quota(exc) is True


def test_is_permanent_zero_quota_false_on_transient_429():
    """_is_permanent_zero_quota returns False for transient 429 without zero quota."""
    from google.genai import errors as genai_errors

    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "15s",
                }
            ],
        }
    }
    exc = genai_errors.APIError(429, body)
    assert _is_permanent_zero_quota(exc) is False


def test_is_permanent_zero_quota_false_on_non_apierror():
    """_is_permanent_zero_quota returns False for non-APIError exceptions."""
    assert _is_permanent_zero_quota(RuntimeError("some error")) is False


def test_is_permanent_zero_quota_false_on_wrong_status_code():
    """_is_permanent_zero_quota returns False for non-429 status codes."""
    from google.genai import errors as genai_errors

    body = {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaValue": "0"}],
                }
            ],
        }
    }
    exc = genai_errors.APIError(400, body)
    assert _is_permanent_zero_quota(exc) is False


def test_is_permanent_zero_quota_false_on_non_zero_quota():
    """_is_permanent_zero_quota returns False for QuotaFailure with non-zero quota."""
    from google.genai import errors as genai_errors

    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaValue": "100"}],
                }
            ],
        }
    }
    exc = genai_errors.APIError(429, body)
    assert _is_permanent_zero_quota(exc) is False


def test_build_contents_user_role_unchanged():
    contents = _make_provider()._build_contents([{"role": "user", "content": "hello"}])
    assert len(contents) == 1
    assert contents[0].role == "user"


def test_build_contents_assistant_role_mapped_to_model():
    contents = _make_provider()._build_contents([{"role": "assistant", "content": "hi"}])
    assert contents[0].role == "model"


def test_build_contents_model_role_unchanged():
    contents = _make_provider()._build_contents([{"role": "model", "content": "hi"}])
    assert contents[0].role == "model"


def test_build_contents_defaults_missing_role_to_user():
    contents = _make_provider()._build_contents([{"content": "no role"}])
    assert contents[0].role == "user"


def test_build_contents_multiple_messages():
    messages = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
    ]
    contents = _make_provider()._build_contents(messages)
    assert len(contents) == 3
    assert [c.role for c in contents] == ["user", "model", "user"]


# ---------------------------------------------------------------------------
# Issue 292 -- pricing table + notional cost computation
# ---------------------------------------------------------------------------


def test_gemini_cost_flash_model():
    """gemini-2.5-flash: 1M in + 1M out = $0.30 + $2.50 = $2.80."""
    cost = _gemini_cost("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert abs(cost - 2.80) < 1e-9


def test_gemini_cost_pro_model():
    """gemini-2.5-pro: 1M in + 1M out = $1.25 + $10.00 = $11.25."""
    cost = _gemini_cost("gemini-2.5-pro", 1_000_000, 1_000_000)
    assert abs(cost - 11.25) < 1e-9


def test_gemini_cost_unknown_model_uses_most_expensive_fallback():
    """Unknown model falls back to the most expensive entry in _GEMINI_PRICING."""
    most_expensive = max(_GEMINI_PRICING.values(), key=lambda p: p["input"] + p["output"])
    cost_unknown = _gemini_cost("gemini-3.0-ultra", 1_000_000, 1_000_000)
    expected = most_expensive["input"] + most_expensive["output"]
    assert abs(cost_unknown - expected) < 1e-9


def test_gemini_cost_zero_tokens():
    """Zero tokens -> $0.00."""
    assert _gemini_cost("gemini-2.5-flash", 0, 0) == 0.0


def test_gemini_cost_usd_in_model_result_is_notional():
    """Gemini adapter cost_usd is notional (record_cost zeroes it for is_free providers)."""
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(
        prompt_token_count=500_000, candidates_token_count=100_000
    )
    result = _make_provider(client).call(
        "gemini-2.5-flash", "sys", [{"role": "user", "content": "hi"}]
    )
    # Notional cost: 500k/1M * 0.30 + 100k/1M * 2.50 = 0.15 + 0.25 = 0.40
    assert abs(result.cost_usd - 0.40) < 1e-9
    # The ModelResult carries the notional amount; record_cost forces 0 when recorded


# ---------------------------------------------------------------------------
# Truncation-aware retry (T2.8/D28)
# ---------------------------------------------------------------------------


def test_call_retries_once_on_truncated_response_then_succeeds():
    """finish_reason=MAX_TOKENS on attempt 1 -> exactly one retry at a
    larger budget, and the (clean) second response is what's returned."""
    from jobcannon.engine.providers.gemini_provider import TRUNCATION_RETRY_TOKEN_MULTIPLIER

    truncated_resp = _make_truncated_response(text='{"score": 1')  # incomplete JSON
    clean_resp = _make_response(text=json.dumps({"score": 1}))
    client = _make_mock_client()
    client.models.generate_content.side_effect = [truncated_resp, clean_resp]

    result = _make_provider(client).call(
        "gemini-2.5-flash",
        "sys",
        [{"role": "user", "content": "q"}],
        output_schema=_SCHEMA,
        max_tokens=1024,
    )

    assert result.data == {"score": 1}
    assert client.models.generate_content.call_count == 2
    call_kwargs = client.models.generate_content.call_args_list
    assert call_kwargs[0].kwargs["config"].max_output_tokens == 1024
    assert (
        call_kwargs[1].kwargs["config"].max_output_tokens
        == 1024 * TRUNCATION_RETRY_TOKEN_MULTIPLIER
    )


def test_call_no_retry_on_clean_response():
    """A well-formed, non-truncated response should NOT trigger a retry."""
    client = _make_mock_client()
    client.models.generate_content.return_value = _make_response(text=json.dumps({"score": 1}))

    result = _make_provider(client).call(
        "gemini-2.5-flash",
        "sys",
        [{"role": "user", "content": "q"}],
        output_schema=_SCHEMA,
        max_tokens=1024,
    )

    assert result.data == {"score": 1}
    assert client.models.generate_content.call_count == 1


def test_call_truncated_on_both_attempts_raises_without_third_call():
    """Truncated JSON on both attempts surfaces as a parse failure -- no
    third call is made past the single retry."""
    truncated_resp_1 = _make_truncated_response(text='{"score": 1')
    truncated_resp_2 = _make_truncated_response(text='{"score": 1, "extra')
    client = _make_mock_client()
    client.models.generate_content.side_effect = [truncated_resp_1, truncated_resp_2]

    with pytest.raises(ValueError, match="Invalid JSON from Gemini"):
        _make_provider(client).call(
            "gemini-2.5-flash",
            "sys",
            [{"role": "user", "content": "q"}],
            output_schema=_SCHEMA,
            max_tokens=1024,
        )

    assert client.models.generate_content.call_count == 2


def test_call_retries_once_on_valid_json_but_finish_reason_max_tokens():
    """Even when the truncated text happens to still parse as valid JSON,
    finish_reason=MAX_TOKENS alone is enough to trigger the retry."""
    truncated_but_parseable = _make_truncated_response(text=json.dumps({"score": 1}))
    clean_resp = _make_response(text=json.dumps({"score": 2}))
    client = _make_mock_client()
    client.models.generate_content.side_effect = [truncated_but_parseable, clean_resp]

    result = _make_provider(client).call(
        "gemini-2.5-flash",
        "sys",
        [{"role": "user", "content": "q"}],
        output_schema=_SCHEMA,
        max_tokens=1024,
    )

    assert result.data == {"score": 2}
    assert client.models.generate_content.call_count == 2


def test_call_retry_budget_clamped_to_ceiling_not_raw_multiplier():
    """A large caller max_tokens must not escalate past
    _TRUNCATION_RETRY_TOKEN_CEILING -- proves the retry budget is bounded
    independent of the caller's own max_tokens, since call_model's own
    schema-validation retry can invoke GeminiProvider.call() again on top of
    this internal retry."""
    from jobcannon.engine.providers.gemini_provider import _TRUNCATION_RETRY_TOKEN_CEILING

    truncated_resp = _make_truncated_response(text='{"score": 1')
    clean_resp = _make_response(text=json.dumps({"score": 1}))
    client = _make_mock_client()
    client.models.generate_content.side_effect = [truncated_resp, clean_resp]

    _make_provider(client).call(
        "gemini-2.5-flash",
        "sys",
        [{"role": "user", "content": "q"}],
        output_schema=_SCHEMA,
        max_tokens=4096,  # x4 would be 16384, above the ceiling
    )

    call_kwargs = client.models.generate_content.call_args_list
    assert call_kwargs[1].kwargs["config"].max_output_tokens == _TRUNCATION_RETRY_TOKEN_CEILING
    assert _TRUNCATION_RETRY_TOKEN_CEILING < 4096 * 4


def test_finish_reason_detects_real_sdk_enum_value():
    """Guards against an SDK bump silently breaking detection: the real
    google-genai FinishReason enum (not just the plain-string test
    stand-in used elsewhere in this file) must be recognized as MAX_TOKENS
    via the .value getattr path in _finish_reason."""
    genai_types = pytest.importorskip("google.genai.types")
    from jobcannon.engine.providers.gemini_provider import _response_was_truncated

    resp = _make_response(text='{"score": 1')
    candidate = MagicMock()
    candidate.finish_reason = genai_types.FinishReason.MAX_TOKENS
    resp.candidates = [candidate]

    assert _response_was_truncated(resp) is True

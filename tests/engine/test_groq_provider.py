"""PORTED from tests/test_groq_provider.py @ 9d57537f2f7100239181d34d0515cba342c83237
(private job-cannon). Ledger L-0240.

# PORT-SEAM: construction tests rewritten to pass a CredentialResolver
# (`lambda provider: "..."`) instead of patching GROQ_API_KEY into
# os.environ -- the private secret-precedence stack (env var -> keyring ->
# config.yaml) does not carry hosted (see groq_provider.py's own PORT-SEAM
# note). call()/cost tests carry unchanged since the REST request assembly,
# response parse, and _groq_cost pricing table are byte-identical carries.
#
# Dropped (private-only surfaces, per L-0036's seam list -- neither carries
# hosted): test_groq_result_flows_through_maybe_record_cost (asserted on
# `_maybe_record_cost` writing a `scoring_costs` row via
# job_finder.web.db_migrate; hosted's record_cost is a structured-log sink,
# no scoring_costs table exists) and test_groq_cost_included_in_cost_gate_sum
# (asserted on claude_client.cost_gate/FREE_PROVIDERS, the OWNER daily-spend
# gate, which has no meaning under BYO-key -- every call bills the tenant's
# own key).
"""

import json
from unittest.mock import Mock, patch

import pytest

from jobcannon.engine.model_types import ModelResult
from jobcannon.engine.providers.groq_provider import (
    _GROQ_PRICING,
    GroqProvider,
    _groq_cost,
)


def test_groq_provider_init_with_key():
    """GroqProvider initialises when the resolver returns a key."""
    provider = GroqProvider(config={}, resolve_credential=lambda provider: "test-groq-key")
    assert provider._api_key == "test-groq-key"
    assert provider._base_url == "https://api.groq.com/openai/v1"


def test_groq_provider_init_no_key_raises():
    """GroqProvider raises ValueError (not crashes) when the resolver has
    no key -- the cascade skips it."""
    with pytest.raises(ValueError, match="Groq API key not set"):
        GroqProvider(config={}, resolve_credential=lambda provider: None)


def test_groq_provider_call_returns_model_result():
    """GroqProvider.call() returns a valid ModelResult with correct fields."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"score": 3, "label": "apply"})}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 80},
    }
    mock_response.raise_for_status = Mock()

    with patch("requests.post", return_value=mock_response) as mock_post:
        provider = GroqProvider(config={}, resolve_credential=lambda provider: "test-key")
        result = provider.call(
            model="llama-3.1-8b-instant",
            system="Score this job",
            messages=[{"role": "user", "content": "Job description here"}],
        )

        # Correct endpoint and payload shape
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["model"] == "llama-3.1-8b-instant"
        assert call_kwargs["json"]["temperature"] == 0
        assert call_kwargs["headers"]["Authorization"] == "Bearer test-key"
        url = mock_post.call_args[0][0]
        assert url == "https://api.groq.com/openai/v1/chat/completions"

        # System prompt injected as first message
        sent_messages = call_kwargs["json"]["messages"]
        assert sent_messages[0] == {"role": "system", "content": "Score this job"}

        # ModelResult contract
        assert isinstance(result, ModelResult)
        assert result.provider == "groq"
        # cost_usd must be > 0 for a known model with real token counts
        expected_cost = _groq_cost("llama-3.1-8b-instant", 200, 80)
        assert abs(result.cost_usd - expected_cost) < 1e-9
        assert result.cost_usd > 0.0
        assert result.input_tokens == 200
        assert result.output_tokens == 80
        assert result.schema_valid is True
        assert result.data == {"score": 3, "label": "apply"}


def test_groq_provider_call_with_output_schema_adds_response_format():
    """output_schema triggers response_format=json_object in the request payload."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"result": "ok"})}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }
    mock_response.raise_for_status = Mock()

    schema = {"type": "object", "properties": {"result": {"type": "string"}}}

    with patch("requests.post", return_value=mock_response) as mock_post:
        provider = GroqProvider(config={}, resolve_credential=lambda provider: "test-key")
        result = provider.call(
            model="llama-3.3-70b-versatile",
            system="Test",
            messages=[{"role": "user", "content": "Test"}],
            output_schema=schema,
        )

        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["response_format"] == {"type": "json_object"}
        assert result.provider == "groq"


def test_groq_provider_call_without_output_schema_omits_response_format():
    """When output_schema is None, response_format is not sent."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"x": 1})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    mock_response.raise_for_status = Mock()

    with patch("requests.post", return_value=mock_response) as mock_post:
        provider = GroqProvider(config={}, resolve_credential=lambda provider: "test-key")
        provider.call(
            model="llama-3.1-8b-instant",
            system="Test",
            messages=[{"role": "user", "content": "Test"}],
            output_schema=None,
        )

        call_kwargs = mock_post.call_args[1]
        assert "response_format" not in call_kwargs["json"]


def test_groq_provider_call_missing_usage_defaults_to_zero():
    """Missing usage block defaults input_tokens and output_tokens to 0."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"ok": True})}}],
        # no "usage" key
    }
    mock_response.raise_for_status = Mock()

    with patch("requests.post", return_value=mock_response):
        provider = GroqProvider(config={}, resolve_credential=lambda provider: "test-key")
        result = provider.call(
            model="llama-3.1-8b-instant",
            system="Test",
            messages=[{"role": "user", "content": "Test"}],
        )

    assert result.input_tokens == 0
    assert result.output_tokens == 0
    # Zero tokens -> zero cost (no rounding issues)
    assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Issue 292 -- pricing table + cost computation
# ---------------------------------------------------------------------------


def test_groq_cost_known_model_8b():
    """llama-3.1-8b-instant: 1M in + 1M out = $0.05 + $0.08 = $0.13."""
    cost = _groq_cost("llama-3.1-8b-instant", 1_000_000, 1_000_000)
    assert abs(cost - 0.13) < 1e-9


def test_groq_cost_known_model_70b():
    """llama-3.3-70b-versatile: 1M in + 1M out = $0.59 + $0.79 = $1.38."""
    cost = _groq_cost("llama-3.3-70b-versatile", 1_000_000, 1_000_000)
    assert abs(cost - 1.38) < 1e-9


def test_groq_cost_unknown_model_uses_most_expensive_fallback():
    """Unknown model falls back to the most expensive entry in _GROQ_PRICING."""
    most_expensive = max(_GROQ_PRICING.values(), key=lambda p: p["input"] + p["output"])
    cost_unknown = _groq_cost("unknown-model-xyz", 1_000_000, 1_000_000)
    expected = most_expensive["input"] + most_expensive["output"]
    assert abs(cost_unknown - expected) < 1e-9


def test_groq_cost_partial_tokens():
    """500k in + 100k out for llama-3.1-8b-instant."""
    # 500k / 1M * 0.05 + 100k / 1M * 0.08 = 0.025 + 0.008 = 0.033
    cost = _groq_cost("llama-3.1-8b-instant", 500_000, 100_000)
    assert abs(cost - 0.033) < 1e-9

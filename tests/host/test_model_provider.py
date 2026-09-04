"""jobcannon.host.model_provider -- hosted cascade dispatcher (L-0036 PR-1,
ADAPT extraction of job_finder/web/model_provider.py).

This module is an ADAPT extraction split across two files and is exempt
from verbatim fidelity-diff comparison; it is verified instead by
behavior/tests, plus a structural diff showing the deadline machinery
carried unchanged -- this file is that behavioral verification. Pure
functions (resolve_hosted_routing, schema validation, degenerate detection)
get direct unit tests; call_model's cascade orchestration is exercised with
_make_adapter, get_active_providers, and build_credential_resolver
monkeypatched, since the real gemini/groq/cerebras adapters
(jobcannon/engine/providers/*) do not exist yet (PR-2, draft #281, is a
separate unit) -- _make_adapter's ImportError-until-then behavior is itself
asserted below.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.model_types import BaseProvider, ModelResult
from jobcannon.host import model_provider as mp


# ---------------------------------------------------------------------------
# resolve_hosted_routing -- pure function
# ---------------------------------------------------------------------------


def test_resolve_hosted_routing_picks_first_eligible_in_preference_order():
    routing = mp.resolve_hosted_routing("quick", ["cerebras", "gemini", "groq"])

    assert routing["provider"] == "gemini"  # HOSTED_ELIGIBLE_PROVIDERS order, not input order
    assert routing["model"] == "gemini-2.5-flash"
    assert [e["provider"] for e in routing["fallback_chain"]] == ["groq", "cerebras"]


def test_resolve_hosted_routing_excludes_unavailable_providers():
    routing = mp.resolve_hosted_routing("score", ["groq"])

    assert routing["provider"] == "groq"
    assert routing["fallback_chain"] == []


def test_resolve_hosted_routing_unknown_tier_raises_value_error():
    with pytest.raises(ValueError):
        mp.resolve_hosted_routing("not-a-real-tier", ["gemini"])


def test_resolve_hosted_routing_no_candidates_raises_cascade_exhausted():
    with pytest.raises(mp.ProviderCascadeExhaustedError):
        mp.resolve_hosted_routing("quick", [])


def test_resolve_hosted_routing_ignores_non_hosted_eligible_names():
    # "ollama"/"anthropic" are real provider_catalog entries but not
    # hosted-eligible -- must never be selectable via this path.
    with pytest.raises(mp.ProviderCascadeExhaustedError):
        mp.resolve_hosted_routing("quick", ["ollama", "anthropic"])


def test_resolve_hosted_routing_triage_has_no_hosted_defaults():
    # None of gemini/groq/cerebras carry a "triage" PROVIDER_DEFAULTS entry
    # (provider_catalog.py) -- structurally exhausted regardless of which
    # providers the tenant has configured.
    with pytest.raises(mp.ProviderCascadeExhaustedError):
        mp.resolve_hosted_routing("triage", ["gemini", "groq", "cerebras"])


# ---------------------------------------------------------------------------
# _make_adapter -- eligibility gate + PR-2 dependency boundary
# ---------------------------------------------------------------------------


def test_make_adapter_rejects_non_hosted_eligible_provider():
    with pytest.raises(ValueError):
        mp._make_adapter("ollama", {}, lambda provider: None)


def test_make_adapter_raises_import_error_until_pr2_adapters_land():
    """jobcannon/engine/providers/{gemini,groq,cerebras}_provider.py are
    PR-2 scope (draft #281), not this unit -- _make_adapter's lazy imports
    must raise ImportError (caught by call_model's cascade loop) rather than
    crash the process, until that PR lands."""
    with pytest.raises(ImportError):
        mp._make_adapter("gemini", {}, lambda provider: "key")


def test_make_adapter_never_caches_across_calls(monkeypatch):
    """Modularity note item 1 (HIGH): no memoization -- two calls for
    the same provider must not return the same object, since caching by
    provider name alone would hand tenant B tenant A's adapter (and the key
    closed over inside it)."""
    calls = []

    class FakeGeminiModule:
        class GeminiProvider:
            def __init__(self, *, config, resolve_credential):
                calls.append(resolve_credential)

    monkeypatch.setitem(
        __import__("sys").modules,
        "jobcannon.engine.providers.gemini_provider",
        FakeGeminiModule(),
    )

    resolver_a = lambda provider: "key-a"  # noqa: E731
    resolver_b = lambda provider: "key-b"  # noqa: E731
    mp._make_adapter("gemini", {}, resolver_a)
    mp._make_adapter("gemini", {}, resolver_b)

    assert calls == [resolver_a, resolver_b]  # fresh construction, no cache reuse


# ---------------------------------------------------------------------------
# Schema validation / sanitization / degenerate detection -- PORT-SEAM
# byte-identical to private; regression coverage for the port.
# ---------------------------------------------------------------------------


def test_validate_schema_returns_empty_list_when_schema_none():
    assert mp._validate_schema({"a": 1}, None) == []


def test_validate_schema_reports_violation():
    schema = {"type": "object", "required": ["a"]}
    errors = mp._validate_schema({}, schema)
    assert len(errors) == 1


def test_sanitize_output_coerces_stringy_integer():
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    result = mp._sanitize_output({"count": "3.0"}, schema)
    assert result["count"] == 3


def test_sanitize_output_coerces_enum_by_prefix_match():
    schema = {
        "type": "object",
        "properties": {"level": {"type": "string", "enum": ["low", "high"]}},
    }
    result = mp._sanitize_output({"level": "highly likely"}, schema)
    assert result["level"] == "high"


def _degenerate_axes() -> dict:
    return dict.fromkeys(mp._SCORING_AXIS_KEYS, 3)


def test_is_degenerate_assessment_true_for_uniform_axes_empty_rationale():
    data = _degenerate_axes()
    data["rationale"] = {k: [] for k in mp._RATIONALE_ARRAY_KEYS}
    assert mp.is_degenerate_assessment(data) is True


def test_is_degenerate_assessment_false_when_axes_vary():
    data = _degenerate_axes()
    data["title_fit"] = 5
    data["rationale"] = {k: [] for k in mp._RATIONALE_ARRAY_KEYS}
    assert mp.is_degenerate_assessment(data) is False


def test_is_degenerate_assessment_false_when_rationale_has_content():
    data = _degenerate_axes()
    data["rationale"] = {
        "strengths": ["real reason"],
        "gaps": [],
        "talking_points": [],
        "resume_priority_skills": [],
    }
    assert mp.is_degenerate_assessment(data) is False


def test_is_degenerate_assessment_false_when_not_a_scoring_shape():
    assert mp.is_degenerate_assessment({"unrelated": "shape"}) is False


# ---------------------------------------------------------------------------
# record_cost
# ---------------------------------------------------------------------------


def test_record_cost_raises_on_empty_provider():
    with pytest.raises(ValueError):
        mp.record_cost(provider="", model="m", cost_usd=0.0)


def test_record_cost_logs_and_does_not_raise(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="jobcannon.host.model_provider"):
        mp.record_cost(provider="gemini", model="gemini-2.5-flash", cost_usd=0.001, user_id="u1")
    assert any("record_cost" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# call_model -- cascade orchestration, adapters/DB monkeypatched
# ---------------------------------------------------------------------------


class _FakeAdapter(BaseProvider):
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = 0

    def call(self, model, system, messages, output_schema=None, max_tokens=1024, timeout=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


def _ok_result(provider="gemini", model="gemini-2.5-flash", data=None) -> ModelResult:
    return ModelResult(
        data=data if data is not None else {"ok": True},
        cost_usd=0.001,
        input_tokens=10,
        output_tokens=5,
        model=model,
        provider=provider,
        schema_valid=True,
    )


@pytest.fixture()
def _no_db_credentials(monkeypatch):
    """Stub the two DB-touching seams call_model reaches for: DB access
    itself is out of scope for these orchestration tests (covered by
    tests/host/test_credentials.py and test_byo_key_credentials.py)."""

    def fake_get_active_providers(conn, user_id):
        return conn["available_providers"]

    monkeypatch.setattr(
        "jobcannon.db._byo_key_credentials.get_active_providers", fake_get_active_providers
    )

    def fake_build_resolver(conn, user_id):
        return lambda provider: "fake-api-key"

    monkeypatch.setattr(mp._credentials, "build_credential_resolver", fake_build_resolver)


def test_call_model_happy_path_returns_result_and_records_cost(monkeypatch, _no_db_credentials):
    adapter = _FakeAdapter(result=_ok_result())
    monkeypatch.setattr(mp, "_make_adapter", lambda provider, config, resolve_credential: adapter)

    recorded = []
    monkeypatch.setattr(mp, "record_cost", lambda **kw: recorded.append(kw))

    conn = {"available_providers": ["gemini"]}
    result = mp.call_model(
        "quick", "sys", [{"role": "user", "content": "hi"}], conn, {}, user_id="u1"
    )

    assert result.provider == "gemini"
    assert adapter.calls == 1
    assert recorded and recorded[0]["provider"] == "gemini"


def test_call_model_no_user_id_raises_cascade_exhausted(_no_db_credentials):
    conn = {"available_providers": []}
    with pytest.raises(mp.ProviderCascadeExhaustedError):
        mp.call_model("quick", "sys", [{"role": "user", "content": "hi"}], conn, {})


def test_call_model_falls_through_cascade_when_first_provider_errors(
    monkeypatch, _no_db_credentials
):
    failing = _FakeAdapter(error=RuntimeError("boom"))
    succeeding = _FakeAdapter(result=_ok_result(provider="groq", model="llama-3.1-8b-instant"))

    def fake_make_adapter(provider, config, resolve_credential):
        return failing if provider == "gemini" else succeeding

    monkeypatch.setattr(mp, "_make_adapter", fake_make_adapter)
    monkeypatch.setattr(mp, "record_cost", lambda **kw: None)

    conn = {"available_providers": ["gemini", "groq"]}
    result = mp.call_model(
        "quick", "sys", [{"role": "user", "content": "hi"}], conn, {}, user_id="u1"
    )

    assert result.provider == "groq"
    assert failing.calls == 1
    assert succeeding.calls == 1


def test_call_model_unavailable_adapter_skips_to_next(monkeypatch, _no_db_credentials):
    """_make_adapter raising ValueError/RuntimeError/ImportError (e.g. the
    PR-2-not-landed-yet case) must be caught and advance the cascade, not
    propagate."""
    succeeding = _FakeAdapter(result=_ok_result(provider="groq", model="llama-3.1-8b-instant"))

    def fake_make_adapter(provider, config, resolve_credential):
        if provider == "gemini":
            raise ImportError("adapter module not present")
        return succeeding

    monkeypatch.setattr(mp, "_make_adapter", fake_make_adapter)
    monkeypatch.setattr(mp, "record_cost", lambda **kw: None)

    conn = {"available_providers": ["gemini", "groq"]}
    result = mp.call_model(
        "quick", "sys", [{"role": "user", "content": "hi"}], conn, {}, user_id="u1"
    )

    assert result.provider == "groq"


def test_call_model_all_degenerate_returns_last_flagged_degenerate(monkeypatch, _no_db_credentials):
    degenerate_data = dict.fromkeys(mp._SCORING_AXIS_KEYS, 3)
    degenerate_data["rationale"] = {k: [] for k in mp._RATIONALE_ARRAY_KEYS}
    adapter = _FakeAdapter(result=_ok_result(data=degenerate_data))

    monkeypatch.setattr(mp, "_make_adapter", lambda provider, config, resolve_credential: adapter)
    monkeypatch.setattr(mp, "record_cost", lambda **kw: None)

    conn = {"available_providers": ["gemini"]}
    result = mp.call_model(
        "score", "sys", [{"role": "user", "content": "hi"}], conn, {}, user_id="u1"
    )

    assert result.degenerate is True
    assert result.provider == "gemini"


def test_call_model_timeout_raises_cascade_timeout_error(monkeypatch, _no_db_credentials):
    """Deterministic deadline-exhaustion test: monkeypatch time.monotonic so
    the second read is already past the deadline, instead of a real sleep
    (avoids the wall-clock-flake class -- see MEMORY.md 'Wall-clock flakes:
    5 signatures')."""
    ticks = iter([1000.0, 1010.0])  # deadline anchor (t+5s budget), then well-past it

    monkeypatch.setattr(mp.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        mp,
        "_make_adapter",
        lambda provider, config, resolve_credential: _FakeAdapter(result=_ok_result()),
    )

    conn = {"available_providers": ["gemini"]}
    with pytest.raises(mp.ProviderCascadeTimeoutError):
        mp.call_model(
            "quick", "sys", [{"role": "user", "content": "hi"}], conn, {}, user_id="u1", timeout=5.0
        )

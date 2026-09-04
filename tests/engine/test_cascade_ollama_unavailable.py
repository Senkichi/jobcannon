# PORTED from tests/test_cascade_ollama_unavailable.py @ b641e69122dfb1186787ed3fcd6013285ccc4f43 (private job-cannon). Ledger L-0531.
"""Tests for cascade fall-through when Ollama is marked unavailable at startup.

Covers:
- ProviderUnavailable is a RuntimeError subclass (caught by cascade tuples)
- Cascade with _jf_ollama_unavailable=True skips Ollama and reaches the next
  provider without invoking OllamaProvider.__init__ (mock spy)
"""

from __future__ import annotations

# PORT-SEAM: `from unittest.mock import MagicMock, patch` dropped -- only the
# exception-class tests below survive; see the trailing PORT-SEAM note for
# what does not and why.
import pytest

from jobcannon.engine.model_types import ProviderUnavailable

# ---------------------------------------------------------------------------
# ProviderUnavailable hierarchy
# ---------------------------------------------------------------------------


def test_provider_unavailable_is_runtime_error_subclass():
    """ProviderUnavailable must be caught by the existing (ValueError, RuntimeError, ImportError)
    catch tuples in model_provider.py at lines ~315 and ~693 — no tuple changes needed."""
    assert issubclass(ProviderUnavailable, RuntimeError)


def test_provider_unavailable_is_exception():
    """Sanity: can be raised and caught as a RuntimeError."""
    with pytest.raises(RuntimeError):
        raise ProviderUnavailable("ollama marked unavailable at startup")


def test_provider_unavailable_message_preserved():
    exc = ProviderUnavailable("ollama marked unavailable at startup")
    assert "ollama" in str(exc)


# PORT-SEAM: dropped from here to EOF --
# test_make_adapter_raises_provider_unavailable_when_flagged,
# test_make_adapter_calls_ollama_when_not_flagged,
# test_cascade_skips_ollama_reaches_next_provider (all patch
# job_finder.web.providers.ollama_provider.OllamaProvider /
# anthropic_provider.AnthropicProvider, neither of which exists on this
# host, and call the old `_make_adapter(name, config=...)` signature);
# test_cached_adapter_does_not_bypass_unavailable_flag,
# test_cached_adapter_invalidated_on_num_ctx_change (assert on the private
# _ADAPTER_CACHE / num_ctx invalidation behavior jobcannon.host.model_provider's
# `_make_adapter` (L-0036 ADAPT) does not have -- see that module's docstring,
# Modularity note item 1: a per-provider-name cache would hand tenant B's
# adapter, and the API key closed over inside it, to tenant B's call).

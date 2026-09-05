# PORTED from tests/test_ollama_probe.py @ 5ffb1c69846d244f977f17cb92318c3ee2c6c320 (private job-cannon). Ledger L-0604.
"""Tests for jobcannon.host._ollama's operator-endpoint liveness probe.

Covers:
- URL resolution precedence (JC_OLLAMA_URL env > config > default)
- AlreadyRunning state (model_present True/False)
- Stage-1b 500ms-backoff retry (two requests.get calls on first failure)
- Unreachable-after-retry -> Unavailable
- Schema mismatch -> Unavailable

# PORT-SEAM (ADAPT-with-drop, see jobcannon/host/_ollama.py module docstring):
# this probe is reduced to operator-endpoint-only. Binary discovery/spawn
# machinery (``spawn_ollama``, ``_find_ollama_binary``,
# ``register_owned_process``, the Win32 Job Object, the ``Installable``
# result type and its ``AlreadyRunning.spawned_by_us`` field) does not exist
# here -- a fresh install is never attempted, only an operator-supplied
# endpoint is probed. The env var is renamed ``JOB_CANNON_OLLAMA_URL`` ->
# ``JC_OLLAMA_URL``. Provider-wiring (``OllamaProvider._base_url``) is a
# separate, out-of-scope port unit per the module docstring.

No network / DB access.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

from jobcannon.host._ollama import (
    _DEFAULT_OLLAMA_URL,
    AlreadyRunning,
    Unavailable,
    probe_ollama,
    resolve_ollama_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_tags_response(model_names: list[str] | None = None) -> MagicMock:
    """Mock requests.get response for a healthy /api/tags endpoint."""
    models = [{"name": n, "model": n} for n in (model_names or [])]
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"models": models}
    return mock_resp


def _conn_error() -> Exception:
    return requests.ConnectionError("connection refused")


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


class TestResolveOllamaUrl:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("JC_OLLAMA_URL", "http://remote:11999")
        config = {"providers": {"ollama": {"base_url": "http://config:11434"}}}
        assert resolve_ollama_url(config) == "http://remote:11999"

    def test_env_var_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("JC_OLLAMA_URL", "http://remote:11999/")
        assert resolve_ollama_url({}) == "http://remote:11999"

    def test_config_beats_default(self, monkeypatch):
        monkeypatch.delenv("JC_OLLAMA_URL", raising=False)
        config = {"providers": {"ollama": {"base_url": "http://config:9999"}}}
        assert resolve_ollama_url(config) == "http://config:9999"

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("JC_OLLAMA_URL", raising=False)
        assert resolve_ollama_url({}) == _DEFAULT_OLLAMA_URL

    def test_empty_env_falls_through_to_config(self, monkeypatch):
        monkeypatch.setenv("JC_OLLAMA_URL", "  ")
        config = {"providers": {"ollama": {"base_url": "http://config:9999"}}}
        assert resolve_ollama_url(config) == "http://config:9999"


# ---------------------------------------------------------------------------
# Probe state: AlreadyRunning
# ---------------------------------------------------------------------------


def test_probe_already_running_model_present():
    target = "qwen2.5:14b"
    with patch(
        "jobcannon.host._ollama.requests.get",
        return_value=_ok_tags_response([target, "llama3:8b"]),
    ):
        state = probe_ollama(target, "http://localhost:11434")

    assert isinstance(state, AlreadyRunning)
    assert state.model_present is True


def test_probe_already_running_model_absent():
    with patch(
        "jobcannon.host._ollama.requests.get",
        return_value=_ok_tags_response(["llama3:8b"]),
    ):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, AlreadyRunning)
    assert state.model_present is False


# ---------------------------------------------------------------------------
# Stage-1b: 500ms backoff retry (two get() calls on connection failure)
# ---------------------------------------------------------------------------


def test_probe_retries_once_on_connection_failure():
    """First attempt raises, second succeeds — state should be AlreadyRunning."""
    call_count = 0

    def _flaky_get(url, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise requests.ConnectionError("first attempt")
        return _ok_tags_response(["qwen2.5:14b"])

    with (
        patch("jobcannon.host._ollama.requests.get", side_effect=_flaky_get),
        patch("jobcannon.host._ollama.time.sleep") as mock_sleep,
    ):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, AlreadyRunning)
    assert call_count == 2
    mock_sleep.assert_called_once_with(0.5)


def test_probe_two_failures_then_unavailable():
    """Both attempts fail -> Unavailable (no binary-discovery fallback here).

    Merged from the private suite's two separate "both attempts fail"
    variants (``test_probe_unavailable_not_installed`` and
    ``test_probe_two_failures_then_unavailable``) -- both asserted the same
    outcome via the same retry path, differing only in the now-dropped
    binary-discovery mocking (``OLLAMA_EXE``/``LOCALAPPDATA``/
    ``shutil.which``) one of them also exercised. With that machinery gone,
    the two variants are identical; keeping one preserves full coverage of
    the "connection never succeeds" path.
    """
    with (
        patch("jobcannon.host._ollama.requests.get", side_effect=_conn_error()),
        patch("jobcannon.host._ollama.time.sleep"),
    ):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Unavailable)


# ---------------------------------------------------------------------------
# Schema mismatch -> Unavailable
# ---------------------------------------------------------------------------


def test_schema_mismatch_returns_unavailable():
    """Port responds but /api/tags returns wrong shape -> Unavailable."""
    bad_resp = MagicMock()
    bad_resp.raise_for_status.return_value = None
    bad_resp.json.return_value = {"not_models": "garbage"}

    with patch("jobcannon.host._ollama.requests.get", return_value=bad_resp):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Unavailable)


def test_schema_mismatch_models_not_list():
    """models key present but not a list -> Unavailable."""
    bad_resp = MagicMock()
    bad_resp.raise_for_status.return_value = None
    bad_resp.json.return_value = {"models": "not-a-list"}

    with patch("jobcannon.host._ollama.requests.get", return_value=bad_resp):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Unavailable)


def test_schema_mismatch_not_a_dict():
    """Response is a list, not a dict -> Unavailable."""
    bad_resp = MagicMock()
    bad_resp.raise_for_status.return_value = None
    bad_resp.json.return_value = [{"models": []}]

    with patch("jobcannon.host._ollama.requests.get", return_value=bad_resp):
        state = probe_ollama("qwen2.5:14b", "http://localhost:11434")

    assert isinstance(state, Unavailable)

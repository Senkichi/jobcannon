"""Targeted tests for scripts/preseed_corpus.py's --verify retry/timeout
logic (#106).

--verify's whole point is telling "board URL is actually dead" apart from
"network had a slow moment." Two consecutive runs against the same 116-row
CSV from the same network produced non-overlapping Greenhouse failure sets
under the old hardcoded 2s timeout — proof it was flaking on latency, not
catching real dead boards. These tests exercise the retry-with-backoff
helper in isolation (mocked ``requests.get`` / ``time.sleep``, no network)
rather than driving the whole script as a subprocess, since the thing that
regressed is the retry/backoff decision logic itself.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "preseed_corpus.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("preseed_corpus", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pc = _load_module()


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self.text = text


def test_verify_timeout_bumped_into_5_to_8s_range():
    """Regression guard for the exact defect in #106: the hardcoded
    ``timeout=2`` that produced flaky false failures. Pin the constant into
    the runbook's suggested 5-8s band rather than asserting an exact value,
    so a future retune within the band doesn't need to touch this test."""
    assert 5 <= pc._VERIFY_TIMEOUT_S <= 8


def test_get_with_retries_recovers_from_transient_timeouts(monkeypatch):
    """Two transient timeouts followed by a real response must NOT surface
    as a failure — this is the exact false-negative #106 reports."""
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_get(url, timeout, allow_redirects):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.Timeout("simulated slow board")
        return _FakeResponse(status_code=200)

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(pc.time, "sleep", lambda s: sleeps.append(s))

    resp = pc._get_with_retries(
        "https://boards.greenhouse.io/example",
        timeout=pc._VERIFY_TIMEOUT_S,
        attempts=pc._VERIFY_MAX_ATTEMPTS,
        backoff_base=pc._VERIFY_BACKOFF_BASE_S,
    )

    assert resp.status_code == 200
    assert calls["n"] == 3
    # Backed off between attempts (didn't hammer the board back-to-back).
    assert len(sleeps) == 2
    assert all(s > 0 for s in sleeps)


def test_get_with_retries_raises_after_exhausting_attempts(monkeypatch):
    """A genuinely-dead board (every attempt times out) must still surface
    as a failure — retries must not paper over real unreachability."""
    calls = {"n": 0}

    def fake_get(url, timeout, allow_redirects):
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("simulated dead board")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)

    with pytest.raises(requests.exceptions.ConnectionError):
        pc._get_with_retries(
            "https://boards.greenhouse.io/nonexistent",
            timeout=pc._VERIFY_TIMEOUT_S,
            attempts=pc._VERIFY_MAX_ATTEMPTS,
            backoff_base=pc._VERIFY_BACKOFF_BASE_S,
        )

    assert calls["n"] == pc._VERIFY_MAX_ATTEMPTS


def test_get_with_retries_does_not_retry_non_transient_errors(monkeypatch):
    """Only timeout/connection failures are transient-and-worth-retrying;
    other request errors (e.g. too-many-redirects) should surface on the
    first attempt instead of burning the retry budget on a non-flaky error."""
    calls = {"n": 0}

    def fake_get(url, timeout, allow_redirects):
        calls["n"] += 1
        raise requests.exceptions.TooManyRedirects("simulated redirect loop")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)

    with pytest.raises(requests.exceptions.TooManyRedirects):
        pc._get_with_retries(
            "https://boards.greenhouse.io/example",
            timeout=pc._VERIFY_TIMEOUT_S,
            attempts=pc._VERIFY_MAX_ATTEMPTS,
            backoff_base=pc._VERIFY_BACKOFF_BASE_S,
        )

    assert calls["n"] == 1


def test_verify_recovers_row_from_transient_timeout_end_to_end(monkeypatch):
    """End-to-end through ``_verify``: a row whose first two GETs time out
    but whose third succeeds must be reported reachable (exit 0), not
    dropped into the unreachable bucket. Also asserts on the *actual*
    ``timeout`` value ``_verify`` passes down to ``requests.get`` — a
    regression that reintroduced ``timeout=2`` at the `_verify` call site
    (while leaving `_get_with_retries` and its own tests untouched) would
    silently defeat #106's fix without this check catching it."""
    calls = {"n": 0}
    seen_timeouts: list[float] = []

    def fake_get(url, timeout, allow_redirects):
        calls["n"] += 1
        seen_timeouts.append(timeout)
        if calls["n"] < 3:
            raise requests.exceptions.Timeout("simulated slow board")
        return _FakeResponse(status_code=200)

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)

    rows = [{"name": "Acme", "ats_platform": "greenhouse", "ats_slug": "acme"}]
    assert pc._verify(rows) == 0
    assert all(t >= 5 for t in seen_timeouts)

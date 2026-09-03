"""Tests for the hard-deadline HTTP wrapper (issue #39).

The load-bearing guarantee: a fetch whose underlying GET blocks longer than the
deadline returns control to the caller AT the deadline — it does not wait for
the slow GET to finish (which, for a real trickle host, is "never").
"""

import time

import pytest

from jobcannon.engine.http_fetch import FetchDeadlineError, fetch_with_deadline


class _Resp:
    def __init__(self, text="ok", status_code=200):
        self.text = text
        self.status_code = status_code


def test_returns_fast_response():
    resp = fetch_with_deadline(
        "http://x", total_deadline_s=5, getter=lambda url, **kw: _Resp("hi")
    )
    assert resp.text == "hi"
    assert resp.status_code == 200


def test_caller_unblocks_at_deadline_not_when_slow_get_finishes():
    """The wedge fix: a 2s-blocking GET must not hold the caller past ~0.3s."""

    def _slow(url, **kw):
        time.sleep(2.0)
        return _Resp()

    start = time.monotonic()
    with pytest.raises(FetchDeadlineError):
        fetch_with_deadline("http://slow", total_deadline_s=0.3, getter=_slow)
    elapsed = time.monotonic() - start
    # Returned at the deadline (~0.3s), NOT after the worker's 2s sleep.
    assert elapsed < 1.0, f"caller blocked {elapsed:.2f}s — deadline not enforced"


def test_kwargs_forwarded_to_getter():
    seen = {}

    def _capture(url, **kw):
        seen["url"] = url
        seen["kw"] = kw
        return _Resp()

    fetch_with_deadline(
        "http://x",
        total_deadline_s=5,
        getter=_capture,
        timeout=10,
        headers={"User-Agent": "ua"},
    )
    assert seen["url"] == "http://x"
    assert seen["kw"] == {"timeout": 10, "headers": {"User-Agent": "ua"}}


def test_getter_exception_propagates():
    def _boom(url, **kw):
        raise ValueError("network down")

    with pytest.raises(ValueError, match="network down"):
        fetch_with_deadline("http://x", total_deadline_s=5, getter=_boom)


@pytest.mark.parametrize("bad", [0, -1, None])
def test_rejects_nonpositive_deadline(bad):
    with pytest.raises(ValueError):
        fetch_with_deadline("http://x", total_deadline_s=bad, getter=lambda url, **kw: _Resp())

"""Tests for jobcannon.engine.inflight_guard (Ledger L-0187).

PORT NOTE: the private repo has no dedicated unit-test file for this module —
it is only exercised indirectly through Flask-route integration tests
(test_redispatch.py, test_resume_prepare_orchestrator.py,
test_scheduler_redispatch.py), which also drive request/DB/scheduler
machinery that is not part of this port. This file extracts the one
self-contained case those suites carry
(test_resume_prepare_orchestrator.py::TestInflightGuard::test_duplicate_request_rejected)
and adds a companion TTL-expiry test for the module's second behavior.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from jobcannon.engine.inflight_guard import _in_flight, _lock, release, try_acquire


def setup_function() -> None:
    with _lock:
        _in_flight.clear()


def test_duplicate_request_rejected() -> None:
    """Second acquire on the same (key, action) is rejected until released."""
    dedup_key = "test-job-6"

    assert try_acquire(dedup_key, "prepare_application") is True
    assert try_acquire(dedup_key, "prepare_application") is False

    release(dedup_key, "prepare_application")
    assert try_acquire(dedup_key, "prepare_application") is True

    release(dedup_key, "prepare_application")


def test_different_actions_do_not_collide() -> None:
    """The guard keys on (key, action) — same key, different action, no collision."""
    dedup_key = "test-job-7"

    assert try_acquire(dedup_key, "prepare_application") is True
    assert try_acquire(dedup_key, "rescore") is True

    release(dedup_key, "prepare_application")
    release(dedup_key, "rescore")


def test_release_never_acquired_is_safe() -> None:
    """release() on a (key, action) that was never claimed is a no-op, not an error."""
    release("never-claimed", "prepare_application")


def test_expired_claim_can_be_reacquired() -> None:
    """A claim past _TTL_SECONDS is reacquirable without an explicit release."""
    dedup_key = "test-job-8"
    now = time.monotonic()

    with patch("jobcannon.engine.inflight_guard.time.monotonic", return_value=now):
        assert try_acquire(dedup_key, "prepare_application") is True

    with patch(
        "jobcannon.engine.inflight_guard.time.monotonic",
        return_value=now + 241,  # past _TTL_SECONDS (240)
    ):
        assert try_acquire(dedup_key, "prepare_application") is True

    release(dedup_key, "prepare_application")

"""Tests for per-host request pacing in the shared ATS Session.

Every ats_platforms HTTP call routes through get_session() (guarded by
tests/test_ats_http_session_guard.py — no file calls bare requests.get/post),
so pacing is enforced once, centrally, in _PacedSession.send() rather than
per-platform or per-company-worker. send() (not request()) is the pacing
point because requests' redirect handling (resolve_redirects) re-enters via
Session.send directly for every hop, bypassing request()/get()/etc entirely
— see _PacedSession's docstring in _http_session.py. These tests exercise
the real Session / semaphore-gating code path with a stubbed
requests.Session.send so no network call happens.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch
from urllib.parse import urlparse

import requests

import jobcannon.engine.ats_platforms._http_session as http_session_mod


def _reset_session_state() -> None:
    """Drop the lazy Session singleton and any per-host semaphores between tests."""
    http_session_mod._session = None
    http_session_mod._host_semaphores.clear()


def _fake_response(request: requests.PreparedRequest, status_code: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = request.url
    response.request = request
    response.raw = None
    return response


def _recording_send(tracker: dict):
    def _fake_send(self, request, **kwargs):
        with tracker["lock"]:
            tracker["active"] += 1
            tracker["max_concurrent"] = max(tracker["max_concurrent"], tracker["active"])
        time.sleep(0.05)
        with tracker["lock"]:
            tracker["active"] -= 1
        return _fake_response(request, 200)

    return _fake_send


def test_host_pacing_bounds_concurrent_requests_to_same_host():
    """Recorded-concurrency-with-overlap: requests to ONE host never exceed
    HOST_PACING_LIMIT — not 1 (accidentally serial) and not more (bound ignored).
    """
    _reset_session_state()
    tracker = {"active": 0, "max_concurrent": 0, "lock": threading.Lock()}

    with patch.object(requests.Session, "send", _recording_send(tracker)):
        session = http_session_mod.get_session()
        threads = [
            threading.Thread(
                target=session.get,
                args=("https://boards-api.greenhouse.io/v1/boards/co/jobs",),
            )
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert tracker["max_concurrent"] == http_session_mod.HOST_PACING_LIMIT, (
        f"expected exactly {http_session_mod.HOST_PACING_LIMIT} concurrent requests to "
        f"the same host, got {tracker['max_concurrent']}"
    )
    _reset_session_state()


def test_host_pacing_is_per_host_not_global():
    """Two hosts gated independently: total in-flight can exceed one host's
    limit, proving the semaphore is keyed per-host rather than a single
    global gate (which would defeat unrelated platforms' throughput for no
    reason).
    """
    _reset_session_state()
    tracker = {"active": 0, "max_concurrent": 0, "lock": threading.Lock()}

    with patch.object(requests.Session, "send", _recording_send(tracker)):
        session = http_session_mod.get_session()
        urls = ["https://boards-api.greenhouse.io/v1/boards/co/jobs"] * 4 + [
            "https://api.lever.co/v0/postings/co"
        ] * 4
        threads = [threading.Thread(target=session.get, args=(url,)) for url in urls]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert tracker["max_concurrent"] > http_session_mod.HOST_PACING_LIMIT, (
        f"expected concurrent requests across two independently-gated hosts to exceed "
        f"a single host's pacing limit ({http_session_mod.HOST_PACING_LIMIT}), "
        f"got max_concurrent={tracker['max_concurrent']}"
    )
    _reset_session_state()


def test_host_pacing_redirect_hop_paces_target_host_independently():
    """Redirect hops route through Session.send (not Session.request/.get),
    bypassing any pacing override placed on request(). This proves the fix:
    (a) a cross-host redirect target IS paced — hostB's semaphore is
    requested — and (b) hostA's semaphore slot is fully released (not held
    across the hop) by the time hostB's dispatch happens, i.e. pacing is
    scoped per-hop, not held for the whole redirect chain.
    """
    _reset_session_state()

    host_a = "boards-api.greenhouse.io"
    host_b = "api.lever.co"
    requested_hosts: list[str] = []
    real_get_host_semaphore = http_session_mod._get_host_semaphore

    def _tracking_get_host_semaphore(host):
        requested_hosts.append(host)
        return real_get_host_semaphore(host)

    hostA_value_during_hostB_dispatch = {"value": None}

    def _fake_send(self, request, **kwargs):
        host = urlparse(request.url).netloc
        if host == host_b:
            # At the moment hostB's hop is dispatched, hostA's semaphore
            # must already be back at its full (uncontended) value — proof
            # its slot isn't held across this hop.
            hostA_sem = http_session_mod._get_host_semaphore(host_a)
            hostA_value_during_hostB_dispatch["value"] = hostA_sem._value
            return _fake_response(request, 200)

        response = _fake_response(request, 302)
        response.headers["location"] = f"https://{host_b}/redirected"
        return response

    with (
        patch.object(requests.Session, "send", _fake_send),
        patch.object(http_session_mod, "_get_host_semaphore", _tracking_get_host_semaphore),
    ):
        session = http_session_mod.get_session()
        response = session.get(f"https://{host_a}/jobs")

    assert response.status_code == 200
    assert response.url == f"https://{host_b}/redirected"
    assert host_a in requested_hosts, "origin host's semaphore was never requested"
    assert host_b in requested_hosts, (
        "redirect target's host semaphore was never requested — the redirect hop bypassed pacing"
    )
    assert hostA_value_during_hostB_dispatch["value"] == http_session_mod.HOST_PACING_LIMIT, (
        "hostA's semaphore slot was still held while hostB's redirect hop was "
        f"being dispatched (value={hostA_value_during_hostB_dispatch['value']}, "
        f"expected {http_session_mod.HOST_PACING_LIMIT})"
    )
    _reset_session_state()


def test_get_host_semaphore_reuses_same_object_per_host():
    """Same host always maps to the same cached semaphore; different hosts
    get independent semaphores (never a shared/global gate).
    """
    _reset_session_state()

    sem1 = http_session_mod._get_host_semaphore("boards-api.greenhouse.io")
    sem2 = http_session_mod._get_host_semaphore("boards-api.greenhouse.io")
    sem3 = http_session_mod._get_host_semaphore("api.lever.co")

    assert sem1 is sem2
    assert sem1 is not sem3

    _reset_session_state()

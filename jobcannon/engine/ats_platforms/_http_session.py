"""Shared requests.Session with pooled HTTP adapters for ATS platform scanners.

Every ATS platform HTTP call uses the same Session object with connection pooling,
avoiding repeated TCP+TLS handshakes in tight loops against the same host.
The Session is a lazy singleton, thread-safe for concurrent requests.

Per-host request pacing lives here (not in ats_scanner/_run.py):
every ``_platforms_*.py`` file routes its HTTP calls through ``get_session()``
(verified by ``tests/test_ats_http_session_guard.py`` — no file calls bare
``requests.get``/``.post``), so this is the single point where a host-based
gate can enforce the invariant for every platform uniformly, keyed on the
*actual* outgoing request's host rather than a hand-maintained
platform-name -> host table that would drift as platforms are added/renamed.
"""

from __future__ import annotations

import threading
from typing import Any, Final
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from jobcannon.engine.ats_platforms._concurrency import HOST_PACING_LIMIT

# Pool sizing for the HTTPAdapter. These are tuned for the coming worker pools
# — pool_connections is the number of connection pools to cache per host,
# pool_maxsize is the maximum number of connections to keep in the pool.
_POOL_CONNECTIONS: Final = 10
_POOL_MAXSIZE: Final = 16

_session: requests.Session | None = None
_session_lock = threading.Lock()

# Per-host pacing semaphores, keyed by the request URL's netloc.
# Lazily created on first request to a given host; shared across every caller
# of get_session() (Phase A's board-level worker pool, page-fetch pools,
# detail-fetch pools, the reconciler, ...) since they all share one Session.
_host_semaphores: dict[str, threading.Semaphore] = {}
_host_semaphores_lock = threading.Lock()


def _get_host_semaphore(host: str) -> threading.Semaphore:
    """Return the bounded semaphore that paces concurrent requests to ``host``.

    Creates one lazily (double-checked under a lock) the first time a given
    host is seen; reused for every subsequent request to that host.
    """
    semaphore = _host_semaphores.get(host)
    if semaphore is not None:
        return semaphore
    with _host_semaphores_lock:
        semaphore = _host_semaphores.get(host)
        if semaphore is None:
            semaphore = threading.Semaphore(HOST_PACING_LIMIT)
            _host_semaphores[host] = semaphore
        return semaphore


class _PacedSession(requests.Session):
    """``requests.Session`` that gates concurrent requests per destination host.

    Paces at ``send()``, not ``request()``. ``Session.get``/``.post``/etc. and
    ``Session.request`` all fetch through ``Session.send`` for the initial
    dispatch, but requests' redirect handling
    (``SessionRedirectMixin.resolve_redirects``) re-enters via ``self.send()``
    directly for every redirect hop — with ``allow_redirects=False`` — and
    never calls back through ``request()``/``get()``/etc. An override on
    ``request()`` would therefore (a) hold the *origin* host's semaphore for
    the whole redirect chain, since ``resolve_redirects`` runs nested inside
    that same ``request()`` call, and (b) never pace a cross-host redirect
    target at all, since a hop's dispatch lands directly in ``send()``.
    ``send()`` is the one choke point that sees both the initial dispatch and
    every hop, so it's the single point where a per-hop, per-host gate is
    correct.

    To key the semaphore per hop's *own* host without letting one semaphore
    hold span multiple hops, this dispatches exactly one hop under that hop's
    host semaphore (forcing ``allow_redirects=False`` on the inner call,
    mirroring what ``resolve_redirects`` itself already does for every
    non-initial hop) and then — only for the outermost caller, and only when
    redirects are allowed — drives ``resolve_redirects`` itself *outside* the
    semaphore. Each subsequent hop re-enters this same ``send`` override via
    ``self.send(...)`` and paces on its own host independently. In serial
    scans (the default, scan_concurrency=1) there is never more than one
    thread calling in, so every semaphore acquire/release is uncontended —
    byte-identical behavior to an unpaced Session.
    """

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        host = urlparse(request.url).netloc
        allow_redirects = kwargs.pop("allow_redirects", True)

        # Dispatch exactly this one hop, paced on its own host. Forcing
        # allow_redirects=False here (regardless of what the caller actually
        # wanted) is what keeps requests' own internal resolve_redirects loop
        # from running *inside* this semaphore hold — Session.send only runs
        # that loop when allow_redirects is truthy.
        with _get_host_semaphore(host):
            response = super().send(request, allow_redirects=False, **kwargs)

        if not allow_redirects:
            return response

        # Redirects were requested: replicate Session.send's own
        # redirect-following + history-stitching (see
        # requests.sessions.Session.send), but with resolve_redirects driven
        # here, outside any semaphore. Each hop it dispatches recurses through
        # self.send(..., allow_redirects=False), i.e. back into this method,
        # which acquires that hop's own host semaphore independently.
        gen = self.resolve_redirects(response, request, **kwargs)
        history = list(gen)

        if history:
            history.insert(0, response)
            response = history.pop()
            response.history = history

        return response


def get_session() -> requests.Session:
    """Return the shared requests.Session singleton with pooled adapters.

    The Session is created on first call and reused for all subsequent calls.
    HTTPAdapter with connection pooling is mounted for https:// to avoid
    repeated TCP+TLS handshakes. The returned Session also paces concurrent
    requests per destination host — see ``_PacedSession``.

    Returns:
        The shared requests.Session object.
    """
    global _session

    if _session is None:
        with _session_lock:
            # Double-check after acquiring lock
            if _session is None:
                _session = _PacedSession()
                adapter = HTTPAdapter(
                    pool_connections=_POOL_CONNECTIONS,
                    pool_maxsize=_POOL_MAXSIZE,
                )
                _session.mount("https://", adapter)

    return _session

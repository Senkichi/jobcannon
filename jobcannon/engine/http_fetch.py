"""Hard-deadline HTTP GET — bounds TOTAL wall-clock time, not just per-read.

``requests``' ``timeout=`` is a *per-read* (inter-byte) timeout, NOT a ceiling
on total request time: a server that trickles one byte every ``timeout``-minus-
epsilon seconds keeps the read alive indefinitely. This was observed wedging a
live ATS scan's Phase-C HTML fallback on a single slow careers host for 90+
minutes (issue #39) — the scan thread blocked forever inside ``requests.get``
while the rest of the app kept heartbeating, never writing the completion row.

``fetch_with_deadline`` runs the GET on a daemon worker thread and abandons it
once a hard wall-clock ``total_deadline_s`` elapses, so the CALLER is guaranteed
to return within the deadline. A genuinely-stuck worker leaks one daemon thread
(reaped at process exit) — a bounded, acceptable cost versus wedging the whole
scan. Always pass a ``timeout=(connect, read)`` (or scalar) in ``get_kwargs`` so
the common "stalled host" case is reaped by ``requests`` itself well before the
deadline fires; the deadline is the backstop for the adversarial trickle case.

The wrapper deliberately preserves ``requests.get``'s call shape and the
``Response`` object it returns, so existing call sites — and their
``patch("...requests.get")`` test seams with simple mock responses — keep
working unchanged. (Module-level ``patch`` targets the shared ``requests``
module, so it is still honoured here regardless of which module issues the GET.)
"""

from __future__ import annotations

import logging
import threading

import requests

logger = logging.getLogger(__name__)

# Default ceiling for a single careers/homepage HTML fetch (including redirect
# hops). Generous enough for a slow-but-real page, tight enough that no single
# host can stall a batch scan.
_DEFAULT_TOTAL_DEADLINE_S = 30.0


class FetchDeadlineError(requests.exceptions.Timeout):
    """A fetch exceeded its hard total wall-clock deadline and was abandoned.

    Subclasses ``requests.exceptions.Timeout`` so a deadline breach slots
    transparently into any existing ``except requests.exceptions.Timeout`` /
    ``except requests.exceptions.RequestException`` handler at a call site — a
    deadline breach is, semantically, a timeout. (Plain ``except Exception``
    handlers catch it too.)"""


def fetch_with_deadline(
    url: str,
    *,
    total_deadline_s: float = _DEFAULT_TOTAL_DEADLINE_S,
    getter=None,
    **get_kwargs,
) -> requests.Response:
    """``requests.get(url, **get_kwargs)`` with a HARD total wall-clock deadline.

    Returns the ``Response`` if it arrives within ``total_deadline_s``; raises
    ``FetchDeadlineError`` if the deadline elapses first (the worker thread is
    abandoned, not cancelled). Re-raises any exception the underlying getter
    raised, preserving the original ``requests.get`` failure contract.

    ``getter`` defaults to ``requests.get`` resolved *at call time*, so a
    ``patch("<module>.requests.get")`` test seam still intercepts. Pass an
    explicit ``getter`` only to unit-test this wrapper itself.
    """
    if total_deadline_s is None or total_deadline_s <= 0:
        raise ValueError("total_deadline_s must be a positive number of seconds")

    call = getter if getter is not None else requests.get
    box: dict = {}
    done = threading.Event()

    def _work() -> None:
        try:
            box["resp"] = call(url, **get_kwargs)
        except BaseException as exc:  # re-raised to the caller below
            box["exc"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_work, name="fetch-deadline", daemon=True)
    worker.start()

    if not done.wait(total_deadline_s):
        logger.warning(
            "fetch_with_deadline: abandoning GET after %.1fs hard deadline: %s",
            total_deadline_s,
            url,
        )
        raise FetchDeadlineError(f"total deadline {total_deadline_s}s exceeded for {url}")

    if "exc" in box:
        raise box["exc"]
    return box["resp"]

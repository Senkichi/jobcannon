"""Optional PostHog fan-out seam (1B Wave 2 PR 8).

Mirrors jobcannon/engine/extraction_health.py's set_recorder()/record() seam
shape: a module-level slot defaulting to None, set once at startup by the
host, and a documented no-op when unwired. Unlike extraction_health.record()
(which does NOT catch recorder exceptions — that recorder is trusted, in-
process code), capture() DOES catch and swallow every exception from the
PostHog client itself: PostHog is a third-party network call on the request
path and must never be able to turn an analytics hiccup into a 500.

Verified against posthog SDK 3.25.0 (pyproject pins posthog>=3.7,<4.0):
Posthog.capture(self, distinct_id=None, event=None, properties=None, ...) —
distinct_id/event/properties keyword names are stable across that range.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None


def set_posthog_client(client: Any) -> None:
    global _client
    _client = client


def capture(distinct_id: str, event: str, properties: dict) -> None:
    if _client is None:
        return  # documented no-op when unwired (mirrors extraction_health)
    try:
        _client.capture(distinct_id=distinct_id, event=event, properties=properties)
    except Exception:
        logger.warning("posthog capture failed for %s", event, exc_info=True)

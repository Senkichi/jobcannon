"""Optional PostHog fan-out seam (1B Wave 2 PR 8), plus the pseudonymization
that gates what identifier ever reaches it.

Two independent module-level slots, each mirroring
jobcannon/engine/extraction_health.py's set_recorder()/record() shape: a
slot defaulting to None, set once at startup by the host
(jobcannon.host.wiring.init_engine_seams), and a documented no-op/fail-closed
behavior when unwired.

`capture()` is a thin wrapper around the vendor SDK and DOES catch and
swallow every exception from the PostHog client itself: PostHog is a
third-party network call on the request path and must never be able to turn
an analytics hiccup into a 500. Verified against posthog SDK 3.25.0
(pyproject pins posthog>=3.7,<4.0): Posthog.capture(self, distinct_id=None,
event=None, properties=None, ...) — distinct_id/event/properties keyword
names are stable across that range.

`pseudonymize()` is the sole producer of the identifier that goes into
`capture()`'s `distinct_id`: a deterministic HMAC-SHA256 of the salt and the
Clerk user id, so the same user always maps to the same pseudonym (analytics
continuity) but the mapping cannot be reversed back to the user id without
the salt — unlike a bare hash of the id alone, which anyone who can guess or
brute-force the (small, structured) id space could reverse. The salt itself
is a dedicated secret (JC_ANALYTICS_PSEUDONYM_SALT), never the Flask session
key, so rotating one never rotates the other. `pseudonymize()` returns None
when no salt is configured — jobcannon.host.events.log_event, its only
caller, MUST treat None as "skip the PostHog fan-out for this event
entirely" and never fall back to sending the raw user id. That gating lives
in the caller rather than here: `capture()` receives an already-resolved
distinct_id and cannot tell a pseudonym from an anonymous session id, so it
is not the place that can enforce "never a raw user id."
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None
_salt: str | None = None


def set_posthog_client(client: Any) -> None:
    global _client
    _client = client


def set_analytics_salt(salt: str | None) -> None:
    """Wired by jobcannon.host.wiring.init_engine_seams from HostConfig.
    analytics_pseudonym_salt. A blank string normalizes to None, same
    fail-closed-on-blank semantics as load_host_config's own env reads."""
    global _salt
    _salt = salt or None


def pseudonymize(user_id: str) -> str | None:
    """Deterministic per-user analytics pseudonym, or None if no salt is
    configured (fail-closed — see module docstring)."""
    if _salt is None:
        return None
    return hmac.new(_salt.encode("utf-8"), user_id.encode("utf-8"), hashlib.sha256).hexdigest()


def capture(distinct_id: str, event: str, properties: dict) -> None:
    if _client is None:
        return  # documented no-op when unwired (mirrors extraction_health)
    try:
        _client.capture(distinct_id=distinct_id, event=event, properties=properties)
    except Exception:
        logger.warning("posthog capture failed for %s", event, exc_info=True)

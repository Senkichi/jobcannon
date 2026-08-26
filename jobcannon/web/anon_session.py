"""The one place a visitor identifier is minted or read.

`jobcannon/host/events.py`'s `_anon_id()` reads `g.anon_session_id` but
nothing has ever written it — every anonymous event today falls back to the
literal `"anonymous"`. This module is that writer: `ensure_session_ids()`
mints a stable per-visitor id pair into the signed Flask session cookie on
first contact and republishes them onto `g` on every request thereafter, and
`capture_attribution()` records where a first-time visitor came from, once,
so a later signup can be attributed.

Called from `jobcannon.web.create_app()`'s `before_request` hook, after the
auth gate resolves `g.consent_granted`, for both the public-path branch and
the authed branch — never from a route handler directly.

The session cookie is functional-only here: it carries ids, picker
selections (via `pending_picker`), and captured attribution. It carries no
consent choice and triggers no event write — consent has exactly one writer,
on an authenticated surface, and travels through the database, not the
cookie.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from flask import current_app, g, request, session

from jobcannon.db.events_schema import _MAX_STR

_CHANNEL_DISALLOWED = re.compile(r"[^a-z0-9_-]")
_CHANNEL_MAX_LEN = 32


def ensure_session_ids() -> None:
    """Mint `anon_session_id` / `feed_session_id` into the session once, then
    republish both onto `g` on every request (minted or not) so route and
    event code always finds them at the same two attributes."""
    if "anon_session_id" not in session:
        session["anon_session_id"] = f"anon_{uuid4().hex}"
        session["feed_session_id"] = uuid4().hex
    g.anon_session_id = session["anon_session_id"]
    g.feed_session_id = session["feed_session_id"]


def _normalized_channel() -> str:
    # `?ref=` takes precedence over `?utm_source=` when both are present —
    # neither is tested in combination today, but `ref` is the more specific
    # of the two conventions.
    raw = request.args.get("ref") or request.args.get("utm_source") or ""
    cleaned = _CHANNEL_DISALLOWED.sub("", raw.lower())[:_CHANNEL_MAX_LEN]
    return cleaned or "direct"


def _referrer_host() -> str:
    referrer = request.referrer
    if not referrer:
        return "unknown"
    try:
        hostname = urlsplit(referrer).hostname
    except ValueError:
        # A malformed Referer (e.g. an unparseable bracketed-IPv6 form) must
        # never turn an otherwise-successful request into a 500 — same
        # fail-closed stance as the rest of this module's request-time reads.
        return "unknown"
    # .hostname (not .netloc) deliberately excludes a port. Path and query are
    # never captured here — hostname only — since a referrer URL can carry
    # search terms, session tokens, or other identifying detail that has no
    # business landing in an attribution record. Bounded to the events
    # payload validator's string cap so the captured value is never already
    # too long to store by the time a later signup event carries it.
    return (hostname or "unknown")[:_MAX_STR]


def capture_attribution() -> None:
    """Record first-touch attribution once per session. Absent values are
    the literal "direct" / "unknown" so the shape is always total — never
    missing keys for a later `user_signed_up` payload to KeyError on."""
    if "attribution" in session:
        return
    session["attribution"] = {
        "channel": _normalized_channel(),
        "referrer_host": _referrer_host(),
        "wave": current_app.config["HOST_CONFIG"].signup_wave,
    }


def get_pending_picker() -> dict[str, Any] | None:
    return session.get("pending_picker")


def set_pending_picker(data: dict[str, Any]) -> None:
    session["pending_picker"] = data

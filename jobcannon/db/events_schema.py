"""Payload allowlist + validator for the events table (1B Wave 2 PR 8).

Free text is made structurally impossible, not discouraged: every event_type
declares its exact set of allowed payload keys, string values are capped at
_MAX_STR chars, and a handful of keys are further constrained to a fixed
enum. An unknown event_type, an unlisted key, an oversized string, or an
out-of-enum value all raise ValueError — log_event (jobcannon/host/events.py)
is the only caller and treats any ValueError as a programmer error, not a
recoverable condition.
"""

from __future__ import annotations

_ALLOWED_KEYS: dict[str, set[str]] = {
    "posting_impression": {"surface"},
    "posting_saved": set(),
    "posting_dismissed": set(),
    "posting_watchlist_added": set(),
    "posting_apply_clicked": {"apply_destination"},
    "user_signed_up": {"channel", "wave", "signup_method", "referrer_url"},
    "user_activated": set(),
    "user_exit_surveyed": {"exit_reason"},
    "consent_recorded": {"consent_type", "granted", "consent_version", "consented_at"},
}

# Event types that ARE themselves the durable record of something that
# already happened, independent of the decision/consent state they describe
# — must survive regardless of age or consent. Single source of truth for
# two independent consumers that would otherwise drift: jobcannon/host/
# events.py's log_event reads this to decide which types write to Postgres
# unconditionally (bypassing the per-request consent gate — see that
# module's `_FIRST_PARTY_ALWAYS`), and jobcannon/db/_events.py's
# delete_expired_events reads it to decide which types the retention reaper
# must never touch. `consent_recorded` is the audit trail of a consent
# decision (including a decline); `user_signed_up` is signup attribution
# that jobcannon/db/_events.py's has_signed_up_event durably keys off of to
# avoid re-emitting on a cleared cookie jar — reaping either would silently
# reopen the exact gap each mechanism exists to close.
DURABLE_EVENT_TYPES = frozenset({"consent_recorded", "user_signed_up"})

_ENUMS: dict[tuple[str, str], set[str]] = {
    ("user_exit_surveyed", "exit_reason"): {"hired", "gave-up", "still-searching"},
}

_MAX_STR = 200


def validate_payload(event_type: str, payload: dict | None) -> None:
    if event_type not in _ALLOWED_KEYS:
        raise ValueError(f"unknown event_type: {event_type!r}")
    payload = payload or {}
    allowed = _ALLOWED_KEYS[event_type]
    for key, val in payload.items():
        if key not in allowed:
            raise ValueError(f"illegal payload key {key!r} for {event_type!r}")
        if val is not None and not isinstance(val, (str, int, float, bool)):
            raise ValueError(
                f"payload value for {key!r} must be a scalar "
                f"(str/int/float/bool/None), got {type(val).__name__}"
            )
        if isinstance(val, str) and len(val) > _MAX_STR:
            raise ValueError(
                f"payload value for {key!r} exceeds {_MAX_STR} chars (free text forbidden)"
            )
        enum = _ENUMS.get((event_type, key))
        if enum is not None and val not in enum:
            raise ValueError(f"{key!r}={val!r} not in {sorted(enum)}")

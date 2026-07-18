"""log_event — the single server-side product-analytics chokepoint
(1B Wave 2 PR 8).

Writes to Postgres (source of truth, durable before this call returns) and
fans out to PostHog (best-effort, only attempted after the Postgres write
has committed). Gated by:
  1. events_schema.validate_payload — a PII-proof allowlist (ValueError on
     any unlisted key / oversized string / out-of-enum value).
  2. a per-request consent check — every event EXCEPT consent_recorded is
     dropped (no Postgres write, no PostHog fan-out) unless consent_granted
     is True. consent_recorded itself always writes to Postgres (it IS the
     audit trail of a consent decision, including a decline), but only fans
     out to PostHog when the decision was a grant.

Adapted from the PR8 brief's log_event(...) sketch, which assumed a Flask
per-request `g.db` connection. This codebase has no such seam — every other
Flask route (jobcannon/web/webhooks.py) and host recorder
(jobcannon/host/health_recorder.py) opens its OWN connection via
jobcannon.db.pool.connection_factory() rather than reading one off `g`, so
log_event follows that established pattern instead of introducing a new one.
The Postgres write happens inside a `with connection_factory() as conn:`
block that also runs pool.commit_unless_nested(conn.raw) before the block
exits — durability is guaranteed before posthog_client.capture() is ever
attempted, preserving the load-bearing write-then-fan-out ordering.

`consent_granted` defaults to the ambient per-request jobcannon.web
before_request-resolved g.consent_granted (jobcannon/web/__init__.py) when
called from inside a Flask request; callers outside a request context
(a future worker/background job) must pass consent_granted explicitly.
flask.has_app_context() guards both `g` reads here so a caller that forgets
to pass consent_granted outside a request degrades to the safe default
(False / anonymous) instead of raising RuntimeError.
"""

from __future__ import annotations

from flask import g, has_app_context

from jobcannon.db import _events, events_schema
from jobcannon.db.pool import commit_unless_nested, connection_factory
from jobcannon.host import posthog_client


def log_event(
    event_type: str,
    *,
    user_id: str | None,
    consent_granted: bool | None = None,
    posting_id: int | None = None,
    feed_position: int | None = None,
    ranker_version: str | None = None,
    feed_session_id: str | None = None,
    interleave_experiment_id: str | None = None,
    interleave_team: str | None = None,
    payload: dict | None = None,
    distinct_id: str | None = None,
) -> None:
    events_schema.validate_payload(event_type, payload)

    if consent_granted is None:
        consent_granted = _consent_from_context()

    # consent_recorded writes unconditionally (it IS the audit trail of a
    # consent decision, grant or decline); every other event type requires
    # consent to have already been granted.
    if event_type != "consent_recorded" and not consent_granted:
        return

    with connection_factory() as conn:
        _events.insert_event(
            conn.raw,
            event_type=event_type,
            user_id=user_id,
            posting_id=posting_id,
            feed_position=feed_position,
            ranker_version=ranker_version,
            feed_session_id=feed_session_id,
            interleave_experiment_id=interleave_experiment_id,
            interleave_team=interleave_team,
            payload=payload,
        )
        commit_unless_nested(conn.raw)

    if event_type == "consent_recorded" and not (payload or {}).get("granted"):
        return  # audit row written above, no PostHog fan-out for a decline

    posthog_client.capture(distinct_id or user_id or _anon_id(), event_type, dict(payload or {}))


def _consent_from_context() -> bool:
    if not has_app_context():
        return False
    return getattr(g, "consent_granted", False)


def _anon_id() -> str:
    if not has_app_context():
        return "anonymous"
    return getattr(g, "anon_session_id", "anonymous")

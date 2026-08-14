"""jobcannon/web/handoff.py — the one anon-to-authed handoff chokepoint.

`run_handoff_if_pending()` is called from `jobcannon.web.create_app()`'s
`before_request` hook, after the auth gate resolves an authed identity and
after `ensure_session_ids()` (jobcannon/web/anon_session.py) has populated
`g.feed_session_id` — the `user_signed_up` emission below reads that
attribute, so this module must never run before it.

This module runs two independently-gated phases on every authed request,
because they have different repeat semantics:

1. **The DB phase (re-key + delete)** runs whenever the session carries a
   pending anon `users`-row id (the `anon_<uuid4hex>` id minted by
   `POST /start`, jobcannon/web/onboarding.py — NOT the per-visitor
   `anon_session_id` cookie value, a different string with a different
   lifetime) — **regardless of whether an earlier request in this session
   already completed the handoff and recorded the signup.** Resubmitting the
   picker at `/start` after signing up is this product's only preference-edit
   gesture, and `/start` is a public path that cannot see the authed
   identity, so the resulting `pending_picker` must still be consumed on a
   later authed request even after the signup marker below is set. On one
   connection in one transaction: ensures the Clerk `users` row exists,
   re-keys the anon `profiles` row onto it (skipped if there is no pending
   picker, or if the picker's `anon_id` does not carry the anon-id prefix —
   `jobcannon.db._users.is_anon_id` guards both the re-key read and the
   delete so a corrupted or foreign id sitting in the session can never touch
   a non-anon row), and deletes the anon `users` row (cascading its
   now-copied profile away). This phase is safe to repeat: `ensure_user`
   upserts, `get_profile` on an already-deleted anon id returns `None` so the
   re-key is skipped, and deleting an already-gone row is a no-op.
2. **The signup emission + one-time `/consent` redirect** fire **at most
   once per browser session**, gated on the `_HANDOFF_DONE_KEY` session
   marker, independent of whether the DB phase above ran this request. After
   the DB phase's commit — because `jobcannon.host.events.log_event` opens
   its own pooled connection and cannot join that transaction — this module
   emits `user_signed_up` with the consent state read fresh from the
   database (never the ambient, request-start `g.consent_granted`, which is
   resolved before this handoff runs and would be stale for exactly this
   event). The handoff never writes consent itself: consent has exactly one
   writer, on an authenticated surface (jobcannon/web/consent.py), and this
   module only reads the stored value to pass explicitly to `log_event`.

**The anon-row deletion above is safe only because no pre-signup surface —
`/start`, `/preview`, `/demo` — ever calls `log_event`.** Every one of them
runs with `g.consent_granted` hardcoded False and emits nothing, so an anon
id never accumulates `events` rows that a later delete could orphan or
silently discard. If a pre-signup surface is ever instrumented, this module
must migrate the anon id's `events` rows onto the Clerk id instead of
relying on the anon `users` row simply disappearing.

Because `capture_attribution()` populates `session["attribution"]` on a
client's very first request regardless of path, this module's DB-touching
work is reachable from EVERY app that registers the `before_request` hook,
not only from routes that expect it. Both phases are therefore fail-closed,
but differently, and a failure in one never blocks the other on a later
request:
- A DB-phase failure (an unopened connection pool — the same
  TESTING-without-a-pool state `jobcannon.web._resolve_consent` and
  `jobcannon.web.pages._read_page_data` already guard against — or a genuine
  DB outage) aborts the whole request's handoff before any session state
  changes, so a later request retries whichever phase(s) were pending, from
  scratch.
- An emission failure (`log_event` raises — e.g. `validate_payload` rejects
  an oversized `wave` value) is caught separately, AFTER the DB phase has
  already committed: `pending_picker` has already been popped (the DB
  phase's write landed; there is nothing left to re-key), but
  `_HANDOFF_DONE_KEY` is deliberately left unset and `session["attribution"]`
  deliberately left in place, so a later authed request retries the
  emission alone. This is an at-least-once retry over an already-idempotent
  DB phase, not a dropped signup — and it never turns an unrelated
  authenticated request into a 500.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import g, redirect, session, url_for

from jobcannon.db import _events
from jobcannon.db._profiles import get_profile, upsert_profile
from jobcannon.db._users import delete_user, ensure_user, is_anon_id
from jobcannon.db.pool import connection_factory
from jobcannon.host.events import log_event
from jobcannon.web.anon_session import get_pending_picker

logger = logging.getLogger(__name__)

_HANDOFF_DONE_KEY = "handoff_done"


def _should_emit() -> bool:
    return not session.get(_HANDOFF_DONE_KEY) and session.get("attribution") is not None


def _pending() -> bool:
    return get_pending_picker() is not None or _should_emit()


def run_handoff_if_pending() -> Any:
    """Returns a redirect Response on the one-time post-handoff trip to
    /consent, else None. Callers (create_app's before_request hook) must
    return this value when non-None — Flask only short-circuits dispatch
    when a before_request function returns a non-None response."""
    if not _pending():
        return None

    clerk_id = g.clerk_user.user_id
    pending_picker = get_pending_picker()
    run_db_phase = pending_picker is not None
    should_emit = _should_emit()
    attribution = session.get("attribution") or {}
    consent_granted = None
    choice_made = None

    try:
        with connection_factory() as conn:
            with conn.raw.transaction():
                ensure_user(conn, clerk_id)
                if run_db_phase and pending_picker.get("anon_id"):
                    anon_id = pending_picker["anon_id"]
                    if is_anon_id(anon_id):
                        anon_profile = get_profile(conn, anon_id)
                        if anon_profile is not None:
                            upsert_profile(
                                conn,
                                clerk_id,
                                skills=anon_profile["skills"],
                                experience_summary=anon_profile["experience_summary"],
                                target_titles=anon_profile["target_titles"],
                                target_locations=anon_profile["target_locations"],
                                seniority_level=anon_profile["seniority_level"],
                                years_of_experience=anon_profile["years_of_experience"],
                            )
                        delete_user(conn, anon_id)
            # The `with conn.raw.transaction():` block above has already
            # committed (its __exit__ commits on normal exit, matching
            # jobcannon/web/onboarding.py's identical pattern for the same
            # reason: ensure_user/upsert_profile/delete_user each call
            # commit_unless_nested internally, and nesting them inside this
            # block makes those internal commits no-ops so the writes land
            # atomically). Read consent state on the SAME connection, right
            # after the commit, before it returns to the pool — but only
            # when the emission below will actually use it.
            if should_emit:
                consent_granted = _events.read_consent_state(conn.raw, clerk_id)
                choice_made = _events.read_consent_choice_made(conn.raw, clerk_id)
    except Exception:
        logger.warning(
            "handoff DB phase failed for user %s (will retry on a later request)",
            clerk_id,
            exc_info=True,
        )
        return None

    if run_db_phase:
        # The DB phase's write already landed above; there is nothing left
        # to re-key even if the emission below fails.
        session.pop("pending_picker", None)

    if not should_emit:
        return None

    # Emitted AFTER the handoff's own commit, on log_event's own connection
    # (host/events.py:log_event opens one; it cannot join the block above) —
    # a failed handoff must never leave a signup event for a signup that
    # did not happen. Guarded separately from the DB phase above: this call
    # runs on EVERY authed request until it succeeds once, so a failure here
    # (e.g. an oversized wave value rejected by events_schema.validate_payload)
    # must never propagate into before_request and 500 an unrelated request.
    try:
        log_event(
            "user_signed_up",
            user_id=clerk_id,
            consent_granted=consent_granted,  # read just now, never the stale ambient g
            feed_session_id=g.feed_session_id,
            payload={
                "channel": attribution.get("channel", "direct"),
                "wave": attribution.get("wave", "0"),
                "signup_method": "clerk",
                "referrer_url": attribution.get("referrer_host", "unknown"),
            },
        )
    except Exception:
        logger.warning(
            "user_signed_up emission failed for user %s (DB phase already "
            "committed; will retry emission on a later request)",
            clerk_id,
            exc_info=True,
        )
        return None

    session.pop("attribution", None)
    session[_HANDOFF_DONE_KEY] = True

    if not choice_made:
        return redirect(url_for("consent.get_consent"))
    return None

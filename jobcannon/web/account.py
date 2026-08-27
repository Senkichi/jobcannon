"""jobcannon/web/account.py — the self-service account-deletion trigger.
`GET /account/delete` renders a confirmation form; `POST /account/delete`,
once the form's confirmation control is present, calls Clerk's user-delete
management endpoint and ends the caller's local session.

Authed only — deliberately NOT added to `jobcannon.web.PUBLIC_PATHS`: a
signed-out visitor has no account to delete.

This route TRIGGERS deletion; it does not perform it. The existing
`user.deleted` Clerk webhook (jobcannon/web/webhooks.py) remains the sole
writer of the cascade (profiles/watchlists/pipeline_status/
byo_key_credentials/events, all `ON DELETE CASCADE` from `users`). Calling
`jobcannon.db._users.delete_user` from here too would race that webhook's
asynchronous delivery and risk a second DELETE landing on an already-gone
row — so this route's job stops at telling Clerk to delete the account and
clearing the local Flask session (`anon_session_id`, `feed_session_id`,
`handoff_done`, etc.); Clerk invalidates the account's own session(s) once
the account itself is gone.

Issue #159: Clerk's own session invalidation is not enough — `auth.py`
verifies the `__session` JWT locally (no network call), so a token minted
moments before this request stays independently valid until its own `exp`.
This route writes a `revoked_subjects` tombstone (`jobcannon.db.
_revoked_subjects.revoke_subject`) BEFORE calling Clerk's delete, not
after: the gate in `jobcannon/web/__init__.py` must be able to reject that
still-valid JWT on this user's very next request, and ordering the write
first closes that window from the instant this handler returns rather than
from whenever Clerk's async webhook eventually arrives. If the tombstone
write itself fails, this handler stops there (502, Clerk never called) —
proceeding to delete the account without a working revocation path would
reopen the exact window this route exists to close.

Reuses the Clerk SDK client `jobcannon.web.__init__.create_app` builds once
(`jobcannon.web.auth.build_clerk_client`) and shares with the JWT verifier,
via `current_app.config["CLERK_CLIENT"]` — this module never constructs a
second client.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, g, render_template, request, session

logger = logging.getLogger(__name__)

account_bp = Blueprint("account", __name__)

# The checkbox's submitted value when checked. Requiring an exact match (not
# just "was the 'confirm' key present at all") means a form replayed with a
# blank or mangled value is rejected the same as an unchecked box.
CONFIRM_VALUE = "delete-my-account"


@account_bp.get("/account/delete", strict_slashes=False)
def get_delete():
    return render_template("account_delete.html")


@account_bp.post("/account/delete", strict_slashes=False)
def post_delete():
    if request.form.get("confirm") != CONFIRM_VALUE:
        return (
            render_template(
                "account_delete.html",
                error="Check the confirmation box to delete your account.",
            ),
            400,
        )

    # Resolved outside the try below on purpose: a missing/misconfigured
    # client (CLERK_CLIENT unset) must fail loudly as an unhandled 500, not
    # get masked as the same friendly "try again" 502 a real Clerk-API
    # failure produces.
    clerk_users = current_app.config["CLERK_CLIENT"].users
    user_id = g.clerk_user.user_id

    try:
        _write_revocation_tombstone(user_id)
    except Exception:
        # Same fail-closed stance as the Clerk-call failure below, but
        # distinguished in the log line: this failure means the account is
        # NOT deleted AND the revocation gate has no row to check against,
        # so calling Clerk next would delete the account while leaving its
        # just-issued JWT valid for the rest of its lifetime — the window
        # issue #159 exists to close. Stop here instead.
        logger.exception(
            "revocation tombstone write failed for user %s (account NOT deleted)",
            user_id,
        )
        return (
            render_template(
                "account_delete.html",
                error="Something went wrong deleting your account. Try again in a moment.",
            ),
            502,
        )

    try:
        clerk_users.delete(user_id=user_id)
    except Exception:
        # Fail-closed, matching jobcannon/web/consent.py's and
        # jobcannon/web/handoff.py's stance on an external call failing
        # mid-request: log with the traceback, never destroy local session
        # state for a deletion that didn't actually happen.
        #
        # The tombstone written above stays in place regardless -- do NOT
        # "fix" this by rolling it back here. On a timeout (one of the
        # failures this branch catches) this handler cannot tell "the
        # delete failed" from "the delete succeeded and only the response
        # was lost" -- in the lost-response case the account IS gone, and
        # rolling back the tombstone would leave this session's still-valid
        # JWT usable until its own exp with no revocation in place at all,
        # reopening the exact window issue #159 exists to close, until
        # Clerk's async user.deleted webhook eventually arrives. Keeping
        # the tombstone is correct either way; the cost is that a still-
        # existing, never-actually-deleted account is locked out of the
        # authed surface until the tombstone expires. The only safe
        # recovery is the iat comparison in jobcannon/db/_revoked_subjects.
        # py's is_subject_revoked: a fresh relogin mints a JWT with an iat
        # after this tombstone's revoked_at, which passes the gate even
        # while the row is still within its TTL.
        logger.exception("Clerk account-delete call failed for user %s", user_id)
        return (
            render_template(
                "account_delete.html",
                error="Something went wrong deleting your account. Try again in a moment.",
            ),
            502,
        )

    session.clear()
    return render_template("account_deleted.html")


def _write_revocation_tombstone(user_id: str) -> None:
    """Commit a `revoked_subjects` row for `user_id` on its own pooled
    connection, synchronously, before the caller proceeds to Clerk. Split
    out so tests can monkeypatch `jobcannon.db._revoked_subjects.
    revoke_subject` (imported here, not aliased at module scope) to force
    the failure branch above without needing the pool itself to fail."""
    from jobcannon.db import _revoked_subjects
    from jobcannon.db.pool import connection_factory

    with connection_factory() as conn:
        _revoked_subjects.revoke_subject(conn, user_id)

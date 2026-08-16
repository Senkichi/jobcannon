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
        clerk_users.delete(user_id=user_id)
    except Exception:
        # Fail-closed, matching jobcannon/web/consent.py's and
        # jobcannon/web/handoff.py's stance on an external call failing
        # mid-request: log with the traceback, never destroy local session
        # state for a deletion that didn't actually happen.
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

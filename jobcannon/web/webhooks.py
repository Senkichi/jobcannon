"""Svix-verified Clerk webhook receiver (1B spec §2).

Contract facts (verified 2026-07-17): payload MUST be the raw request body
(request.get_data() before any JSON parsing) — re-serialized JSON is Svix's
documented #1 verification-failure cause. Deliveries are at-least-once, so
user.created/user.updated is an UPSERT on the Clerk user id. The primary
email is data.email_addresses[] matched by data.primary_email_address_id —
there is no flat data.email. user.deleted carries only data.id.

user.deleted -> hard DELETE cascades to profiles/feed_state/watchlists/
pipeline_status/byo_key_credentials AND events (m0001 FKs, all ON DELETE
CASCADE) — spec consequence C-1 ("deletion must erase per-user raw events")
is implemented STRUCTURALLY at the FK layer rather than by a future runbook;
anonymous (user_id IS NULL) events are unaffected. Do not weaken the events
FK to SET NULL — that would re-open C-1.

user.deleted ALSO writes a `revoked_subjects` tombstone (issue #159) on the
SAME connection as `delete_user`, before it, in the same `with
connection_factory()` block — this is the second of the two revocation
writers (`jobcannon/web/account.py::post_delete` is the first, for
in-app-triggered deletions; this branch is what covers a deletion started
from Clerk's own Account Portal, which never touches account.py at all).
No try/except around either call: both are left to propagate to Flask's
default 500 on failure, matching this handler's existing bare-call
convention — Svix retries on a 5xx, and `revoke_subject` is an idempotent
upsert (jobcannon/db/_revoked_subjects.py), so a retried delivery re-writes
the same tombstone rather than erroring.
"""

from __future__ import annotations

from flask import Blueprint, current_app, request
from svix.webhooks import Webhook, WebhookVerificationError

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


def _primary_email(data: dict) -> str | None:
    primary_id = data.get("primary_email_address_id")
    for entry in data.get("email_addresses") or []:
        if entry.get("id") == primary_id:
            return entry.get("email_address")
    return None


@webhooks_bp.post("/clerk")
def clerk_webhook():
    payload = request.get_data()
    try:
        event = Webhook(current_app.config["WEBHOOK_SECRET"]).verify(payload, request.headers)
    except (WebhookVerificationError, ValueError, RuntimeError):
        # ValueError covers the underlying standardwebhooks/svix reference
        # implementation's un-wrapped failures on attacker-controlled input:
        # base64.b64decode() raises binascii.Error (a ValueError subclass)
        # on a malformed (non-base64) svix-signature value instead of
        # raising WebhookVerificationError, and a syntactically-valid
        # signature over a non-JSON body surfaces as json.JSONDecodeError
        # (also a ValueError subclass) from verify()'s own json.loads(data)
        # call — both are untrusted-input failures that must map to 400,
        # never bubble up as an unhandled 500. RuntimeError covers the other
        # untrusted-config edge: Webhook(secret) itself raises a bare
        # RuntimeError("Secret can't be empty.") when WEBHOOK_SECRET resolves
        # to "" (e.g. a TESTING config that injects a blank secret) — that
        # construction happens on this same line, inside this try, so it
        # must degrade to 400 too rather than an unhandled 500. Verified
        # empirically against svix 1.98.0 / standardwebhooks 1.0.1
        # 2026-07-17.
        return ("", 400)

    event_type = event.get("type")
    data = event.get("data") or {}
    user_id = data.get("id")
    if not user_id:
        return ("", 400)

    from jobcannon.db import _revoked_subjects, _users
    from jobcannon.db.pool import connection_factory

    if event_type in ("user.created", "user.updated"):
        with connection_factory() as conn:
            _users.ensure_user(conn, user_id, email=_primary_email(data))
    elif event_type == "user.deleted":
        with connection_factory() as conn:
            _revoked_subjects.revoke_subject(conn, user_id)
            _users.delete_user(conn, user_id)
    # Unknown event types: acknowledged 200 so Clerk doesn't retry forever.
    return ("", 200)

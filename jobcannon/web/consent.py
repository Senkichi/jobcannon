"""jobcannon/web/consent.py — the one consent-collection surface in the
product: `GET /consent` renders the current choice, `POST /consent` records
a grant or decline.

Authed only — deliberately NOT added to `jobcannon.web.PUBLIC_PATHS`: an
anonymous visitor has no identity to attach a consent decision to. Reachable
two ways: the one-time redirect `jobcannon.web.handoff.run_handoff_if_pending`
issues right after a signup with no prior choice, and the persistent link in
`base.html`.

`record_consent` (jobcannon/db/_events.py) remains the single writer of
`users.analytics_consent`; this route's POST handler is its only caller.
Do NOT also call `jobcannon.host.events.log_event("consent_recorded", ...)`
here — `record_consent` already inserts the audit event itself, so calling
both would write two rows for one decision.

`CONSENT_TYPE` / `CONSENT_VERSION` match the literals
tests/host/test_events.py already exercises against `record_consent`, so
this route's writes describe the same vocabulary the existing tests pin.

Ships the mechanism only: no policy copy, no legal text, no retention
statement, and no preferences dashboard beyond this single grant/decline
surface — a repeat visit simply records a new decision.
"""

from __future__ import annotations

import logging

from flask import Blueprint, g, redirect, render_template, request, url_for

from jobcannon.db import _events
from jobcannon.db.pool import commit_unless_nested, connection_factory

logger = logging.getLogger(__name__)

consent_bp = Blueprint("consent", __name__)

CONSENT_TYPE = "analytics"
CONSENT_VERSION = "v1"

_VALID_CHOICES = frozenset({"grant", "decline"})


def _read_consent_context() -> dict:
    """Fail-closed read, mirroring jobcannon/web/pages.py's _read_page_data
    shape: an unopened connection pool or a genuine DB outage degrades this
    page render to "no consent, never chosen" rather than a 500."""
    user_id = g.clerk_user.user_id
    try:
        with connection_factory() as conn:
            return {
                "consent_granted": _events.read_consent_state(
                    conn.raw, user_id, current_version=CONSENT_VERSION
                ),
                "choice_made": _events.read_consent_choice_made(
                    conn.raw, user_id, current_version=CONSENT_VERSION
                ),
            }
    except Exception:
        logger.warning(
            "consent state read failed for user %s (defaulting to no consent)",
            user_id,
            exc_info=True,
        )
        return {"consent_granted": False, "choice_made": False}


@consent_bp.get("/consent", strict_slashes=False)
def get_consent():
    return render_template("consent.html", **_read_consent_context())


@consent_bp.post("/consent", strict_slashes=False)
def post_consent():
    choice = request.form.get("choice")
    if choice not in _VALID_CHOICES:
        context = _read_consent_context()
        context["error"] = f"unrecognized choice: {choice!r}"
        return render_template("consent.html", **context), 400

    granted = choice == "grant"
    with connection_factory() as conn:
        consented_at = _events.db_now_iso(conn)
        _events.record_consent(
            conn,
            user_id=g.clerk_user.user_id,
            consent_type=CONSENT_TYPE,
            granted=granted,
            consent_version=CONSENT_VERSION,
            consented_at=consented_at,
        )
        commit_unless_nested(conn.raw)

    return redirect(url_for("pages.feed"))

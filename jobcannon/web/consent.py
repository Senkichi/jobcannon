"""jobcannon/web/consent.py — the one consent-collection surface in the
product: `GET /consent` renders the current choice, `POST /consent` records
a grant or decline.

`POST /consent` stays authed only — an anonymous visitor has no identity to
attach a consent decision to. `GET /consent` is marked `@public_get` (issue
#171): the footer's "Analytics preferences" link is rendered on every page
regardless of auth state, so a signed-out visitor who clicks it gets a
signed-out explanation (consent_signed_out.html) instead of the generic
401 gate. This is a per-view, GET-only opt-out, NOT a `PUBLIC_PATHS` entry —
`PUBLIC_PATHS` would also exempt the POST mutation (wrong: consent is an
account-level, authed-only decision) and skip clerk-js loading entirely
(wrong: a signed-in visitor hitting this same view still needs the header
nav's authed state). Reachable two ways beyond the footer link: the
one-time redirect `jobcannon.web.handoff.run_handoff_if_pending` issues
right after a signup with no prior choice, and the persistent link in
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
surface — a repeat visit simply records a new decision. consent.html
links out to /privacy (jobcannon.web.legal, issue #94) for the ratified
policy text; this route still carries no legal text of its own.

`post_consent` no longer redirects to the FEED on success (issue #182: the
previous 302 there silently discarded the grant/decline ack -- the only way
to see "Current choice: allowed/declined" was to navigate back to
/consent). An htmx-driven grant/decline (the norm -- the only form this
route ever renders posts via hx-post) re-renders the SAME panel in place
with `confirmed=True`, via `_consent_response`, showing the transient
"Analytics enabled/disabled." banner immediately, no round trip.

A plain/no-JS POST instead 303s back to GET /consent (Post/Redirect/Get) --
NOT the pre-#182 302-to-feed regression, since GET /consent always renders
"Current choice: allowed/declined." regardless of the transient banner, so
the ack is still visible after the redirect, never silently discarded. This
differs from the HX branch only in choosing PRG over an in-place re-render:
re-rendering the full page directly (matching the HX branch) would leave a
no-JS refresh/back resubmitting the POST -- a real, if minor, gap the HX
branch doesn't have (an htmx POST is never subject to a browser's native
refresh/back-resubmit prompt).
"""

from __future__ import annotations

import logging

from flask import Blueprint, g, redirect, render_template, request, url_for

from jobcannon.db import _events
from jobcannon.db.pool import commit_unless_nested, connection_factory
from jobcannon.web import public_get

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
@public_get
def get_consent():
    # A signed-out visitor has no consent state to read -- render the
    # explanatory signed-out variant instead of touching the DB at all.
    # A signed-in visitor (clerk_auth still resolves identity when it's
    # present, even on a public_get-marked view) gets the normal page.
    if g.clerk_user is None:
        return render_template("consent_signed_out.html")
    return render_template("consent.html", **_read_consent_context())


def _consent_response(context: dict, status: int):
    """Fragment-route convention (CLAUDE.md): HX-Request gets just the
    swappable panel; a direct/no-JS request gets the full page, same
    template context either way -- the panel partial is `{% include %}`d
    by consent.html, so there is exactly one place that decides what the
    confirmed/error/choice-made states look like.

    Used for BOTH branches of the 400 (unrecognized choice) response --
    nothing was written, so there's no PRG concern -- but only the HX
    branch of a successful post_consent: the non-HX success path 303s
    instead (see the module docstring), so this never renders a
    `confirmed=True` FULL page itself."""
    template = (
        "_consent_panel.html"
        if (request.headers.get("HX-Request") or "").lower() == "true"
        else "consent.html"
    )
    return render_template(template, **context), status


@consent_bp.post("/consent", strict_slashes=False)
def post_consent():
    choice = request.form.get("choice")
    if choice not in _VALID_CHOICES:
        context = _read_consent_context()
        context["error"] = f"unrecognized choice: {choice!r}"
        return _consent_response(context, 400)

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

    if (request.headers.get("HX-Request") or "").lower() != "true":
        # Post/Redirect/Get for the non-HX (no-JS) path only -- see the
        # module docstring. The HX branch below is unaffected: an htmx POST
        # is never subject to a browser refresh/back resubmit prompt, so it
        # keeps the richer in-place "Analytics enabled/disabled." banner.
        return redirect(url_for("consent.get_consent"), code=303)

    # Carry the choice just written rather than re-reading it back from the
    # DB via _read_consent_context(): that helper's fail-closed except
    # branch defaults to consent_granted=False, so a DB hiccup in the
    # narrow window right after this commit could render "Analytics
    # disabled." after a successful grant. record_consent() above already
    # raises on failure (never silently no-ops), so `granted` is ground
    # truth and choice_made is trivially True -- a decision was JUST made.
    context = {"consent_granted": granted, "choice_made": True, "confirmed": True}
    return _consent_response(context, 200)

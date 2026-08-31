"""GET/POST /profile — the profile editor (Spec 2, resolving #262's "how does
the user see their profile?").

Editor-first (decision 1): the page IS the edit form, with a compact stats
strip (Saved / Applied / Dismissed, decision 4) above it. NOT in PUBLIC_PATHS,
so jobcannon/web/__init__.py's before_request gate guarantees g.clerk_user and
`g.clerk_user.user_id` IS `profiles.user_id` — direct key, no lookup (the
postings_history.py / consent.py precedent).

Write path (decision 5, plan Deviation 1): the complete snapshot goes through
`replace_profile`, a plain overwrite, so a blanked field stays blank. This
module is the clerk profile domain's ONLY writer — /start 303s a signed-in
visitor here before it can write (decision 2).

Form contract (the `start_submit` shape): a validation failure re-renders at
200 with every submitted value echoed back, never a 4xx or a redirect; success
is PRG — 303 back to GET /profile?saved=1, which renders the confirmation
(the /consent pattern). Plain form POST, no htmx. CSRFProtect is app-wide;
the template carries csrf_token().

Reads fail CLOSED (pages.py's `_read_page_data` posture, for a stronger
reason here): a blank form rendered over a failed read would invite the
visitor to "save" an empty snapshot on top of a profile that exists. So a
read failure renders an unavailable notice and no form at all. Writes are
NOT caught — a failed write must surface as the 500 error page, never as a
redirect that looks like success.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, g, redirect, render_template, request, url_for

from jobcannon.db._profiles import get_profile, replace_profile
from jobcannon.db._user_actions import count_pipeline_statuses, count_saved_postings
from jobcannon.db.pool import connection_factory
from jobcannon.web.onboarding import SENIORITY_LEVELS, SKILLS_OPTIONS
from jobcannon.web.postings_history import _VIEWS
from jobcannon.web.profile_form import (
    WORKPLACE_FORM_OPTIONS,
    echo_form_values,
    parse_profile_form,
    profile_form_values,
)

logger = logging.getLogger(__name__)

profile_bp = Blueprint("profile", __name__)


def _read_page_data(user_id: str) -> tuple[Any, dict[str, int], bool]:
    """(row, counts, ok). `counts` is keyed by the postings-history view
    tokens: "saved" from watchlists plus every pipeline status. On any
    failure `ok` is False and the caller renders the unavailable branch."""
    try:
        with connection_factory() as conn:
            row = get_profile(conn, user_id)
            counts = {
                "saved": count_saved_postings(conn, user_id),
                **count_pipeline_statuses(conn, user_id),
            }
            return row, counts, True
    except Exception:
        logger.warning(
            "profile page read failed for user %s (rendering unavailable)",
            user_id,
            exc_info=True,
        )
        return None, {}, False


def _stats(counts: dict[str, int]) -> list[dict[str, Any]] | None:
    """Strip cells in postings-history tab order, each linking to that view.
    Iterates `postings_history._VIEWS` so a new view token there shows up
    here without a second hand-maintained list; a count of 0 renders "0"
    (spec §3), never hides the cell. None when there are no counts at all
    (a failed read) — the template hides the strip rather than lying with
    zeros."""
    if not counts:
        return None
    return [
        {
            "view": view,
            "count": counts.get(view, 0),
            "href": url_for("postings_history.index", view=view),
        }
        for view in _VIEWS
    ]


def _render(
    *,
    values: dict[str, Any],
    counts: dict[str, int],
    error: str | None = None,
    saved: bool = False,
    unavailable: bool = False,
) -> str:
    return render_template(
        "profile.html",
        values=values,
        stats=_stats(counts),
        error=error,
        saved=saved,
        unavailable=unavailable,
        skills=SKILLS_OPTIONS,
        seniority_levels=SENIORITY_LEVELS,
        workplace_options=WORKPLACE_FORM_OPTIONS,
    )


@profile_bp.get("/profile", strict_slashes=False)
def edit():
    user_id = g.clerk_user.user_id
    row, counts, ok = _read_page_data(user_id)
    if not ok:
        return _render(values=profile_form_values(None), counts=counts, unavailable=True)
    return _render(
        values=profile_form_values(row),
        counts=counts,
        saved=request.args.get("saved") == "1",
    )


@profile_bp.post("/profile", strict_slashes=False)
def submit():
    user_id = g.clerk_user.user_id
    snapshot, error = parse_profile_form(request.form)
    if error is not None:
        # Echo the submission, not the stored row (the visitor's typing is
        # what they need to fix); the strip still reads live counts so the
        # error page is the same page, minus nothing.
        _, counts, _ok = _read_page_data(user_id)
        return _render(values=echo_form_values(request.form), counts=counts, error=error)
    with connection_factory() as conn:
        replace_profile(conn, user_id, **snapshot)
    return redirect(url_for("profile.edit", saved=1), code=303)

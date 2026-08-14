"""actions_bp — POST /postings/<id>/save, /dismiss, /apply: the authed-only
mutation surface for a posting's per-user watchlist/pipeline state.

Not listed in `jobcannon.web.PUBLIC_PATHS`, so the `before_request` gate in
jobcannon/web/__init__.py already 401s an unauthenticated request to any
route this blueprint owns — no separate auth check is needed here.

Each route performs its `jobcannon.db._user_actions` mutation, logs the
matching allowlisted event through `jobcannon.host.events.log_event`, and
re-renders `jobcannon/web/templates/_posting_row.html` for the caller's HTMX
outerHTML swap — always `200` (never `204`: HTMX requires `200` for an
outerHTML swap, matching the rest of this codebase's fragment-route
convention). Dismiss is the one route whose "re-rendered fragment" is an
empty body: `_fetch_entry` below re-runs the exact same query
`jobcannon.db._feed.list_feed_postings` uses for a plain page render, which
already excludes a `pipeline_status.status = 'dismissed'` row — so a
dismissed posting's disappearance from the DOM falls out of the one query
both places share, rather than a second, independently-maintained
"is this row still visible" rule living here.

A `posting_id` that does not exist is a `404`, not a `500`:
`jobcannon.db._user_actions`'s writes carry a `REFERENCES postings(id)`
foreign key with no existence pre-check, so a nonexistent id raises
`psycopg.errors.ForeignKeyViolation` from inside the `with connection_factory()`
block — caught here and turned into `abort(404)`. Letting the `with` block's
`__exit__` run (rather than catching around a bare `conn.raw.execute`) is
what returns the aborted connection to the pool correctly; pooled connections
already roll back on an exception leaving their context (psycopg_pool), so no
manual rollback is needed here.

Apply's `apply_destination` (the event payload's platform token / destination
hostname, never a full URL) comes from `jobcannon.web.apply_url`, computed
from the SAME row `_fetch_entry` just read — not a second query. When a
posting has no usable URL, `apply_destination_for_row` returns `None` and
this route skips the `log_event` call entirely rather than writing an event
whose one allowed payload key would otherwise have to carry a `None`: the
mutation still lands (a user may have applied elsewhere and this row's URL
extraction just came up empty), but no `posting_apply_clicked` row is
written for a destination that was never actually known.
"""

from __future__ import annotations

import logging

import psycopg
from flask import Blueprint, abort, g, render_template

from jobcannon.db._feed import list_feed_postings
from jobcannon.db._user_actions import dismiss_posting, mark_applied, save_posting
from jobcannon.db.pool import connection_factory
from jobcannon.host.events import log_event
from jobcannon.web.apply_url import apply_destination_for_row
from jobcannon.web.feed_entries import build_entry

logger = logging.getLogger(__name__)

actions_bp = Blueprint("actions", __name__)


def _fetch_entry(conn, user_id: str, posting_id: int) -> dict | None:
    """The one row `list_feed_postings` would render for this user today,
    narrowed to a single posting id. Returns None both when the posting does
    not exist and when it does but is excluded (dismissed) — routes that
    need to 404 on the former rely on the ForeignKeyViolation the write
    itself raises, not on this function's return value."""
    rows = list_feed_postings(conn, user_id=user_id, posting_id=posting_id, limit=1)
    return build_entry(rows[0], None) if rows else None


def _row_response(entry: dict | None):
    if entry is None:
        return "", 200
    return render_template("_posting_row.html", entry=entry, show_actions=True), 200


@actions_bp.post("/postings/<int:posting_id>/save")
def save(posting_id: int):
    user_id = g.clerk_user.user_id
    try:
        with connection_factory() as conn:
            save_posting(conn, user_id, posting_id)
            entry = _fetch_entry(conn, user_id, posting_id)
    except psycopg.errors.ForeignKeyViolation:
        abort(404)
    log_event("posting_saved", user_id=user_id, posting_id=posting_id)
    return _row_response(entry)


@actions_bp.post("/postings/<int:posting_id>/dismiss")
def dismiss(posting_id: int):
    user_id = g.clerk_user.user_id
    try:
        with connection_factory() as conn:
            dismiss_posting(conn, user_id, posting_id)
            entry = _fetch_entry(conn, user_id, posting_id)
    except psycopg.errors.ForeignKeyViolation:
        abort(404)
    log_event("posting_dismissed", user_id=user_id, posting_id=posting_id)
    return _row_response(entry)


@actions_bp.post("/postings/<int:posting_id>/apply")
def apply(posting_id: int):
    user_id = g.clerk_user.user_id
    try:
        with connection_factory() as conn:
            mark_applied(conn, user_id, posting_id)
            entry = _fetch_entry(conn, user_id, posting_id)
    except psycopg.errors.ForeignKeyViolation:
        abort(404)
    destination = apply_destination_for_row(entry["row"]) if entry is not None else None
    if destination is not None:
        log_event(
            "posting_apply_clicked",
            user_id=user_id,
            posting_id=posting_id,
            payload={"apply_destination": destination},
        )
    return _row_response(entry)

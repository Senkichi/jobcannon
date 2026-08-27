"""actions_bp — POST /postings/<id>/save, /dismiss, /apply, /undo-apply: the
authed-only mutation surface for a posting's per-user watchlist/pipeline
state.

Not listed in `jobcannon.web.PUBLIC_PATHS`, so the `before_request` gate in
jobcannon/web/__init__.py already 401s an unauthenticated request to any
route this blueprint owns — no separate auth check is needed here.

Each route performs its `jobcannon.db._user_actions` mutation, logs the
matching allowlisted event through `jobcannon.host.events.log_event`, and
re-renders `jobcannon/web/templates/_posting_row.html`, always returning
`200` (never `204`: HTMX requires `200` for an outerHTML swap, matching the
rest of this codebase's fragment-route convention). Save, dismiss, and
undo-apply (#177) are driven by an `hx-post` + `hx-swap="outerHTML"` on the
row's own control, so that fragment IS what replaces the row in the DOM.
Apply is invoked from a plain `fetch()` instead (see `_posting_row.html`'s
apply markup for why — an `<a href>` and an htmx AJAX trigger cannot coexist
on one click); its own success handler applies the SAME fragment as a manual
outerHTML swap (via `htmx.process` so the swapped-in row's own hx-post
controls, including Undo, are live) rather than leaving the click's outbound
navigation as the only observable effect. Dismiss is the one
route whose "re-rendered fragment" is an empty body: `_fetch_entry` below,
called WITHOUT `include_dismissed` (the only one of the four call sites that
doesn't — see `_fetch_entry`'s own docstring for why), re-runs the same
dismissed-excluding query `jobcannon.db._feed.list_feed_postings` uses for a
plain page render — so a dismissed posting's disappearance from the DOM
falls out of that shared query, rather than a second, independently-
maintained "is this row still visible" rule living here. Save, apply, and
undo-apply (#200) instead pass `include_dismissed=True`, so a mutation that
happens to act on a posting some EARLIER, unrelated action already
dismissed still renders that row's real current state rather than an empty
body indistinguishable from failure.

A `posting_id` that does not exist is a `404`, not a `500`:
`jobcannon.db._user_actions`'s save/dismiss/apply writes carry a
`REFERENCES postings(id)` foreign key with no existence pre-check, so a
nonexistent id raises `psycopg.errors.ForeignKeyViolation` from inside the
`with connection_factory()` block — caught here and turned into
`abort(404)`. Letting the `with` block's `__exit__` run (rather than
catching around a bare `conn.raw.execute`) is what returns the aborted
connection to the pool correctly; pooled connections already roll back on an
exception leaving their context (psycopg_pool), so no manual rollback is
needed here.

`undo_apply` is the one exception to that mechanism: its write is a bare
DELETE (`unmark_applied`), which never violates a foreign key regardless of
whether `posting_id` exists — there is nothing for `undo_apply` to catch.
Rather than adding an unconditional existence pre-check (which the design
above deliberately avoids for the INSERT/UPDATE routes), `unmark_applied`
itself returns whether the posting exists, computed with a second `SELECT`
only on the ambiguous path where its DELETE matched zero rows — see its own
docstring (jobcannon/db/_user_actions.py) for why that path is the only one
where "not applied" and "doesn't exist" can't otherwise be told apart.

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
from jobcannon.db._profiles import get_profile
from jobcannon.db._user_actions import dismiss_posting, mark_applied, save_posting, unmark_applied
from jobcannon.db.pool import connection_factory
from jobcannon.host.events import log_event
from jobcannon.web.apply_url import apply_destination_for_row
from jobcannon.web.feed_entries import build_entry

logger = logging.getLogger(__name__)

actions_bp = Blueprint("actions", __name__)


def _fetch_entry(
    conn, user_id: str, posting_id: int, *, include_dismissed: bool = False
) -> dict | None:
    """The one row `list_feed_postings` would render for this user today,
    narrowed to a single posting id. `include_dismissed` (#200, default
    `False` — the same default `list_feed_postings` itself has) is per-
    caller, not hardcoded here, because this helper is shared by all four
    routes below and they need opposite answers:

    - `save`/`apply`/`undo_apply` pass `include_dismissed=True`. Before
      #200, a mutation acting on a posting that was ALREADY dismissed by an
      earlier, unrelated action (or, for undo_apply, IS dismissed rather
      than applied — its own docstring already says it must not 404 that
      case) hit this function's default exclude-dismissed query, got back
      no row, and `_row_response` rendered an empty `200` body —
      indistinguishable from the mutation having silently failed, even
      though the write itself succeeded. Passing `True` here fixes that:
      the fragment these three routes swap in now shows the row's actual
      current state honestly, dismissed or not.
    - `dismiss` deliberately does NOT pass it, keeping the default `False`
      — seeing this file's `dismiss()` docstring and this module's own
      docstring above for why an empty body is dismiss's own INTENDED
      response (the row disappearing via an empty outerHTML swap), a
      behavior `test_dismissed_posting_disappears_from_the_dismissers_feed_
      but_not_anothers` already locks in and #200 must not regress.

    A normal feed page render (`jobcannon/web/pages.py`) calls
    `list_feed_postings` directly, never through this helper, and never
    passes `include_dismissed` — so it is unaffected by any of the above.

    `_fetch_entry` returns `None` only when the posting does not exist at
    all (or, for `dismiss`, when it does but is now excluded): routes that
    need to 404 on a nonexistent posting rely on the ForeignKeyViolation the
    write itself raises, not on this function's return value.

    Loads the caller's profile and passes it to `build_entry` the same way
    `jobcannon/web/pages.py`'s route does — `build_entry`'s second argument
    is the only input to `why_chips`'s title/skill overlap chip
    (`jobcannon/web/why.py::_overlap_chip`), so skipping this read here would
    silently drop that chip from every mutation-response fragment, even
    though the page render right before it showed the chip for the same
    row."""
    rows = list_feed_postings(
        conn,
        user_id=user_id,
        posting_id=posting_id,
        include_dismissed=include_dismissed,
        limit=1,
    )
    if not rows:
        return None
    profile = get_profile(conn, user_id)
    return build_entry(rows[0], profile)


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
            entry = _fetch_entry(conn, user_id, posting_id, include_dismissed=True)
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
            entry = _fetch_entry(conn, user_id, posting_id, include_dismissed=True)
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


@actions_bp.post("/postings/<int:posting_id>/undo-apply")
def undo_apply(posting_id: int):
    """#177: the Undo control `_posting_row.html` renders only on a row whose
    `entry.applied` is True. Same shape as save/dismiss (hx-post +
    hx-swap="outerHTML" on the row itself, unlike Apply's plain fetch) —
    `unmark_applied` deletes the `pipeline_status` row rather than writing a
    third status value, so the re-fetched entry comes back with
    `applied=False` and the row swaps back to its normal Apply control.

    `unmark_applied`'s own return value (not a `ForeignKeyViolation` catch —
    a bare DELETE never raises one, see this module's own docstring) is what
    404s a `posting_id` that never existed at all, matching save/dismiss/
    apply's contract without 404-ing a posting that exists but was, say,
    separately dismissed rather than applied."""
    user_id = g.clerk_user.user_id
    with connection_factory() as conn:
        posting_exists = unmark_applied(conn, user_id, posting_id)
        if not posting_exists:
            abort(404)
        entry = _fetch_entry(conn, user_id, posting_id, include_dismissed=True)
    log_event("posting_apply_undone", user_id=user_id, posting_id=posting_id)
    return _row_response(entry)

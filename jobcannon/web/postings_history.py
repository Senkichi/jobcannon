"""postings_history_bp — GET /postings: issue #180's in-app view of a
signed-in user's saved / applied / dismissed postings, gated behind the
before_request auth check the same way jobcannon/web/actions.py is (not
listed in PUBLIC_PATHS).

`view` (query string, default "saved") selects the cohort: "saved" reads
`jobcannon.db._user_actions.list_watchlist_entries`, "applied"/"dismissed"
read `list_pipeline_status_entries` and filter by `status` in Python — the
SAME two read helpers `jobcannon/web/export.py` already calls, never a
second, independently-maintained query over `watchlists`/`pipeline_status`.
An unrecognized `view` degrades to "saved" (the tolerant-default discipline
`jobcannon/web/pages.py::_parse_feed_filters` already uses for
workplace_type/sort) rather than a 400 or 500.

`page` (query string, default 1) selects a `FEED_PAGE_MAX`-sized slice of
that cohort's id list -- 1-based, validated as an int, and defaulted back to
1 on anything non-numeric or below 1; a page past the end of the list is not
an error, it just yields zero entries (the same "degrade, never 500"
discipline `view` uses). This is what turns `list_postings_by_ids`'s own
FEED_PAGE_MAX cap (jobcannon/db/_feed.py) into a page size instead of a
silent truncation: `_paginate_ids` below slices the id list BEFORE any row
is read, so only the current page's ids are ever handed to that function --
its own cap can no longer discard a row this page was supposed to show.

Every row is rendered through the SAME `jobcannon.web.feed_entries.build_entry`
+ `_posting_row.html` pair the authed feed uses (`jobcannon.db._feed.
list_postings_by_ids` is the one new read query — see its own docstring for
why it is not `list_feed_postings` with an id filter), but always with
`show_actions` left unset (Jinja's Undefined is falsy in `{% if %}`, the
same tolerance `_posting_row.html` already relies on for /preview and
/demo): jobcannon/web/actions.py's `_fetch_entry` re-reads a mutated row
through `list_feed_postings`, whose authed branch unconditionally excludes
`status = 'dismissed'` UNLESS the caller passes `include_dismissed=True` —
a Save/Dismiss/Apply/Undo control rendered on this page's own "dismissed"
tab would otherwise 200 with an EMPTY re-render fragment on click (the row
silently vanishing even on an otherwise-successful mutation, since
`_fetch_entry`'s re-read of that same posting comes back with zero rows).
#200 has since added exactly that escape hatch — `include_dismissed` on
`jobcannon/db/_feed.py`'s `_build_filters`/`list_feed_postings`, threaded
through `_fetch_entry`'s own `include_dismissed` parameter for save/apply/
undo_apply — so the hazard this docstring originally described is no
longer structurally blocking a fix here. This page still renders every row
read-only (no mutation control) for a DIFFERENT reason: `build_entry`/
`_posting_row.html` carry no `dismissed` field or visual state at all (see
`_fetch_entry`'s own docstring, actions.py), so wiring Save/Dismiss/Apply/
Undo onto this tab today would let a user act on a row with no way to tell,
after the click, whether it is still dismissed — a UX gap, not a
data-safety one anymore. Tracked as a follow-up: add a dismissed visual
state to `_posting_row.html` first, then wire mutation controls back in
using the now-available `include_dismissed` escape hatch.

Same HX-Request split every other route in this codebase uses, but note
what actually reaches each branch: the tab links (`postings_history.html`)
and the pagination prev/next links (`_postings_history_list.html`) are all
plain `<a href>` with no hx-boost on this page, so a real tab click or
page-N click is always a full-page GET that never carries `HX-Request` —
that traffic always lands on the `else` branch below, which renders
`postings_history.html` (the full page with the tab nav), and that template
itself `{% include %}`s the SAME `_postings_history_list.html` fragment for
its list slot. The `if request.headers.get("HX-Request") == "true":` branch
is reachable only by a caller that sets that header itself (a future JS
fetch, or a test) — never by anything the shipped UI does today.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, g, render_template, request

from jobcannon.db._feed import FEED_PAGE_MAX, list_postings_by_ids
from jobcannon.db._profiles import get_profile
from jobcannon.db._user_actions import list_pipeline_status_entries, list_watchlist_entries
from jobcannon.db.pool import connection_factory
from jobcannon.web.feed_entries import build_entry

logger = logging.getLogger(__name__)

postings_history_bp = Blueprint("postings_history", __name__)

_VIEWS = ("saved", "applied", "dismissed")
_DEFAULT_VIEW = "saved"

_EMPTY_COPY = {
    "saved": "No saved postings yet.",
    "applied": "No applied postings yet.",
    "dismissed": "No dismissed postings yet.",
}


def _parse_view(args: Any) -> str:
    view = (args.get("view") or _DEFAULT_VIEW).strip().lower()
    return view if view in _VIEWS else _DEFAULT_VIEW


def _parse_page(args: Any) -> int:
    """1-based, tolerant-default the same way `_parse_view` is: anything
    that doesn't parse as an int, or parses below 1, degrades to page 1
    rather than a 400. A value past the last page is left as-is (never
    clamped) -- `_paginate_ids` below already turns that into an empty
    slice via ordinary Python slicing, never an IndexError."""
    raw = args.get("page")
    if raw is None:
        return 1
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return 1
    return page if page >= 1 else 1


def _paginate_ids(posting_ids: list[int], page: int) -> tuple[list[int], dict[str, Any]]:
    """Slices the caller's already-user-scoped id list into one
    `FEED_PAGE_MAX`-sized page in Python, before a single posting row is
    read. Returns the page's own id slice plus the rendering metadata
    `_postings_history_list.html` needs for its "Showing X-Y of N" line and
    prev/next links. `page_start`/`page_end`/`has_prev`/`has_next` all come
    back as the empty-page defaults (0 / False) when the slice is empty --
    page 1 of an empty cohort and a page past the end look identical here,
    which is fine: both render this page's ordinary empty-state copy, never
    a distinct "you've gone too far" message."""
    total = len(posting_ids)
    start_index = (page - 1) * FEED_PAGE_MAX
    page_ids = posting_ids[start_index : start_index + FEED_PAGE_MAX]
    if not page_ids:
        return page_ids, {
            "page": page,
            "total": total,
            "page_start": 0,
            "page_end": 0,
            "has_prev": False,
            "has_next": False,
            "prev_page": page - 1,
            "next_page": page + 1,
        }
    page_end = start_index + len(page_ids)
    return page_ids, {
        "page": page,
        "total": total,
        "page_start": start_index + 1,
        "page_end": page_end,
        "has_prev": page > 1,
        "has_next": page_end < total,
        "prev_page": page - 1,
        "next_page": page + 1,
    }


def _read_entries(
    user_id: str, view: str, page: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fail-closed read, the same discipline as jobcannon/web/pages.py's
    `_read_feed_postings`: an unopened connection pool or a genuine DB
    outage degrades to an empty result list (this page's own empty-state
    copy still renders) rather than a 500 on an authenticated route.

    Newest-first ordering: `list_watchlist_entries` /
    `list_pipeline_status_entries` both return oldest-first (their own
    docstrings, `ORDER BY created_at` / `status_changed_at`), so the id list
    `_paginate_ids` slices is built from the REVERSED rows -- that
    ordering, not `list_postings_by_ids`, is what makes page 1 "most
    recent" rather than "oldest"; `list_postings_by_ids` preserves whatever
    order its caller's id list is already in, it does not re-sort by any DB
    column itself."""
    try:
        with connection_factory() as conn:
            if view == "saved":
                rows = list_watchlist_entries(conn, user_id)
                posting_ids = [
                    row["posting_id"]
                    for row in reversed(rows)
                    if row["posting_id"] is not None  # a saved COMPANY, not a posting
                ]
            else:
                rows = list_pipeline_status_entries(conn, user_id)
                posting_ids = [row["posting_id"] for row in reversed(rows) if row["status"] == view]
            page_ids, pagination = _paginate_ids(posting_ids, page)
            postings = list_postings_by_ids(conn, page_ids, user_id=user_id)
            profile = get_profile(conn, user_id)
            return [build_entry(row, profile) for row in postings], pagination
    except Exception:
        logger.warning(
            "postings history read failed for user %s view %s (defaulting to empty)",
            user_id,
            view,
            exc_info=True,
        )
        _, empty_pagination = _paginate_ids([], page)
        return [], empty_pagination


@postings_history_bp.get("/postings", strict_slashes=False)
def index():
    user_id = g.clerk_user.user_id
    view = _parse_view(request.args)
    page = _parse_page(request.args)
    entries, pagination = _read_entries(user_id, view, page)
    empty_copy = _EMPTY_COPY[view]

    if request.headers.get("HX-Request") == "true":
        return render_template(
            "_postings_history_list.html",
            entries=entries,
            empty_copy=empty_copy,
            view=view,
            pagination=pagination,
        )

    return render_template(
        "postings_history.html",
        entries=entries,
        view=view,
        views=_VIEWS,
        empty_copy=empty_copy,
        pagination=pagination,
    )

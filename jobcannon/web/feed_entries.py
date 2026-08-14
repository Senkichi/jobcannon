"""build_entry — composes one `list_feed_postings` / `get_posting_row` row
into the dict shape `_posting_row.html` renders: `row` (the raw DB row),
`chips` (jobcannon.web.why.why_chips(...), or the pending marker when that
call has nothing to restate yet), `saved` (bool, from the `saved` column
jobcannon/db/_feed.py now selects), and `apply_url` (the first usable
outbound link, jobcannon.web.apply_url.pick_apply_url, or None when the
posting has none — the row partial renders a disabled control in that case).

Shared by jobcannon/web/pages.py (the authenticated feed's initial render)
and jobcannon/web/actions.py (the save/dismiss/apply fragment re-render) so
both consumers of `_posting_row.html` build the identical entry shape from
one place instead of drifting — the same reasoning jobcannon/db/_feed.py's
`_build_filters` gives for being shared between `list_feed_postings` and
`count_feed_postings`.
"""

from __future__ import annotations

from typing import Any

from jobcannon.web.apply_url import pick_apply_url
from jobcannon.web.why import why_chips

# A row whose why_chips() call has nothing to restate still needs the UI to
# say something rather than silently render an empty chip list. This is a
# placeholder applied at the call site, not a why_chips() return value:
# why.py's own contract requires it be able to return []
# (tests/host/test_why.py::test_no_selections_yields_no_overlap_chip), so the
# fallback belongs here.
_WHY_PENDING_MARKER = "why: not yet available for this posting"


def build_entry(row: Any, profile_or_selections: Any) -> dict[str, Any]:
    saved = row["saved"]
    return {
        "row": row,
        "chips": why_chips(row, profile_or_selections) or [_WHY_PENDING_MARKER],
        "saved": bool(saved) if saved is not None else False,
        "apply_url": pick_apply_url(row),
    }

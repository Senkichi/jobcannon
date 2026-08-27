"""build_entry — composes one `list_feed_postings` / `get_posting_row` row
into the dict shape `_posting_row.html` renders: `row` (the raw DB row),
`chips` (jobcannon.web.why.why_chips(...), possibly empty — the pending
marker in `_posting_row.html` is keyed on `structural_axes` being NULL, not
on an empty chip list), `saved` (bool, from the `saved` column
jobcannon/db/_feed.py now selects), `applied` (bool, from the `applied`
column — `pipeline_status.status = 'applied'`, #177), and `apply_url` (the
first usable outbound link, jobcannon.web.apply_url.pick_apply_url, or None
when the posting has none — the row partial renders a disabled control in
that case).

Shared by jobcannon/web/pages.py (the authenticated feed's initial render)
and jobcannon/web/actions.py (the save/dismiss/apply fragment re-render) so
both consumers of `_posting_row.html` build the identical entry shape from
one place instead of drifting — the same reasoning jobcannon/db/_feed.py's
`_build_filters` gives for staying a single WHERE-clause builder rather than
duplicating filter logic per caller.
"""

from __future__ import annotations

from typing import Any

from jobcannon.web.apply_url import pick_apply_url
from jobcannon.web.why import why_chips


# An empty why_chips() return renders an empty chip list on purpose — no
# placeholder chip is injected here. The "signals still computing" marker in
# _posting_row.html covers the one state worth flagging (structural_axes
# still NULL), keyed on that column directly; a chips-empty fallback would
# duplicate and contradict it whenever the two conditions diverge.
def build_entry(row: Any, profile_or_selections: Any) -> dict[str, Any]:
    saved = row["saved"]
    applied = row["applied"]
    return {
        "row": row,
        "chips": why_chips(row, profile_or_selections),
        "saved": bool(saved) if saved is not None else False,
        "applied": bool(applied) if applied is not None else False,
        "apply_url": pick_apply_url(row),
    }

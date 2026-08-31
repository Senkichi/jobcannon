"""build_entry — composes one `list_feed_postings` / `list_postings_by_ids` row
into the dict shape `_posting_row.html` renders: `row` (the raw DB row),
`chips` (jobcannon.web.feed_entries.select_chips(jobcannon.web.why.chip_kinds(...))
output — a list of `{"label": str, "highlight": bool}` dicts, capped at 3,
possibly empty — the pending marker in `_posting_row.html` is keyed on
`structural_axes` being NULL, not on an empty chip list), `saved` (bool, from
the `saved` column
jobcannon/db/_feed.py now selects), `applied` (bool, from the `applied`
column — `pipeline_status.status = 'applied'`, #177), `apply_url` (the
first usable outbound link, jobcannon.web.apply_url.pick_apply_url, or None
when the posting has none — the row partial renders a disabled control in
that case), `salary_display` (jobcannon.web.salary_fmt.format_salary, or
None for no salary line), and `display_location` / `show_workplace_badge`
(dedupe_location below — spec §1's secondary tier: suppress whichever of
location / workplace-type badge merely restates the other).

Shared by jobcannon/web/pages.py (the authenticated feed's initial render),
jobcannon/web/actions.py (the save/dismiss/apply fragment re-render), and
jobcannon/web/onboarding.py's /preview, so every consumer of
`_posting_row.html` builds the identical entry shape from one place instead
of drifting — the same reasoning jobcannon/db/_feed.py's `_build_filters`
gives for staying a single WHERE-clause builder rather than duplicating
filter logic per caller.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from jobcannon.web.apply_url import pick_apply_url
from jobcannon.web.salary_fmt import format_salary
from jobcannon.web.why import chip_kinds

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: Any) -> set[str]:
    # Same 3-line tokenizer as jobcannon.web.why._tokenize — duplicated
    # rather than importing a private name across module boundaries.
    if not text:
        return set()
    return set(_WORD_RE.findall(str(text).lower()))


def dedupe_location(location: str | None, workplace_type: str | None) -> tuple[str | None, bool]:
    """(display_location, show_workplace_badge) — spec §1 secondary tier.

    Token-based and case-insensitive, so scraped variants ("Remote",
    "remote", "REMOTE - remote") all dedupe against workplace_type
    ('REMOTE'/'HYBRID'/'ONSITE' or None, m0001):

    - location tokens ⊆ workplace tokens (location only restates the
      type, e.g. "Remote" vs REMOTE) → drop the location, keep the badge.
    - workplace tokens ⊆ location tokens (location says more, e.g.
      "Remote (US)") → keep the location, the badge is redundant.
    - disjoint / partial → both carry information, show both.
    """
    loc_tokens = _tokenize(location)
    wt_tokens = _tokenize(workplace_type)
    if not wt_tokens:
        return (location or None), False
    if not loc_tokens:
        return None, True
    if loc_tokens <= wt_tokens:
        return None, True
    if wt_tokens <= loc_tokens:
        return location, False
    return location, True


# Priority is a total order (spec §1: overlap > freshness > seniority >
# jd_quality — ties impossible), so selection is a stable slice, and the
# green honesty accent can only ever land on the single top chip.
_CHIP_PRIORITY = ("overlap", "freshness", "seniority", "jd_quality")
_HIGHLIGHT_KINDS = frozenset({"overlap", "freshness"})
_CHIP_CAP = 3


def select_chips(kinds: Mapping[str, str | None]) -> list[dict[str, object]]:
    """Cap and prioritize chip_kinds output for rendering (spec §1/§2).
    highlight=True (-> .jc-chip--why, the row's one green accent) goes to
    at most the FIRST selected chip, and only when its kind is overlap or
    freshness — seniority/JD boilerplate never earns green even when it
    happens to lead."""
    ordered = [(kind, kinds.get(kind)) for kind in _CHIP_PRIORITY if kinds.get(kind)]
    return [
        {"label": label, "highlight": index == 0 and kind in _HIGHLIGHT_KINDS}
        for index, (kind, label) in enumerate(ordered[:_CHIP_CAP])
    ]


# An empty chips selection renders an empty chip list on purpose — no
# placeholder chip is injected here. The "signals still computing" marker in
# _posting_row.html covers the one state worth flagging (structural_axes
# still NULL), keyed on that column directly; a chips-empty fallback would
# duplicate and contradict it whenever the two conditions diverge.
def build_entry(row: Any, profile_or_selections: Any) -> dict[str, Any]:
    saved = row["saved"]
    applied = row["applied"]
    display_location, show_workplace_badge = dedupe_location(row["location"], row["workplace_type"])
    return {
        "row": row,
        "chips": select_chips(chip_kinds(row, profile_or_selections)),
        "saved": bool(saved) if saved is not None else False,
        "applied": bool(applied) if applied is not None else False,
        "apply_url": pick_apply_url(row),
        "salary_display": format_salary(row),
        "display_location": display_location,
        "show_workplace_badge": show_workplace_badge,
    }

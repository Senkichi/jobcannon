"""why_chips — pure, DB-free literal restatements of stored posting values.

No model call, no classification, no fit label: a chip may say *what is
stored* on the row (an age band, a title/skill token that overlaps the
visitor's own selections), never *how good* the posting is for that
visitor. The axis-value -> label mapping lives once, here, so
`jobcannon/web/onboarding.py` (and later authenticated consumers of the
shared feed partials) never re-derive it.

Row access is STRING-KEY only via `_get`, matching every DAL module in this
codebase (`jobcannon/db/_profiles.py`, `_stats.py`, `_feed.py`): both the
pooled `HybridRow` (jobcannon/db/rows.py, a `Sequence` with `__getitem__`
and no `.get()`) and a plain `dict` support `row["col"]`, so `_get` tries
`__getitem__` and falls back on `KeyError`/`IndexError` rather than relying
on a `.get()` method neither row type guarantees.

`selections_or_profile` is deliberately either shape: the pre-signup preview
(jobcannon/web/onboarding.py) passes the session's `pending_picker` dict
(keys `titles` / `skills`); a later authenticated consumer may pass a
`profiles` row instead (keys `target_titles` / `skills`) — `_selection_tokens`
reads both spellings so this module doesn't have to change when that second
caller arrives.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_WORD_RE = re.compile(r"[a-z0-9]+")

# score_freshness (jobcannon/host/structural_axes/freshness.py) returns one
# of exactly these six values. Checked highest-first: 0.3 ("no usable date
# at all") sits ABOVE 0.2 ("confirmed >90 days old") even though it is a
# smaller number of days-implied-freshness, because it means something
# different (no evidence, not old evidence) — a plain `>=` ladder in this
# order still resolves both correctly since the six values are strictly
# decreasing and each threshold below equals its own exact source value.
#
# score_freshness anchors the age bucket on posted_date only when
# posted_date_precision is 'exact'/'approximate'; otherwise it falls back to
# last_seen (freshness.py's own docstring states this). A single "posted..."
# label set would misdescribe every last_seen-anchored row as an
# origination date it does not have, so the label set is chosen per row from
# the same precision value the scorer itself branches on. The two "no
# anchor at all" / "flagged stale" bands (0.3 / 0.1) describe a fact that
# does not depend on which anchor was used, so they are identical in both
# sets.
#
# This chip-selection branch reads posted_date_precision alone (no
# posted_date) and trusts that a non-null 'exact'/'approximate' precision
# means score_freshness actually anchored on posted_date rather than
# silently falling back to last_seen because posted_date was null. That
# trust is not an assumption: postings' schema CHECK constraint
# `(posted_date IS NULL) = (posted_date_precision IS NULL)`
# (jobcannon/db/migrations/m0001_initial_schema.py) makes a non-null
# precision without a posted_date unrepresentable at the database layer, and
# tests/host/test_schema.py::test_posted_date_pairing_check asserts the
# insert itself raises. No row this module can ever see has one without the
# other.
_FRESHNESS_BANDS_POSTED_ANCHOR: tuple[tuple[float, str], ...] = (
    (1.0, "posted within the last week"),
    (0.7, "posted within the last month"),
    (0.4, "posted within the last quarter"),
    (0.3, "no confirmed post date"),
    (0.2, "posting is likely over 90 days old"),
    (0.1, "listing shows signs of being stale"),
)
_FRESHNESS_BANDS_LAST_SEEN_ANCHOR: tuple[tuple[float, str], ...] = (
    (1.0, "last seen within the last week"),
    (0.7, "last seen within the last month"),
    (0.4, "last seen within the last quarter"),
    (0.3, "no confirmed post date"),
    (0.2, "last seen over 90 days ago"),
    (0.1, "listing shows signs of being stale"),
)
_TRUSTWORTHY_POSTED_DATE_PRECISIONS = ("exact", "approximate")

_JD_QUALITY_MIN = 0.6


def _get(mapping: Any, key: str, default: Any = None) -> Any:
    try:
        return mapping[key]
    except (KeyError, IndexError):
        return default


def _tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(_WORD_RE.findall(str(text).lower()))


def _freshness_chip(axes: Mapping[str, Any], posted_date_precision: Any) -> str | None:
    axis = _get(axes, "freshness")
    if not isinstance(axis, Mapping):
        return None
    value = _get(axis, "value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    bands = (
        _FRESHNESS_BANDS_POSTED_ANCHOR
        if posted_date_precision in _TRUSTWORTHY_POSTED_DATE_PRECISIONS
        else _FRESHNESS_BANDS_LAST_SEEN_ANCHOR
    )
    for threshold, label in bands:
        if value >= threshold:
            return label
    return None


def _seniority_chip(axes: Mapping[str, Any]) -> str | None:
    axis = _get(axes, "seniority_clarity")
    if not isinstance(axis, Mapping):
        return None
    value = _get(axis, "value")
    return "level stated in title" if value is True else None


def _jd_quality_chip(axes: Mapping[str, Any]) -> str | None:
    axis = _get(axes, "jd_quality")
    if not isinstance(axis, Mapping):
        return None
    value = _get(axis, "value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return "JD looks complete" if value >= _JD_QUALITY_MIN else None


def _selection_tokens(selections_or_profile: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ("titles", "target_titles", "skills"):
        values = _get(selections_or_profile, key)
        if not values:
            continue
        for value in values:
            tokens |= _tokenize(value)
    return tokens


def _overlap_chip(row: Any, selections_or_profile: Mapping[str, Any]) -> str | None:
    selection_tokens = _selection_tokens(selections_or_profile)
    if not selection_tokens:
        return None
    matched = sorted(selection_tokens & _tokenize(_get(row, "title")))
    if not matched:
        return None
    return f"title matches your selections: {', '.join(matched)}"


def chip_kinds(
    row: Any, selections_or_profile: Mapping[str, Any] | None
) -> dict[str, str | None]:
    """Chip label per kind, unprioritized and uncapped — the single source
    jobcannon.web.feed_entries.select_chips prioritizes and caps from
    (spec §1 tier 3). Keys are always exactly ("overlap", "freshness",
    "seniority", "jd_quality"); a kind with nothing honest to say maps to
    None.

    Same row contract why_chips documented: `structural_axes`,
    `posted_date_precision`, and `title` by string key — the exact shape
    `jobcannon.db._feed.list_feed_postings` returns. A None or malformed
    `structural_axes` degrades to None for the three axis-derived kinds
    only; overlap reads the row/selections directly and still resolves
    (a posting the axes batch hasn't reached yet still gets chips, just
    fewer).
    """
    selections_or_profile = selections_or_profile or {}
    kinds: dict[str, str | None] = {
        "overlap": _overlap_chip(row, selections_or_profile),
        "freshness": None,
        "seniority": None,
        "jd_quality": None,
    }
    axes = _get(row, "structural_axes")
    if isinstance(axes, Mapping):
        posted_date_precision = _get(row, "posted_date_precision")
        kinds["freshness"] = _freshness_chip(axes, posted_date_precision)
        kinds["seniority"] = _seniority_chip(axes)
        kinds["jd_quality"] = _jd_quality_chip(axes)
    return kinds


def why_chips(row: Any, selections_or_profile: Mapping[str, Any] | None) -> list[str]:
    """COMPAT WRAPPER — deleted by this plan's Task 10. Flat chip strings
    in the legacy render order (freshness, seniority, jd_quality, overlap),
    kept only because feed_entries.build_entry renders flat strings until
    the Wave-2 template rewrite (Task 8) swaps it to
    select_chips(chip_kinds(...)). The "salary listed" chip is gone for
    good (spec §1: redundant once the salary number is prominent in the
    card's primary tier)."""
    kinds = chip_kinds(row, selections_or_profile)
    ordered = (kinds["freshness"], kinds["seniority"], kinds["jd_quality"], kinds["overlap"])
    return [chip for chip in ordered if chip is not None]

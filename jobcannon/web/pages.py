"""GET / (authed feed) and GET /demo (public guest demo).

GET / now renders the authed user's profile plus a real, server-filtered
posting list: title/company/workplace-type/location filters and a sort
token, all read from the query string and validated against a fixed
allowlist before any of it reaches SQL — an unrecognized token degrades to
the unfiltered/default value rather than a 500. Each row carries its own
literal "why" chips (jobcannon.web.why.why_chips) or, when that call has
nothing to restate for a given row, a single pending-signal marker, so a
row never renders a silently-empty chip list. `feed_state` has no writer
anywhere in this codebase yet, so every row's rank comes back NULL today —
the ordering label says so honestly (`UNRANKED_VERSION`, defined once here
so a later event-emitting consumer can import the same literal instead of
retyping it) rather than implying a ranking that has not run. No
watchlist/pipeline UI and no `posting_impression` events are wired from
this route — that remains separate, later work. GET /demo is unchanged: it
still shows corpus COUNTS only, never a posting list. Picker-first
onboarding (GET/POST /start, GET /preview) lives in
jobcannon/web/onboarding.py, not here.

`corpus_stats` / `get_profile` / `list_feed_postings` / `connection_factory`
are imported at MODULE level (unlike `jobcannon/web/__init__.py`'s
`_resolve_consent`, which does an inline `from jobcannon.db import _events`
import inside the function body) specifically so tests can monkeypatch
`jobcannon.web.pages.corpus_stats`, `.get_profile`, `.list_feed_postings`,
and `.connection_factory` directly as module attributes — an inline import
re-fetches the real function on every call and would not be patchable that
way.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, g, render_template, request

from jobcannon.db import _feed
from jobcannon.db._feed import list_feed_postings
from jobcannon.db._profiles import GUEST_USER_ID, get_profile
from jobcannon.db._stats import corpus_stats
from jobcannon.db.pool import connection_factory
from jobcannon.web.why import why_chips

logger = logging.getLogger(__name__)

pages_bp = Blueprint("pages", __name__)

_EMPTY_STATS = {"postings": 0, "companies": 0, "freshest_last_seen": None}

# The literal ranker-version label shown for every row `list_feed_postings`
# returns with no matching `feed_state` row — which is every row today
# (feed_state has no writer anywhere in this codebase: jobcannon/db/_feed.py's
# module docstring). Defined once here and meant to be imported by a later
# event-emitting consumer rather than retyped, so the displayed label and
# whatever gets logged can never drift apart.
UNRANKED_VERSION = "unranked-v0"

# A row whose why_chips() call has nothing to restate (e.g. the
# structural-axes batch — capped at 500 rows per scan tick,
# jobcannon/host/structural_axes/__init__.py — hasn't reached it yet, no
# salary is listed, and the visitor's profile has no title/skill overlap)
# still needs the UI to say something rather than silently render an empty
# chip list. This is a placeholder applied at the call site, not a
# why_chips() return value: why.py's own contract requires it be able to
# return [] (tests/host/test_why.py::test_no_selections_yields_no_overlap_chip),
# so the fallback belongs here, not inside why_chips itself.
_WHY_PENDING_MARKER = "why: not yet available for this posting"

# Workplace-type filter vocabulary: the form speaks lowercase,
# postings.workplace_type (jobcannon/db/_jobs.py, written from
# jobcannon/engine/location_canonical.py's WorkplaceType Literal) holds only
# uppercase tokens or NULL, and jobcannon/db/_feed.py's _build_filters
# compares with exact equality — no lower() on either side. "any" maps to
# None (_build_filters already treats that as "no filter applied"). Mirrors
# jobcannon/web/onboarding.py's identical mapping for the picker's own
# workplace_type field; kept local (not imported from there) so this route's
# filter parsing has no dependency on the onboarding module's internals.
_WORKPLACE_TYPES = ("any", "remote", "hybrid", "onsite")
_WORKPLACE_TYPE_DB_VALUES = {"remote": "REMOTE", "hybrid": "HYBRID", "onsite": "ONSITE"}


def _read_page_data(user_id: str) -> tuple[dict, Any]:
    """Fail-closed page-data read, mirroring `_resolve_consent`'s shape
    (jobcannon/web/__init__.py): any DB error — an unopened connection pool
    (TESTING, the same state as tests/host/test_auth.py's identity-only
    tests) or a genuine outage — degrades to the corpus-empty / no-profile
    branch rather than surfacing as a 500."""
    try:
        with connection_factory() as conn:
            return corpus_stats(conn), get_profile(conn, user_id)
    except Exception:
        logger.warning(
            "page data read failed for user %s (defaulting to corpus-empty)",
            user_id,
            exc_info=True,
        )
        return dict(_EMPTY_STATS), None


def _parse_feed_filters(args: Any) -> dict[str, str]:
    """GET query params -> display-safe filter values. Every value is a
    plain string, never None, so the template can always do
    `value="{{ filters.title }}"` with no `or ''` guard, and every value is
    ALREADY validated: an unrecognized workplace_type or sort token degrades
    to "any" / "default" here rather than raising — this is what keeps an
    unknown sort token a graceful no-op instead of a 500."""
    workplace_type = (args.get("workplace_type") or "any").strip().lower()
    if workplace_type not in _WORKPLACE_TYPES:
        workplace_type = "any"
    sort = (args.get("sort") or "default").strip()
    if sort not in _feed._SORTS:
        sort = "default"
    return {
        "title": (args.get("title") or "").strip(),
        "company": (args.get("company") or "").strip(),
        "location": (args.get("location") or "").strip(),
        "workplace_type": workplace_type,
        "sort": sort,
    }


def _feed_query_kwargs(filters: dict[str, str]) -> dict[str, Any]:
    """Display-safe `filters` (see `_parse_feed_filters`) -> the keyword
    arguments `list_feed_postings` accepts. `title` is matched via the same
    exact-match `titles` list parameter the picker's structured selections
    already use (jobcannon/db/_feed.py has no substring title filter)."""
    return {
        "titles": [filters["title"]] if filters["title"] else None,
        "company": filters["company"] or None,
        "workplace_type": _WORKPLACE_TYPE_DB_VALUES.get(filters["workplace_type"]),
        "location_contains": filters["location"] or None,
        "sort": filters["sort"],
    }


def _read_feed_postings(*, user_id: str, filters: dict[str, str]) -> list[Any]:
    """Fail-closed feed read, the same discipline as `_read_page_data` /
    jobcannon/web/onboarding.py's `_read_preview_postings`: an unopened
    connection pool or a genuine DB outage degrades to an empty result list
    (`_feed_list.html`'s own empty-state branch still renders) rather than a
    500 on an authenticated route. A sibling of `_read_page_data` rather than
    an extension of it — the no-profile / empty-corpus branches never need a
    feed read at all, so keeping this a separate call avoids querying
    postings when the page will not render them."""
    try:
        with connection_factory() as conn:
            return list_feed_postings(conn, user_id=user_id, **_feed_query_kwargs(filters))
    except Exception:
        logger.warning(
            "feed postings read failed for user %s (defaulting to empty result set)",
            user_id,
            exc_info=True,
        )
        return []


def _ordering_label(rows: list[Any]) -> dict[str, Any]:
    """Honest ordering label: `feed_state` is read-only in Phase 1C (never
    written anywhere), so every row comes back with rank_score NULL today —
    the unranked branch is the default rendering path, not an edge case.
    Written as a real check on the rows (mirroring
    jobcannon/web/onboarding.py's identical helper) so this stays correct
    once a later PR ships a real ranker, rather than a bare constant."""
    ranked_versions = [r["ranker_version"] for r in rows if r["rank_score"] is not None]
    if not ranked_versions:
        return {"personalized": False, "ranker_version": UNRANKED_VERSION}
    version = ranked_versions[0] if len(set(ranked_versions)) == 1 else None
    return {"personalized": True, "ranker_version": version or UNRANKED_VERSION}


@pages_bp.get("/", strict_slashes=False)
def feed():
    user_id = g.clerk_user.user_id
    stats, profile = _read_page_data(user_id)
    filters = _parse_feed_filters(request.args)

    entries: list[dict[str, Any]] = []
    ordering = {"personalized": False, "ranker_version": UNRANKED_VERSION}
    if profile is not None and stats.get("postings", 0) > 0:
        rows = _read_feed_postings(user_id=user_id, filters=filters)
        entries = [
            {"row": row, "chips": why_chips(row, profile) or [_WHY_PENDING_MARKER]} for row in rows
        ]
        ordering = _ordering_label(rows)

    return render_template(
        "feed.html",
        stats=stats,
        profile=profile,
        entries=entries,
        filters=filters,
        ordering=ordering,
        sort_tokens=sorted(_feed._SORTS),
        workplace_types=_WORKPLACE_TYPES,
    )


@pages_bp.get("/demo", strict_slashes=False)
def demo():
    stats, profile = _read_page_data(GUEST_USER_ID)
    return render_template("demo.html", stats=stats, profile=profile)

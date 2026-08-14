"""GET / (authed feed) and GET /demo (public guest demo).

GET / now renders the authed user's profile plus a real, server-filtered
posting list: title/company/workplace-type/location filters and a sort
token, all read from the query string and validated against a fixed
allowlist before any of it reaches SQL — an unrecognized token degrades to
the unfiltered/default value rather than a 500. Each row carries its own
literal "why" chips (jobcannon.web.feed_entries.build_entry, which wraps
jobcannon.web.why.why_chips); a row whose `structural_axes` is still NULL
(the axes batch caps at 500 rows per scan tick, so a large pre-seed leaves
a transient NULL slice) also renders a "signals still computing" marker
alongside whatever chips it does have, rather than silently omitting the
fact that axis-derived signals aren't in yet. That marker lives in
`_posting_row.html` (shared by this route and jobcannon.web.onboarding's
/preview through `_feed_list.html`), keyed on the NULL column itself and
never on an empty chip list, so it can't drift between the two consumers.
`feed_state` has no writer
anywhere in this codebase yet, so every row's rank comes back NULL today —
the ordering label says so honestly (`UNRANKED_VERSION`, defined once here
so a later event-emitting consumer can import the same literal instead of
retyping it) rather than implying a ranking that has not run. Each row also
carries its per-user `saved` state and a usable apply link when one exists
(jobcannon.web.feed_entries.build_entry) — the save/dismiss/apply mutation
routes themselves live in jobcannon/web/actions.py, not here, this route
only renders the controls (`show_actions=True`) — and, on every render,
this route logs one `posting_impression` event per rendered row
(feed_position 1-based, ranker_version from the row or `UNRANKED_VERSION`)
through jobcannon.host.events.log_event. GET /demo is unchanged: it
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
from jobcannon.host.events import log_event
from jobcannon.web.feed_entries import build_entry

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

# The `surface` value logged on every posting_impression event this route
# emits (events_schema._ALLOWED_KEYS["posting_impression"] == {"surface"}).
# Defined once so the literal typed into the feed template's rendering
# decision and the literal written to the payload can never drift apart —
# there is only one call site today, but the constant is what makes that a
# choice rather than an accident.
_IMPRESSION_SURFACE = "feed"

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
    arguments `list_feed_postings` accepts. `title` is matched via
    `title_contains` (a substring `LIKE`, jobcannon/db/_feed.py) — distinct
    from the exact-match `titles` list parameter the picker's structured
    selections use; this route's title box is free text, not a pick from a
    fixed corpus-derived set."""
    return {
        "title_contains": filters["title"] or None,
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


def _log_impressions(user_id: str, rows: list[Any]) -> None:
    """One `posting_impression` event per rendered row: `feed_position` is
    the 1-based index within THIS response, `ranker_version` is the row's
    own `feed_state` value or the shared `UNRANKED_VERSION` literal when
    unranked. No cross-request dedup — a second page view is a second
    impression, by design, which is what makes "every impression carries
    position and ranker_version" mechanically checkable rather than
    incidentally true of whichever request happened to run first.
    `consent_granted` is left at its default (None): `log_event` resolves it
    from the ambient per-request `g.consent_granted` set by
    `jobcannon.web`'s before_request hook, so a non-consenting user's
    impressions are dropped before any Postgres write, not filtered here."""
    for position, row in enumerate(rows, start=1):
        log_event(
            "posting_impression",
            user_id=user_id,
            posting_id=row["id"],
            feed_position=position,
            ranker_version=row["ranker_version"] or UNRANKED_VERSION,
            payload={"surface": _IMPRESSION_SURFACE},
        )


@pages_bp.get("/", strict_slashes=False)
def feed():
    user_id = g.clerk_user.user_id
    stats, profile = _read_page_data(user_id)
    filters = _parse_feed_filters(request.args)

    entries: list[dict[str, Any]] = []
    ordering = {"personalized": False, "ranker_version": UNRANKED_VERSION}
    if profile is not None and stats.get("postings", 0) > 0:
        rows = _read_feed_postings(user_id=user_id, filters=filters)
        entries = [build_entry(row, profile) for row in rows]
        ordering = _ordering_label(rows)
        _log_impressions(user_id, rows)

    return render_template(
        "feed.html",
        stats=stats,
        profile=profile,
        entries=entries,
        filters=filters,
        ordering=ordering,
        sort_tokens=sorted(_feed._SORTS),
        workplace_types=_WORKPLACE_TYPES,
        show_actions=True,
    )


@pages_bp.get("/demo", strict_slashes=False)
def demo():
    stats, profile = _read_page_data(GUEST_USER_ID)
    return render_template("demo.html", stats=stats, profile=profile)

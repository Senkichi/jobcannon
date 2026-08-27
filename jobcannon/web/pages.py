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
through jobcannon.host.events.log_event. GET /demo now renders the
canned guest profile (jobcannon.db._profiles.GUEST_USER_ID) plus, once that
profile is seeded, the same populated feed and literal why-chips filtered to
its target_titles — the corpus-counts-only render is now the fallback for
when the guest profile row is absent or the corpus itself is empty, not the
whole page. Unlike GET /, /demo renders no mutation controls (it never
passes show_actions) and logs no events: its g.consent_granted is hardcoded
False (jobcannon/web/__init__.py) and its render path never calls
`_log_impressions` or log_event. Picker-first onboarding (GET/POST
/start, GET /preview) lives in jobcannon/web/onboarding.py, not here.

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

from flask import Blueprint, g, render_template, request, url_for

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


def _read_feed_postings(
    *, user_id: str, filters: dict[str, str], after: tuple[float | None, Any, int] | None = None
) -> list[Any]:
    """Fail-closed feed read, the same discipline as `_read_page_data` /
    jobcannon/web/onboarding.py's `_read_preview_postings`: an unopened
    connection pool or a genuine DB outage degrades to an empty result list
    (`_feed_list.html`'s own empty-state branch still renders) rather than a
    500 on an authenticated route. A sibling of `_read_page_data` rather than
    an extension of it — the no-profile / empty-corpus branches never need a
    feed read at all, so keeping this a separate call avoids querying
    postings when the page will not render them. `after` (#156) is the
    keyset cursor for "Load more" — see jobcannon/db/_feed.py's
    list_feed_postings/_cursor_predicate."""
    try:
        with connection_factory() as conn:
            return list_feed_postings(
                conn, user_id=user_id, after=after, **_feed_query_kwargs(filters)
            )
    except Exception:
        logger.warning(
            "feed postings read failed for user %s (defaulting to empty result set)",
            user_id,
            exc_info=True,
        )
        return []


# Every `_parse_feed_filters` key's own "no filter applied" value — omitted
# from a "Load more" URL's query string rather than round-tripped literally,
# so the next-page link stays as clean as a fresh, unfiltered GET / would be
# (and, for workplace_type/sort specifically, never risks reading back as a
# real filter value on the next request — see _feed_load_more_url).
_FILTER_DEFAULTS = {
    "title": "",
    "company": "",
    "location": "",
    "workplace_type": "any",
    "sort": "default",
}


def _feed_load_more_url(filters: dict[str, str], rows: list[Any]) -> str | None:
    """Next-page URL for the authed feed's "Load more" control, or None when
    this page came back short of FEED_PAGE_MAX — a keyset page shorter than
    the cap proves there is nothing left to seek past (#156's stable-cursor
    requirement: this is a seek, never an OFFSET, so there is no drift
    between the row a click was expecting and the row it gets even if the
    corpus changes between clicks). Carries forward every non-default filter
    value already on this page (`_FILTER_DEFAULTS`) plus the cursor derived
    from the LAST row actually rendered (`cursor_from_row`) — a "Load more"
    click can never land on a different filter set than what the visitor is
    currently looking at."""
    if len(rows) < _feed.FEED_PAGE_MAX:
        return None
    query = {k: v for k, v in filters.items() if v != _FILTER_DEFAULTS.get(k, "")}
    return url_for("pages.feed", **query, **_feed.cursor_from_row(rows[-1]))


def _read_demo_feed_postings(profile: Any) -> list[Any]:
    """Fail-closed feed read for /demo, the same discipline as
    `_read_feed_postings`: an unopened connection pool or a genuine DB
    outage degrades to an empty result list (`_feed_list.html`'s own
    empty-state branch still renders) rather than a 500 on a public,
    unauthenticated entry point. Filtered by the guest profile's own
    target_titles (the canned selections `scripts/seed_guest_demo.py`
    seeds) — /demo has no query-string filters of its own; it is a
    read-only showcase of one fixed profile, not a general-purpose search."""
    try:
        with connection_factory() as conn:
            return list_feed_postings(conn, user_id=GUEST_USER_ID, titles=profile["target_titles"])
    except Exception:
        logger.warning("demo feed read failed (defaulting to empty result set)", exc_info=True)
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
    """#156: paginates via a keyset cursor read from `cursor_id`/
    `cursor_last_seen`/`cursor_rank_score` query params (jobcannon.db._feed.
    parse_cursor — a malformed/tampered value degrades to a first page, not
    a 500). Same HX-Request split as GET /start (#148): the "Load more"
    button hx-gets THIS route with the current filters plus the next cursor
    attached, so an HX-Request returns only the next batch (+ a further
    "Load more", or nothing when exhausted — `_feed_page.html`, WITH
    show_actions so save/dismiss/apply keep working on appended rows); a
    direct browser hit gets the full page, at whatever page the cursor
    names. Impressions are logged for every row this response actually
    renders, including "Load more" continuations — a later batch is exactly
    as much a real impression as the first one."""
    user_id = g.clerk_user.user_id
    stats, profile = _read_page_data(user_id)
    # #176: the no-profile branch's copy is derived from this, not hardcoded
    # release-date prose — a profile row exists iff the visitor has completed
    # the picker (jobcannon/db/_profiles.py's upsert_profile is the only
    # writer), the same "has this visitor made selections yet" question
    # jobcannon/web/onboarding.py's /preview route already answers with its
    # own `has_selections` flag for the pre-signup feed.
    has_selections = profile is not None
    filters = _parse_feed_filters(request.args)
    after = _feed.parse_cursor(request.args)

    entries: list[dict[str, Any]] = []
    ordering = {"personalized": False, "ranker_version": UNRANKED_VERSION}
    load_more_url = None
    if profile is not None and stats.get("postings", 0) > 0:
        rows = _read_feed_postings(user_id=user_id, filters=filters, after=after)
        entries = [build_entry(row, profile) for row in rows]
        ordering = _ordering_label(rows)
        _log_impressions(user_id, rows)
        load_more_url = _feed_load_more_url(filters, rows)

    if request.headers.get("HX-Request") == "true":
        return render_template(
            "_feed_page.html", entries=entries, load_more_url=load_more_url, show_actions=True
        )

    return render_template(
        "feed.html",
        stats=stats,
        profile=profile,
        has_selections=has_selections,
        entries=entries,
        filters=filters,
        ordering=ordering,
        sort_tokens=sorted(_feed._SORTS),
        workplace_types=_WORKPLACE_TYPES,
        show_actions=True,
        load_more_url=load_more_url,
    )


@pages_bp.get("/demo", strict_slashes=False)
def demo():
    stats, profile = _read_page_data(GUEST_USER_ID)

    entries: list[dict[str, Any]] = []
    ordering = {"personalized": False, "ranker_version": UNRANKED_VERSION}
    if profile is not None and stats.get("postings", 0) > 0:
        rows = _read_demo_feed_postings(profile)
        entries = [build_entry(row, profile) for row in rows]
        ordering = _ordering_label(rows)

    return render_template(
        "demo.html", stats=stats, profile=profile, entries=entries, ordering=ordering
    )

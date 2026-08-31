"""GET / (authed feed), POST /feed/clear-selection, and GET /demo (public
guest demo).

GET / now renders the authed user's profile plus a real, server-filtered
posting list: a free-text title/company/location box and a sort token,
read from the query string and validated against a fixed allowlist before
any of it reaches SQL — an unrecognized token degrades to the
unfiltered/default value rather than a 500. On top of those query-string
filters (#170), the signed-in user's saved picker selections
(profiles.target_titles/target_companies/workplace_type, written by
jobcannon/web/onboarding.py's POST /start) ALSO filter this feed, through
the exact same jobcannon.db._feed.selection_filter_kwargs predicate builder
onboarding.py's pre-signup /preview calls (#169) — see `_read_feed_postings`
below for the one collision (workplace_type) and how it resolves. A saved
titles/companies selection ANDs (never ORs) with the free-text title/company
boxes, by deliberate product decision (#206) — an exact-match saved pick can
zero out an otherwise-matching search with no visible signal why, so
`_saved_selection_indicator`/`_feed_empty_reason` surface that a selection is
filtering the feed and `clear_selection` (this module's other route) gives a
one-click way out, rather than changing the AND itself. Each row carries its own
literal "why" chips (jobcannon.web.feed_entries.build_entry, which wraps
jobcannon.web.why.chip_kinds); a row whose `structural_axes` is still NULL
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

from flask import Blueprint, g, redirect, render_template, request, url_for

from jobcannon.db import _feed
from jobcannon.db._feed import list_feed_postings
from jobcannon.db._profiles import GUEST_USER_ID, clear_profile_targets, get_profile
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


def _parse_feed_filters(args: Any) -> dict[str, Any]:
    """GET query params -> display-safe filter values. Every value is a
    plain string, never None, so the template can always do
    `value="{{ filters.title }}"` with no `or ''` guard, and every value is
    ALREADY validated: an unrecognized workplace_type or sort token degrades
    to "any" / "default" here rather than raising — this is what keeps an
    unknown sort token a graceful no-op instead of a 500.

    `workplace_type_explicit` (F1 fix): True only when the request itself
    named a recognized `?workplace_type=` value — including an explicit
    "any" — as opposed to the param being absent entirely (both cases
    otherwise collapse to the same displayed "any"/None). `_read_feed_postings`
    uses this to distinguish "the visitor asked to see every workplace type"
    from "the visitor didn't say," so an explicit "any" can actually clear a
    saved profile preference instead of silently falling back to it. Not a
    real SQL-facing filter kwarg — `_feed_query_kwargs` deliberately never
    reads this key, only `filters` consumers that need the absent/explicit
    distinction do."""
    raw_workplace_type = (args.get("workplace_type") or "").strip().lower()
    workplace_type_explicit = raw_workplace_type in _WORKPLACE_TYPES
    workplace_type = raw_workplace_type if workplace_type_explicit else "any"
    sort = (args.get("sort") or "default").strip()
    if sort not in _feed._SORTS:
        sort = "default"
    return {
        "title": (args.get("title") or "").strip(),
        "company": (args.get("company") or "").strip(),
        "location": (args.get("location") or "").strip(),
        "workplace_type": workplace_type,
        "workplace_type_explicit": workplace_type_explicit,
        "sort": sort,
    }


def _feed_query_kwargs(filters: dict[str, Any]) -> dict[str, Any]:
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


def _effective_query_kwargs(
    filters: dict[str, Any], selection_kwargs: dict[str, Any]
) -> dict[str, Any]:
    """`_feed_query_kwargs(filters)` plus the one F1 workplace_type fallback
    (an absent/non-explicit query-string value falls back to the profile's
    saved preference) — factored out of `_read_feed_postings` (#206 follow-
    up) so `_feed_empty_reason`'s ground-truth collision probe below can
    resolve the SAME effective non-selection filters the real query used,
    rather than a second, independently re-derived copy of the F1 fallback
    logic that could drift from this one."""
    query_kwargs = _feed_query_kwargs(filters)
    if query_kwargs["workplace_type"] is None and not filters.get("workplace_type_explicit"):
        query_kwargs = {**query_kwargs, "workplace_type": selection_kwargs["workplace_type"]}
    return query_kwargs


def _read_feed_postings(
    *,
    user_id: str,
    filters: dict[str, Any],
    selection_kwargs: dict[str, Any],
    after: tuple[float | None, Any, int] | None = None,
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
    list_feed_postings/_cursor_predicate.

    #170: `selection_kwargs` is `jobcannon.db._feed.selection_filter_kwargs`'s
    output — the SAME predicate builder `jobcannon/web/onboarding.py`'s
    pre-signup /preview calls against a session-held `pending_picker` dict
    (#169) — so titles/companies/workplace_type filter identically on both
    surfaces by construction, not by two independently-written call sites
    staying in sync by discipline. Taken pre-built (#206) rather than a raw
    `profile` row so `_feed_content_context` (this route's caller) can derive
    it exactly ONCE per request and reuse the same dict for the saved-
    selection indicator/empty-state-reason logic — a `profile` param here
    would let a future edit derive `selection_filter_kwargs(profile)` a
    second time nearby and risk the two calls drifting if one call site ever
    got a stale/different `profile` value.

    `titles`/`companies` have no query-string equivalent on this route (the
    feed's own title/company boxes are free-text `title_contains`/`company`,
    a different filter — see `_feed_query_kwargs`), so they compose
    additively with no collision — see `_feed_empty_reason` for how the
    resulting AND (#206) is surfaced to the visitor rather than silently
    zeroing results. `workplace_type` is the one key both sources can
    supply: an explicit `?workplace_type=` query-string value
    (`_parse_feed_filters` already validated it against the same allowlist)
    always wins, since it reflects what THIS request is asking for right
    now; the profile's saved preference applies only as a fallback when the
    visitor hasn't overridden it, mirroring `_feed_query_kwargs`'s existing
    "any" -> None -> no filter mapping for the same field.

    F1 fix: an absent `?workplace_type=` param and an EXPLICIT
    `?workplace_type=any` both map to `query_kwargs["workplace_type"] is
    None` (see `_feed_query_kwargs`), so `is None` alone can't tell "not
    overridden, fall back to the profile" apart from "overridden to Any,
    clear the profile's filter." `filters["workplace_type_explicit"]`
    (`_parse_feed_filters`) carries that distinction: only fall back to the
    profile's saved preference when the query string genuinely said
    nothing."""
    try:
        with connection_factory() as conn:
            query_kwargs = _effective_query_kwargs(filters, selection_kwargs)
            return list_feed_postings(
                conn,
                user_id=user_id,
                after=after,
                titles=selection_kwargs["titles"],
                companies=selection_kwargs["companies"],
                **query_kwargs,
            )
    except Exception:
        logger.warning(
            "feed postings read failed for user %s (defaulting to empty result set)",
            user_id,
            exc_info=True,
        )
        return []


# singular label -> plural label, for `_count_label`. "company" is the one
# irregular plural among the two count kinds this route ever renders;
# everything else is a bare trailing "s".
_PLURAL_LABELS = {"title": "titles", "company": "companies"}


def _count_label(n: int, singular: str) -> str:
    """`n` and its unit ("title"/"company") -> the grammatically-correct
    display string ("1 title" / "2 titles", "0 companies" / "1 company") —
    the ONE place this grammar is spelled out (runner review finding on
    #226), so `_feed_list.html`'s indicator banner and its collision
    empty-state copy — both of which quote the same saved-selection counts —
    render identical, correctly-pluralized text instead of each hand-rolling
    its own ` titles`/` companies` string concatenation."""
    return f"{n} {singular}" if n == 1 else f"{n} {_PLURAL_LABELS[singular]}"


def _saved_selection_indicator(selection_kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """#206: the feed indicator's ONLY source of "does a saved selection
    apply right now, and how big is it" — reads the same `selection_kwargs`
    dict `_read_feed_postings` filters with, never a second, independent walk
    of `profile["target_titles"]`/`["target_companies"]` that could disagree
    with `_select`'s own present-and-truthy rule (`jobcannon/db/_feed.py`:
    an empty list is already folded to None there). Returns None — not a
    zeroed dict — when neither field is set, so the template's `{% if
    saved_selection %}` gate (Undefined-tolerant the same way `show_actions`/
    `load_more_url` already are elsewhere in this route) renders nothing for
    a fresh profile or one that has just been Cleared. `title_label`/
    `company_label` are the pre-pluralized display strings (`_count_label`);
    `title_count`/`company_count` stay as plain ints for any future non-copy
    consumer."""
    titles = selection_kwargs["titles"] or []
    companies = selection_kwargs["companies"] or []
    if not titles and not companies:
        return None
    return {
        "title_count": len(titles),
        "company_count": len(companies),
        "title_label": _count_label(len(titles), "title"),
        "company_label": _count_label(len(companies), "company"),
    }


def _feed_empty_reason(
    user_id: str, selection_kwargs: dict[str, Any], filters: dict[str, Any]
) -> str:
    """#206: which empty-state copy `_feed_list.html` should render, computed
    from the same two already-derived sources as everything else this route
    does — `selection_kwargs` (never re-walking the profile row by hand) and
    `filters` (never re-reading `request.args` here). "collision" is the
    specific, previously-silent failure #206 reports: an exact-match saved
    title/company selection ANDed with a free-text search this route's own
    `title`/`company` boxes submit, zeroing a result set that would
    otherwise be non-empty for the search alone. `location` is deliberately
    excluded — it has no saved-selection counterpart (`selection_filter_kwargs`
    carries no location field), so a location-only miss is never a
    collision, it's just genuinely zero matching postings.

    Ground truth, not a heuristic (post-review follow-up on #226): a saved
    selection AND a free-text title/company search both being present is
    necessary but not SUFFICIENT for "collision" — two review passes (an
    adversarial refuter's probe P1 and an independent Devin pass) each found
    a real over-promise in the plain has-both-so-it-must-be-a-collision
    heuristic this used to be: (1) the search itself can be a corpus-wide
    miss (saved titles=["Engineer"], search title="Zzzznonexistent" — no
    posting anywhere has that substring, selection or no), and (2) the saved
    selection and the free-text search can target DIFFERENT columns (saved
    titles + a free-text COMPANY search that simply doesn't exist in the
    corpus) — in both cases clearing the saved selection would not produce a
    single additional row, so telling the visitor to Clear is a false
    promise. This function only runs on the zero-result path (its sole
    caller, `_feed_content_context`, only calls it when `entries` is already
    empty) and, ONLY once both flags are set, re-runs `list_feed_postings`
    (the SAME DAL function/predicate builder `_read_feed_postings` uses —
    never a second, independently written predicate) with the saved
    title/company selection dropped (`titles=None, companies=None`) but
    every other filter unchanged (`_effective_query_kwargs`, the same
    workplace_type-fallback resolution the real query used), `limit=1` as a
    pure existence probe. "collision" iff that probe finds a row — i.e. the
    search alone, without the saved selection, DOES match something, so
    clearing the selection would genuinely help. Every other empty cause (an
    actually-thin corpus, a saved-selection-only zero match with no search
    typed, a search that misses on its own regardless of the selection, or
    `_read_feed_postings`'s/this probe's own fail-closed empty list on a DB
    error) falls through to "empty", preserving the pre-#206 flat copy."""
    has_selection = bool(selection_kwargs["titles"] or selection_kwargs["companies"])
    has_search = bool(filters["title"] or filters["company"])
    if not (has_selection and has_search):
        return "empty"
    try:
        with connection_factory() as conn:
            query_kwargs = _effective_query_kwargs(filters, selection_kwargs)
            without_selection = list_feed_postings(
                conn, user_id=user_id, titles=None, companies=None, limit=1, **query_kwargs
            )
        return "collision" if without_selection else "empty"
    except Exception:
        logger.warning(
            "collision probe failed for user %s (defaulting to 'empty')",
            user_id,
            exc_info=True,
        )
        return "empty"


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


def _carry_forward_filters(filters: dict[str, Any]) -> dict[str, Any]:
    """Every `_parse_feed_filters` key whose value differs from its "no
    filter applied" default (`_FILTER_DEFAULTS`), PLUS any key whose matching
    `<key>_explicit` flag is set even when its value equals the default — the
    only one today is `workplace_type_explicit` (F1 fix): an explicit
    `?workplace_type=any` and an absent param render identically ("any" ==
    `_FILTER_DEFAULTS["workplace_type"]`), so without this the override would
    silently vanish from the URL this builds and `_read_feed_postings` would
    revert to the profile's saved filter there. `<key>_explicit` keys
    themselves are excluded from the returned dict — they steer this
    function, they are never a real `pages.feed` query param.

    Extracted from `_feed_load_more_url` (#206) so the Clear control's own
    form action/redirect target (`clear_selection`) carries forward the SAME
    title/company/location/workplace_type/sort values a "Load more" click on
    that same page would — in particular so an explicit `?workplace_type=`
    override survives a Clear round-trip exactly like it survives
    pagination, rather than a second, hand-written carry-forward rule
    risking a divergent (and, per F1's own history, previously-buggy)
    treatment of the explicit-vs-absent distinction."""
    return {
        k: v
        for k, v in filters.items()
        if not k.endswith("_explicit")
        and (v != _FILTER_DEFAULTS.get(k, "") or filters.get(f"{k}_explicit"))
    }


def _feed_load_more_url(filters: dict[str, Any], rows: list[Any]) -> str | None:
    """Next-page URL for the authed feed's "Load more" control, or None when
    this page came back short of FEED_PAGE_MAX — a keyset page shorter than
    the cap proves there is nothing left to seek past (#156's stable-cursor
    requirement: this is a seek, never an OFFSET, so there is no drift
    between the row a click was expecting and the row it gets even if the
    corpus changes between clicks). Carries forward every non-default filter
    value already on this page plus any explicit override (`_carry_forward_filters`),
    plus the cursor derived from the LAST row actually rendered
    (`cursor_from_row`).

    Known limitation, not fixed here: the titles/companies filters embedded
    via the profile are re-derived fresh from the DB on every request, not
    carried in this URL or the keyset cursor. If the visitor's saved
    selections change between this render and the click (e.g. a picker
    resubmission in another tab mid-pagination), a row that newly matches
    and sorts above the page-1 cursor can be skipped on both pages — so a
    "Load more" click is NOT guaranteed to land on the same filter set the
    visitor is currently looking at in that narrow window. Benign (no
    duplication, self-corrects on a fresh page-1 load); a cursor that
    encodes a hash of the filter set would close this but is out of scope
    here."""
    if len(rows) < _feed.FEED_PAGE_MAX:
        return None
    return url_for(
        "pages.feed", **_carry_forward_filters(filters), **_feed.cursor_from_row(rows[-1])
    )


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


def _feed_content_context(
    user_id: str,
    profile: Any,
    filters: dict[str, Any],
    after: tuple[float | None, Any, int] | None = None,
) -> dict[str, Any]:
    """#206: everything `_feed_content.html` (indicator/Clear control, filter
    form, entries, "Load more", empty-state copy) needs for one render,
    derived from exactly one `selection_filter_kwargs(profile)` call —
    shared by `feed()`'s full-page GET and `clear_selection()`'s own
    HX-Request fragment response so both agree on "what does this feed look
    like right now" by construction, mirroring `jobcannon/web/onboarding.py`'s
    `preview()` computing its own `selection_kwargs` once and reusing it for
    both the read and `has_selections` (#169). Returns the SAME defaults
    `feed()` used inline before this helper existed (empty entries,
    unranked ordering, no "Load more", no indicator, "empty" reason) when
    `profile is None` — the no-profile branch never reaches this function in
    practice (both callers already guard on it), but a defensive default
    here costs nothing and avoids a `NoneType` selection_filter_kwargs call
    if that guard is ever loosened."""
    if profile is None:
        return {
            "entries": [],
            "ordering": {"personalized": False, "ranker_version": UNRANKED_VERSION},
            "load_more_url": None,
            "saved_selection": None,
            "empty_reason": "empty",
        }
    selection_kwargs = _feed.selection_filter_kwargs(profile)
    saved_selection = _saved_selection_indicator(selection_kwargs)
    rows = _read_feed_postings(
        user_id=user_id, filters=filters, selection_kwargs=selection_kwargs, after=after
    )
    entries = [build_entry(row, profile) for row in rows]
    ordering = _ordering_label(rows)
    _log_impressions(user_id, rows)
    load_more_url = _feed_load_more_url(filters, rows)
    empty_reason = "empty" if entries else _feed_empty_reason(user_id, selection_kwargs, filters)
    return {
        "entries": entries,
        "ordering": ordering,
        "load_more_url": load_more_url,
        "saved_selection": saved_selection,
        "empty_reason": empty_reason,
    }


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

    content = {
        "entries": [],
        "ordering": {"personalized": False, "ranker_version": UNRANKED_VERSION},
        "load_more_url": None,
        "saved_selection": None,
        "empty_reason": "empty",
    }
    if profile is not None and stats.get("postings", 0) > 0:
        content = _feed_content_context(user_id, profile, filters, after)

    if request.headers.get("HX-Request") == "true":
        return render_template(
            "_feed_page.html",
            entries=content["entries"],
            load_more_url=content["load_more_url"],
            show_actions=True,
        )

    return render_template(
        "feed.html",
        stats=stats,
        profile=profile,
        has_selections=has_selections,
        filters=filters,
        workplace_types=_WORKPLACE_TYPES,
        show_actions=True,
        clear_selection_url=url_for("pages.clear_selection", **_carry_forward_filters(filters)),
        **content,
    )


@pages_bp.post("/feed/clear-selection", strict_slashes=False)
def clear_selection():
    """#206: the recoverability half of the saved-selection AND-collision
    fix (`_feed_empty_reason`/`_saved_selection_indicator` above are the
    discoverability half). PRODUCT DECISION: this clears the saved
    titles/companies selection, it does NOT change the AND relationship
    between a saved selection and a free-text search — that stays additive
    filtering, unchanged, for every profile that has not clicked Clear.

    Not in `PUBLIC_PATHS` (`jobcannon/web/__init__.py`), so `clerk_auth`'s
    `before_request` gate already 401s an anonymous POST here before this
    function body ever runs — the same guard `jobcannon/web/actions.py`'s
    save/dismiss/apply/undo-apply routes rely on, no separate check needed
    (`tests/host/test_routing_errors.py::test_gate_covers_every_registered_route_for_every_declared_method`
    proves this holds for every route the app registers, including this
    one, with no per-route addition required there). CSRF-protected the
    same way every other state-changing route in this app is: Flask-WTF's
    `CSRFProtect(app)` is global (`jobcannon/web/__init__.py`) and this
    blueprint is not `csrf.exempt`-ed, so a request carrying neither the
    hidden `csrf_token` form field nor the `X-CSRFToken` header 400s before
    reaching here too.

    `clear_profile_targets(conn, user_id)` (`jobcannon/db/_profiles.py`,
    #228) is what actually clears the stored selection — ONE `UPDATE ...
    RETURNING` statement that zeroes `target_titles`/`target_companies` to
    literal empty lists (never `None` — `_profiles.py`'s COALESCE-preserve
    columns treat an omitted/None argument as "leave the old value," the
    same distinction `jobcannon/web/onboarding.py`'s picker resubmission
    already relies on, #169) and never names `workplace_type` at all.
    Before #228 this route round-tripped `workplace_type` through a
    `get_profile` read and an `upsert_profile` write to satisfy that
    function's required (non-COALESCE, m0012) kwarg without actually
    intending to touch the column — the read and the write were two
    separate statements with no lock between them, so a concurrent
    `upsert_profile(..., workplace_type=...)` commit in that window (e.g.
    the picker resubmitting from a second tab) was silently reverted by the
    stale value this route wrote back. `clear_profile_targets` closes that
    window structurally: its SET clause has nothing to read stale, so there
    is nothing for a concurrent writer to race against
    (`jobcannon/db/_profiles.py`'s module docstring has the full design
    rationale, including why loosening `upsert_profile` itself was
    rejected instead).

    No-profile-row guard (Devin review, #226): a signed-in user who has
    never completed the picker has no `profiles` row (`upsert_profile` —
    `jobcannon/db/_profiles.py` — is the only row-creating writer), and this
    route is reachable pre-onboarding regardless — `base.html` puts a valid
    CSRF token on every authed page's `<body>` via `hx-headers`,
    Clear-button-visibility notwithstanding. Writing a zeroed `profiles` row
    for that visitor (the pre-#226 behavior) would flip `feed.html`'s
    `has_selections = profile is not None` gate true, silently skipping the
    "Set up your feed" onboarding CTA (`feed.html:4`) for someone who never
    ran the picker. `clear_profile_targets` has no INSERT arm (see its own
    docstring), so a `profile is None` visitor's UPDATE matches zero rows
    and returns None on its own — no separate existence check needed here,
    and no phantom row is ever created either way.

    HX-aware like every other mutation route in this codebase
    (`jobcannon/web/actions.py`, `jobcannon/web/onboarding.py`'s
    `start_submit`): an HX-Request (the Clear button's own `hx-post`) gets
    `_feed_content.html` re-rendered at 200 — the SAME wrapper `feed.html`
    itself includes, so the swap lands on a live `#feed-content` anchor and
    the response carries a fresh copy of that same anchor for any later
    swap (see `_feed_content.html`'s own docstring, mirroring
    `_feed_page.html`'s self-perpetuating "Load more" pattern). A plain
    form POST (no JS) gets a 303 redirect back to the feed, carrying
    forward whatever title/company/location/workplace_type/sort values were
    on the request that rendered the Clear control (`_carry_forward_filters`,
    baked into the form's own `action`/`hx-post` URL by `feed()` /
    `_feed_content_context`'s callers) — so the free-text search that
    triggered the "collision" empty-state in the first place survives the
    round trip instead of being dropped along with the saved selection."""
    user_id = g.clerk_user.user_id
    with connection_factory() as conn:
        profile = clear_profile_targets(conn, user_id)

    filters = _parse_feed_filters(request.args)

    if request.headers.get("HX-Request") == "true":
        content = _feed_content_context(user_id, profile, filters)
        return render_template(
            "_feed_content.html",
            filters=filters,
            workplace_types=_WORKPLACE_TYPES,
            show_actions=True,
            clear_selection_url=url_for("pages.clear_selection", **_carry_forward_filters(filters)),
            **content,
        ), 200

    return redirect(url_for("pages.feed", **_carry_forward_filters(filters)), code=303)


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

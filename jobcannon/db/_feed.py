"""Feed read DAL — the first postings-list query in this repo's history and
the first `feed_state` read. Read-only: `feed_state` has no writer anywhere
in this codebase yet (there is no rank producer), so every row's
`rank_score`/`ranker_version` comes back NULL until a future PR ships one.
The default ordering is written for that state first, not as a fallback —
`tests/host/test_feed_state_not_written.py` guards this module (and every
other module under the scanned roots) never writing to `feed_state`.

An authenticated reader (`user_id` not None) never sees a posting they have
dismissed: `list_feed_postings` LEFT JOINs `pipeline_status` and excludes
`status = 'dismissed'` rows. Every row also carries a `saved` flag (whether a
`watchlists` row exists for that `(user_id, posting_id)` pair) so
`jobcannon/web/feed_entries.py::build_entry` can render per-user state
without a second query. Both tables are written exclusively by
`jobcannon/db/_user_actions.py`; this module only reads them.

Row access: STRING-KEY only, matching every other DAL module in this
package (`_profiles.py`, `_stats.py`, `_companies.py`) — both the pooled
`HybridRow` and the test fixtures' `dict_row` support `row["col"]`.

Anonymous vs. authenticated shape parity: the `feed_state` LEFT JOIN is
built into the query text only when `user_id` is not None (an anonymous
reader has no rank to look up, so there is no reason to join a per-user
table for every row of a shared corpus). Both branches still alias their
rank columns to the same output names (`rank_score`, `ranker_version`), and
`_SORTS` fragments reference those bare output aliases rather than a
table-qualified column — PostgreSQL resolves `ORDER BY <alias>` against the
SELECT list, so the identical fragment works whether or not `fs` is present
in the FROM clause. That is what makes
`test_anonymous_and_authed_shapes_return_identical_columns` true by
construction instead of by coincidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

FEED_PAGE_MAX = 25

# Public sort token -> a fixed, code-owned ORDER BY fragment. No user-supplied
# string ever reaches ORDER BY; an unknown token raises ValueError instead of
# being interpolated. Fragments reference SELECT-list output aliases (see
# module docstring) so the same fragment is valid for both the anonymous and
# authenticated query shapes.
_SORTS: dict[str, str] = {
    "default": "rank_score DESC NULLS LAST, last_seen DESC NULLS LAST, id DESC",
}

# Column order is fixed here and mirrored by the anonymous branch so both
# shapes return identical columns. direct_url is
# deliberately omitted: it is unconditionally NULL for every posting
# (jobcannon/db/_jobs.py's INSERT always writes None there), so source_urls /
# sightings are the real provenance fields. employment_type / is_remote /
# department / comp_data_json are also omitted: they are insert-only and
# platform-dependent (jobcannon/engine/ats_scanner/_run.py), so NULL there
# means "unknown", never "confirmed absent" — 1C does not filter or render on
# them.
_SELECT_COLUMNS = (
    "p.id, p.title, p.company, p.location, p.workplace_type, "
    "p.salary_min, p.salary_max, p.salary_currency, p.salary_period, "
    "p.posted_date, p.posted_date_precision, p.last_seen, p.ats_platform, "
    "p.structural_axes, p.source_urls, p.sightings"
)


def _escape_like(value: str) -> str:
    """Escape LIKE metacharacters (`\\`, `%`, `_`) so a substring filter
    matches literally. Paired with `LIKE %s ESCAPE '\\'` at the call site —
    without this, a user typing `%` in the location box would match every
    row instead of rows literally containing a percent sign."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _cursor_predicate(
    rank_expr: str, after: tuple[float | None, datetime, int] | None
) -> tuple[str, list[Any]]:
    """The keyset "seek" WHERE fragment for `_SORTS["default"]`'s own three
    columns (rank_score, last_seen, id) -- never a second, independently
    invented cursor shape, so pagination can never drift out of sync with
    that ORDER BY. `after` is (rank_score, last_seen, id) taken from a
    previous page's last row (`cursor_from_row`), or None for a first page
    (returns an empty fragment). `rank_expr` is the RAW SQL expression for
    rank_score -- `fs.rank_score` on the authed branch, a literal
    `NULL::double precision` on the anonymous one -- because WHERE cannot
    reference the SELECT list's output alias the way ORDER BY can; the two
    branches alias to the same output name but need their own raw
    expression here.

    `last_seen` is `timestamptz NOT NULL` (m0001), so only `rank_score`
    needs NULLS-LAST handling: COALESCE to '-infinity' emulates "NULLS
    LAST" in a DESC ordering by substituting a real, storable
    double-precision sentinel that sorts behind every other *finite*
    value, which a plain `<` seek predicate cannot express on a nullable
    column by itself. This is NOT exactly equivalent to NULLS LAST if a
    row ever stores a real `rank_score = -Infinity` (a valid double
    precision value): it would then collide with the NULL sentinel and
    could be skipped on a page boundary. `feed_state` has no writer
    anywhere in this codebase yet (guarded by test_feed_state_not_written.py),
    so this can't occur today — tracked as a follow-up for whenever a
    ranker writer lands (would need a separate `IS NULL` flag in the row
    constructor rather than a shared sentinel). Both sides of the row
    comparison are COALESCEd, not just the column: `after`'s rank_score is
    NULL on every real page today, and a PostgreSQL row-constructor
    comparison treats ANY NULL element pair as unknown and drops the row,
    so COALESCEing only the column side would silently return zero rows
    on every "load more" click. The explicit `::double precision` cast on
    the parameter is required: an untyped NULL inside COALESCE raises
    "could not determine data type"."""
    if after is None:
        return "", []
    after_rank_score, after_last_seen, after_id = after
    clause = (
        f"(COALESCE({rank_expr}, '-infinity'::double precision), p.last_seen, p.id) "
        "< (COALESCE(%s::double precision, '-infinity'::double precision), %s, %s)"
    )
    return clause, [after_rank_score, after_last_seen, after_id]


def cursor_from_row(row: Any) -> dict[str, str]:
    """The next page's keyset cursor as URL query params, derived from the
    LAST row of the page just rendered -- never computed independently of
    an actual returned row, so a cursor can never point somewhere the sort
    key didn't actually visit. Consumed by `parse_cursor` (round-trips
    through a plain query string, not a signed/opaque token: every value
    here is one this same row already rendered to the viewer, so there is
    nothing a tampered cursor could expose that page 1 didn't already
    show -- at worst a malformed value degrades to `parse_cursor` treating
    it as no cursor, never a 500)."""
    rank_score = row["rank_score"]
    last_seen = row["last_seen"]
    return {
        "cursor_rank_score": "" if rank_score is None else repr(float(rank_score)),
        "cursor_last_seen": last_seen.isoformat(),
        "cursor_id": str(row["id"]),
    }


def parse_cursor(args: Any) -> tuple[float | None, datetime, int] | None:
    """Query params (a Flask `request.args`-shaped mapping) -> the `after`
    tuple `list_feed_postings` accepts, or None for "no cursor" (render a
    first page). A cursor value that fails to *parse* (non-numeric id,
    non-ISO timestamp, non-float rank score) degrades to None rather than
    raising -- the caller gets a fresh first page instead of a 500, the
    same fail-open discipline jobcannon/web/pages.py's other query-param
    parsing already uses. This function only guards parseability, not SQL
    validity: a value that parses but is out of range for the DB column
    (e.g. an id wider than bigint) or is a float special value `float()`
    accepts but SQL rejects (`nan`, `inf`) passes this function and is
    instead caught by the caller's broad `except Exception` around the DB
    call, which degrades to an *empty* batch rather than a fresh first
    page -- still fail-closed (no 500, no data leak), just a different
    empty state than "no cursor" produces."""
    raw_id = (args.get("cursor_id") or "").strip()
    if not raw_id:
        return None
    try:
        cursor_id = int(raw_id)
    except ValueError:
        return None

    raw_last_seen = (args.get("cursor_last_seen") or "").strip()
    if not raw_last_seen:
        return None
    try:
        last_seen = datetime.fromisoformat(raw_last_seen)
    except ValueError:
        return None

    raw_rank = (args.get("cursor_rank_score") or "").strip()
    rank_score: float | None = None
    if raw_rank:
        try:
            rank_score = float(raw_rank)
        except ValueError:
            return None

    return (rank_score, last_seen, cursor_id)


def _build_filters(
    *,
    titles: list[str] | None,
    title_contains: str | None,
    workplace_type: str | None,
    location_contains: str | None,
    company: str | None,
    companies: list[str] | None = None,
    posting_id: int | None = None,
) -> tuple[list[str], list[Any]]:
    """Parameterized WHERE fragments + bound params for `list_feed_postings`,
    factored out as its own function so any future second caller of the same
    filter set builds it identically rather than re-deriving it. Every
    filter value is a bound parameter; only ORDER BY is ever built from the
    `_SORTS` allowlist.

    `titles` (exact-match, `= ANY(%s)`) and `title_contains` (substring,
    `LIKE`) are two distinct callers, not two spellings of the same filter:
    `titles` serves the picker's structured selections
    (jobcannon/web/onboarding.py), where the value is one of a fixed set of
    corpus-derived strings and exact match is correct; `title_contains`
    serves the authed feed's free-text title box (jobcannon/web/pages.py),
    where a user is typing a fragment. Both may be passed at once without
    conflict — they AND together like every other filter here.

    `companies` (exact-match list, `= ANY(%s)`) is `titles`'s twin for the
    picker's multi-select company field (#169) — same corpus-derived-string,
    exact-match rationale. `company` (singular, exact-match on ONE string) is
    a pre-existing, unrelated caller: the authed feed's free-text company box
    (jobcannon/web/pages.py's `_feed_query_kwargs`), which submits one typed
    value, not a pick from a fixed set. Despite both being exact-match (not
    substring, unlike the titles/title_contains split), they stay two
    separate parameters rather than one that accepts either a str or a list:
    `companies` MUST support zero-or-more selections (an empty picker
    selection means "no filter", not "match nothing"), a shape `company`'s
    single-string-or-None contract cannot express without the caller
    special-casing an empty list into None itself — pushing that
    normalization into `selection_filter_kwargs` (below) instead of in two
    places. Both may be passed at once — they AND together like every other
    filter here.

    `posting_id` narrows to exactly one posting. It exists so
    `jobcannon/web/actions.py` can re-fetch the single row it just mutated
    through this SAME query — dismissed-exclusion and the `saved` flag
    (`list_feed_postings`'s authed branch) apply identically whether the
    caller wants a full page or one row, rather than a second,
    independently-maintained "what does this user see for posting X" query.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if titles:
        clauses.append("p.title = ANY(%s)")
        params.append(list(titles))
    if title_contains is not None:
        clauses.append("p.title LIKE %s ESCAPE '\\'")
        params.append(f"%{_escape_like(title_contains)}%")
    if workplace_type is not None:
        clauses.append("p.workplace_type = %s")
        params.append(workplace_type)
    if company is not None:
        clauses.append("p.company = %s")
        params.append(company)
    if companies:
        clauses.append("p.company = ANY(%s)")
        params.append(list(companies))
    if location_contains is not None:
        clauses.append("p.location LIKE %s ESCAPE '\\'")
        params.append(f"%{_escape_like(location_contains)}%")
    if posting_id is not None:
        clauses.append("p.id = %s")
        params.append(posting_id)
    return clauses, params


def _select(mapping: Any, *keys: str) -> Any:
    """Bracket-access lookup trying each key in order, returning the first
    present-and-truthy value, or None if every key is absent/empty — the
    same STRING-KEY-only discipline this module's docstring requires (a
    pooled `HybridRow` has no `.get()`). Mirrors
    `jobcannon/web/why.py`'s `_selection_tokens` dual-key-reading idiom for
    exactly the same reason: `selection_filter_kwargs` below reads either a
    session-held `pending_picker` dict or a `profiles` row."""
    for key in keys:
        try:
            value = mapping[key]
        except (KeyError, IndexError):
            continue
        if value:
            return value
    return None


def selection_filter_kwargs(selections_or_profile: Any) -> dict[str, Any]:
    """The ONE selections -> `list_feed_postings` filter-kwargs derivation
    (#169/#170), shared by `jobcannon/web/onboarding.py`'s pre-signup
    /preview (a session-held `pending_picker` dict, keys `titles`/
    `companies`/`workplace_type`) and `jobcannon/web/pages.py`'s authed feed
    (a `profiles` row, keys `target_titles`/`target_companies`/
    `workplace_type`) — a single point of enforcement so the two surfaces
    can never diverge on what "your selections" means. Accepts either shape
    via `_select`'s dual-key reads; `workplace_type` uses the same key on
    both shapes (already an uppercase DB token or None for "any" —
    `jobcannon/web/onboarding.py`'s `_WORKPLACE_FILTERS` — so it is passed
    through unchanged, never re-mapped here). `None`/`{}` input (no pending
    picker, no profile row) returns every key as None, i.e. no filter."""
    m = selections_or_profile or {}
    return {
        "titles": _select(m, "titles", "target_titles"),
        "companies": _select(m, "companies", "target_companies"),
        "workplace_type": _select(m, "workplace_type"),
    }


def list_feed_postings(
    conn: Any,
    *,
    user_id: str | None = None,
    titles: list[str] | None = None,
    title_contains: str | None = None,
    workplace_type: str | None = None,
    location_contains: str | None = None,
    company: str | None = None,
    companies: list[str] | None = None,
    posting_id: int | None = None,
    sort: str = "default",
    limit: int = FEED_PAGE_MAX,
    offset: int = 0,
    after: tuple[float | None, datetime, int] | None = None,
) -> list[Any]:
    """Stored values only — no score, label, or classification is computed
    here. `structural_axes` may come back NULL for a real, existing posting
    (the axes batch cap processes 500 rows per scan tick, so a large
    pre-seed leaves a transient NULL slice); callers must handle that, this
    function does not filter it out.

    An authed reader (`user_id` not None) never sees a posting whose
    `pipeline_status.status = 'dismissed'` — that exclusion clause is added
    only on this branch (not folded into `_build_filters`) because it
    depends on `user_id` and the anonymous branch has no per-user row to
    exclude by.

    `after` (#156) is a keyset cursor — the `(rank_score, last_seen, id)`
    tuple `cursor_from_row` derived from a previous page's last row, or None
    for a first page. It is only meaningful against `_SORTS["default"]`'s
    own ordering (see `_cursor_predicate`'s docstring), so passing a cursor
    against any other sort token raises rather than silently seeking through
    rows in an order the cursor was never computed against — today `_SORTS`
    has exactly one token, so this only guards a future second one. `after`
    and a nonzero `offset` are mutually exclusive: `OFFSET` is kept only so
    non-cursor callers (existing callers of this function, pre-#156) are
    unaffected, but combining it with `after` would silently skip rows past
    the seek point — the exact drift keyset pagination exists to avoid — so
    that combination raises rather than being allowed to compose."""
    if sort not in _SORTS:
        raise ValueError(f"unknown sort token: {sort!r}")
    if after is not None and sort != "default":
        raise ValueError(f"cursor pagination is only defined for sort='default', got {sort!r}")
    if after is not None and offset != 0:
        raise ValueError("cursor pagination (after=...) cannot be combined with offset != 0")
    order_by = _SORTS[sort]
    limit = max(0, min(limit, FEED_PAGE_MAX))

    where_clauses, params = _build_filters(
        titles=titles,
        title_contains=title_contains,
        workplace_type=workplace_type,
        location_contains=location_contains,
        company=company,
        companies=companies,
        posting_id=posting_id,
    )

    raw = conn.raw if hasattr(conn, "raw") else conn

    if user_id is not None:
        cursor_sql, cursor_params = _cursor_predicate("fs.rank_score", after)
        authed_where_clauses = [*where_clauses, "(ps.status IS DISTINCT FROM 'dismissed')"]
        if cursor_sql:
            authed_where_clauses.append(cursor_sql)
        where_sql = f"WHERE {' AND '.join(authed_where_clauses)}"
        sql = (
            f"SELECT {_SELECT_COLUMNS}, "
            "fs.rank_score AS rank_score, fs.ranker_version AS ranker_version, "
            "(w.id IS NOT NULL) AS saved "
            "FROM postings p "
            "LEFT JOIN feed_state fs ON fs.user_id = %s AND fs.posting_id = p.id "
            "LEFT JOIN pipeline_status ps ON ps.user_id = %s AND ps.posting_id = p.id "
            "LEFT JOIN watchlists w ON w.user_id = %s AND w.posting_id = p.id "
            f"{where_sql} "
            f"ORDER BY {order_by} "
            "LIMIT %s OFFSET %s"
        )
        query_params = [user_id, user_id, user_id, *params, *cursor_params, limit, offset]
    else:
        cursor_sql, cursor_params = _cursor_predicate("NULL::double precision", after)
        all_where = [*where_clauses, cursor_sql] if cursor_sql else where_clauses
        where_sql = f"WHERE {' AND '.join(all_where)}" if all_where else ""
        sql = (
            f"SELECT {_SELECT_COLUMNS}, "
            "NULL::double precision AS rank_score, NULL::text AS ranker_version, "
            "NULL::boolean AS saved "
            "FROM postings p "
            f"{where_sql} "
            f"ORDER BY {order_by} "
            "LIMIT %s OFFSET %s"
        )
        query_params = [*params, *cursor_params, limit, offset]

    return raw.execute(sql, query_params).fetchall()


def _distinct_matching(raw: Any, column: str, q: str | None, limit: int) -> list[str]:
    """Shared body for `distinct_titles`/`distinct_companies` (#148): with no
    `q`, identical to their pre-#148 behavior (alphabetical, unfiltered,
    capped at `limit`) — every existing caller's contract is unchanged. With
    `q`, ILIKE substring-matches `column` (case-insensitive, LIKE
    metacharacters escaped the same way `_build_filters` already escapes
    them for `title_contains`/`location_contains`) and ranks prefix matches
    ahead of non-prefix matches, then alphabetically within each group — a
    corpus that skews heavily toward a handful of alphabetically-early
    prefixes (the bug #148 reports) no longer buries a real target title
    outside the `limit`-item window as long as it's actually searched for.

    `column` is always one of the two fixed literals its two call sites pass
    ("title" / "company") — never request-derived — so interpolating it
    directly into the SQL text is the same trusted, code-owned pattern
    `_SORTS` already uses for `ORDER BY`, not a SQL-injection surface. The
    inner `SELECT DISTINCT` is wrapped in a subquery specifically so the
    outer `ORDER BY` can reference the prefix-match expression: PostgreSQL
    requires every `ORDER BY` expression on a `SELECT DISTINCT` to appear in
    its own select list, and `column ILIKE prefix_pattern` is not `column`
    itself."""
    if not q:
        rows = raw.execute(
            f"SELECT DISTINCT {column} FROM postings ORDER BY {column} LIMIT %s", (limit,)
        ).fetchall()
        return [row[column] for row in rows]
    escaped = _escape_like(q)
    contains_pattern = f"%{escaped}%"
    prefix_pattern = f"{escaped}%"
    rows = raw.execute(
        f"SELECT {column} FROM ("
        f"SELECT DISTINCT {column} FROM postings WHERE {column} ILIKE %s ESCAPE '\\'"
        f") matched "
        f"ORDER BY ({column} ILIKE %s ESCAPE '\\') DESC, {column} ASC "
        "LIMIT %s",
        (contains_pattern, prefix_pattern, limit),
    ).fetchall()
    return [row[column] for row in rows]


def distinct_titles(conn: Any, *, q: str | None = None, limit: int = 50) -> list[str]:
    """Bounded, corpus-derived option source for the picker's title field —
    options are derived from the corpus at read time, never a hardcoded list,
    so the picker cannot drift from what the database actually contains.
    `q` (#148) case-insensitively substring-filters and ranks by
    prefix-match first — see `_distinct_matching`."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    return _distinct_matching(raw, "title", q, limit)


def distinct_companies(conn: Any, *, q: str | None = None, limit: int = 50) -> list[str]:
    """Same contract as `distinct_titles`, for the company field (#148)."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    return _distinct_matching(raw, "company", q, limit)

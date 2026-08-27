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
    needs NULLS-LAST handling: COALESCE to '-infinity' is exactly
    equivalent to "NULLS LAST" in a DESC ordering (a real, storable
    double-precision value that sorts behind every other value), which a
    plain `<` seek predicate cannot express on a nullable column by
    itself. Both sides of the row comparison are COALESCEd, not just the
    column: `feed_state` has no writer anywhere in this codebase yet
    (guarded by test_feed_state_not_written.py), so `after`'s rank_score
    is NULL on every real page today -- a PostgreSQL row-constructor
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
    first page). A malformed or tampered cursor (non-numeric id, non-ISO
    timestamp) degrades to None rather than raising -- the caller gets a
    fresh first page instead of a 500, the same fail-open discipline
    jobcannon/web/pages.py's other query-param parsing already uses."""
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
    posting_id: int | None = None,
) -> tuple[list[str], list[Any]]:
    """Parameterized WHERE fragments + bound params, shared by
    `list_feed_postings` and `count_feed_postings` so the two queries can
    never drift out of sync. Every filter value is a bound parameter; only
    ORDER BY is ever built from the `_SORTS` allowlist.

    `titles` (exact-match, `= ANY(%s)`) and `title_contains` (substring,
    `LIKE`) are two distinct callers, not two spellings of the same filter:
    `titles` serves the picker's structured selections
    (jobcannon/web/onboarding.py), where the value is one of a fixed set of
    corpus-derived strings and exact match is correct; `title_contains`
    serves the authed feed's free-text title box (jobcannon/web/pages.py),
    where a user is typing a fragment. Both may be passed at once without
    conflict — they AND together like every other filter here.

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
    if location_contains is not None:
        clauses.append("p.location LIKE %s ESCAPE '\\'")
        params.append(f"%{_escape_like(location_contains)}%")
    if posting_id is not None:
        clauses.append("p.id = %s")
        params.append(posting_id)
    return clauses, params


def list_feed_postings(
    conn: Any,
    *,
    user_id: str | None = None,
    titles: list[str] | None = None,
    title_contains: str | None = None,
    workplace_type: str | None = None,
    location_contains: str | None = None,
    company: str | None = None,
    posting_id: int | None = None,
    sort: str = "default",
    limit: int = FEED_PAGE_MAX,
    offset: int = 0,
) -> list[Any]:
    """Stored values only — no score, label, or classification is computed
    here. `structural_axes` may come back NULL for a real, existing posting
    (the axes batch cap processes 500 rows per scan tick, so a large
    pre-seed leaves a transient NULL slice); callers must handle that, this
    function does not filter it out.

    An authed reader (`user_id` not None) never sees a posting whose
    `pipeline_status.status = 'dismissed'` — that exclusion clause is added
    only on this branch, never shared with `count_feed_postings` via
    `_build_filters`, because it depends on `user_id` and the anonymous
    branch has no per-user row to exclude by."""
    if sort not in _SORTS:
        raise ValueError(f"unknown sort token: {sort!r}")
    order_by = _SORTS[sort]
    limit = max(0, min(limit, FEED_PAGE_MAX))

    where_clauses, params = _build_filters(
        titles=titles,
        title_contains=title_contains,
        workplace_type=workplace_type,
        location_contains=location_contains,
        company=company,
        posting_id=posting_id,
    )

    raw = conn.raw if hasattr(conn, "raw") else conn

    if user_id is not None:
        authed_where_clauses = [*where_clauses, "(ps.status IS DISTINCT FROM 'dismissed')"]
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
        query_params = [user_id, user_id, user_id, *params, limit, offset]
    else:
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        sql = (
            f"SELECT {_SELECT_COLUMNS}, "
            "NULL::double precision AS rank_score, NULL::text AS ranker_version, "
            "NULL::boolean AS saved "
            "FROM postings p "
            f"{where_sql} "
            f"ORDER BY {order_by} "
            "LIMIT %s OFFSET %s"
        )
        query_params = [*params, limit, offset]

    return raw.execute(sql, query_params).fetchall()


def count_feed_postings(
    conn: Any,
    *,
    titles: list[str] | None = None,
    title_contains: str | None = None,
    workplace_type: str | None = None,
    location_contains: str | None = None,
    company: str | None = None,
) -> int:
    """Same filters as `list_feed_postings`, for the "N matches" line.
    `user_id` is deliberately not a parameter here: the `feed_state` join is
    a LEFT JOIN keyed on the shared corpus, so it never changes which rows
    match, only what their `rank_score`/`ranker_version` columns read — a
    count is identical whether or not that join is present."""
    where_clauses, params = _build_filters(
        titles=titles,
        title_contains=title_contains,
        workplace_type=workplace_type,
        location_contains=location_contains,
        company=company,
    )
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(f"SELECT COUNT(*) AS n FROM postings p {where_sql}", params).fetchone()
    return row["n"]


def distinct_titles(conn: Any, *, limit: int = 50) -> list[str]:
    """Bounded, corpus-derived option source for the picker's title field —
    options are derived from the corpus at read time, never a hardcoded list,
    so the picker cannot drift from what the database actually contains."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    rows = raw.execute(
        "SELECT DISTINCT title FROM postings ORDER BY title LIMIT %s", (limit,)
    ).fetchall()
    return [row["title"] for row in rows]


def distinct_companies(conn: Any, *, limit: int = 50) -> list[str]:
    """Bounded, corpus-derived option source for the picker's company field —
    options are derived from the corpus at read time, never a hardcoded list,
    so the picker cannot drift from what the database actually contains."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    rows = raw.execute(
        "SELECT DISTINCT company FROM postings ORDER BY company LIMIT %s", (limit,)
    ).fetchall()
    return [row["company"] for row in rows]

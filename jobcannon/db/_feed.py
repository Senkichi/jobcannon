"""Feed read DAL — the first postings-list query in this repo's history and
the first `feed_state` read. Read-only: `feed_state` has no writer anywhere
in this codebase yet (there is no rank producer), so every row's
`rank_score`/`ranker_version` comes back NULL until a future PR ships one.
The default ordering is written for that state first, not as a fallback —
`tests/host/test_feed_state_not_written.py` guards this module (and every
other module under the scanned roots) never writing to `feed_state`.

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

# Column order matches the brief's selected-column list exactly. direct_url is
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


def _build_filters(
    *,
    titles: list[str] | None,
    workplace_type: str | None,
    location_contains: str | None,
    company: str | None,
) -> tuple[list[str], list[Any]]:
    """Parameterized WHERE fragments + bound params, shared by
    `list_feed_postings` and `count_feed_postings` so the two queries can
    never drift out of sync. Every filter value is a bound parameter; only
    ORDER BY is ever built from the `_SORTS` allowlist."""
    clauses: list[str] = []
    params: list[Any] = []
    if titles:
        clauses.append("p.title = ANY(%s)")
        params.append(list(titles))
    if workplace_type is not None:
        clauses.append("p.workplace_type = %s")
        params.append(workplace_type)
    if company is not None:
        clauses.append("p.company = %s")
        params.append(company)
    if location_contains is not None:
        clauses.append("p.location LIKE %s ESCAPE '\\'")
        params.append(f"%{_escape_like(location_contains)}%")
    return clauses, params


def list_feed_postings(
    conn: Any,
    *,
    user_id: str | None = None,
    titles: list[str] | None = None,
    workplace_type: str | None = None,
    location_contains: str | None = None,
    company: str | None = None,
    sort: str = "default",
    limit: int = FEED_PAGE_MAX,
    offset: int = 0,
) -> list[Any]:
    """Stored values only — no score, label, or classification is computed
    here. `structural_axes` may come back NULL for a real, existing posting
    (the axes batch cap processes 500 rows per scan tick, so a large
    pre-seed leaves a transient NULL slice); callers must handle that, this
    function does not filter it out."""
    if sort not in _SORTS:
        raise ValueError(f"unknown sort token: {sort!r}")
    order_by = _SORTS[sort]
    limit = max(0, min(limit, FEED_PAGE_MAX))

    where_clauses, params = _build_filters(
        titles=titles,
        workplace_type=workplace_type,
        location_contains=location_contains,
        company=company,
    )
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    raw = conn.raw if hasattr(conn, "raw") else conn

    if user_id is not None:
        sql = (
            f"SELECT {_SELECT_COLUMNS}, "
            "fs.rank_score AS rank_score, fs.ranker_version AS ranker_version "
            "FROM postings p "
            "LEFT JOIN feed_state fs ON fs.user_id = %s AND fs.posting_id = p.id "
            f"{where_sql} "
            f"ORDER BY {order_by} "
            "LIMIT %s OFFSET %s"
        )
        query_params = [user_id, *params, limit, offset]
    else:
        sql = (
            f"SELECT {_SELECT_COLUMNS}, "
            "NULL::double precision AS rank_score, NULL::text AS ranker_version "
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

"""tests/host/test_crawl_batch_pg_predicates.py — Postgres-dialect regression
for crawl_careers_batch's two cohort-selection lanes (issue #380).

``jobcannon/engine/careers_crawler/__init__.py``'s ``crawl_careers_batch``
(L-0461, landed via #369) wrote its two lane SELECTs in SQLite dialect
without going through any translation seam for two constructs:

- ``ats_probe_status IS NOT 'hit'`` — SQLite's ``IS NOT`` accepts any
  right-hand expression; Postgres's ``IS [NOT]`` only accepts
  ``{TRUE,FALSE,UNKNOWN,NULL}``, so ``IS NOT 'hit'`` is a syntax error on
  Postgres, not merely wrong at runtime (see
  ``test_ats_probe_status_is_not_hit_would_have_raised_syntax_error_on_postgres``
  below for the positive control).
- ``datetime('now', ? || ' days')``, bound with ``(f"-{freshness_days}",)``
  — a shape ``db/compat.py``'s ``_DATETIME_REWRITES`` does not match (the
  regex requires the literal ``-`` baked into the SQL text, not folded
  into the bound parameter), so it reaches Postgres as a literal
  ``datetime(...)`` call, which does not exist there (see
  ``test_datetime_now_days_param_would_not_have_been_translated_on_postgres``
  below).

Both are fixed at their call sites in ``__init__.py``: the predicate now
reads ``IS DISTINCT FROM 'hit'`` (dialect-safe natively on both SQLite
3.39+ and Postgres, zero compat.py change needed), and the datetime shape
now matches ``ats_scanner/_run.py``'s ``_dormancy_gate_clause()`` canonical
form (``datetime('now', '-' || ? || ' days')`` bound with a plain positive
int), which the pre-existing ``_DATETIME_REWRITES[0]`` rule already
translates to ``now() - make_interval(days => %s)``.

While isolating the WHERE clause for a live-Postgres test, two more
pre-existing incompatibilities on the SAME lines surfaced and are fixed
alongside the two issue #380 predicates, both using patterns already
established elsewhere in this codebase rather than novel constructs:

- ``c.merged_into_id IS NULL`` — ``merged_into_id`` does not exist on the
  hosted ``companies`` table (no migration m0001-m0028 adds it). Three
  sibling ``ats_scanner`` ports already carry the identical carve-out
  (``_probe.py:370`` L-0018, ``_run_html.py:108`` L-0019,
  ``_run_playwright.py:156`` L-0020) — ``crawl_careers_batch`` (L-0461)
  simply missed applying it. Dropped here with the same ``# PORT-SEAM:``
  convention.
- ``c.careers_scan_enabled = 1`` — an integer literal compared against a
  ``boolean NOT NULL`` column (``m0021_wi13_scan_lane_columns.py:87``),
  the identical bug class ``test_scan_dialect.py``'s
  ``test_no_integer_literal_boolean_comparison_on_scan_enabled`` guards
  for the sibling ``scan_enabled`` column, fixed the same way there:
  ``TRUE`` is a valid literal on both dialects natively, no compat.py
  rewrite needed.

Two SELECT-list columns (``c.careers_api_endpoint``, ``c.careers_crawl_tier``)
were ALSO undefined on the hosted schema until migration 29 (public #385,
#347) added them; ``c.careers_nav_recipe`` was dropped from the SELECT list
entirely (zero readers anywhere in this port — see ``__init__.py``'s own
PORT-SEAM comment block). The bench-decay fragment (``_bench_predicate.py``'s
``build_bench_predicate_sql``, public #386) had two further gaps: its
``datetime(scanned_at) < datetime('now', '-N days')`` shape needed a third
``db/compat.py`` rewrite rule (bare-column ``datetime(...)`` unwrap — see
that module's docstring), and its SQL reads ``company_scan_log.jobs_matched``,
a column no migration had ever added until migration 29 also added it
(discovered while un-stubbing this very test — see that migration's
docstring for the full rationale). With all of the above landed, this test
now exercises the REAL lane queries end-to-end: the real 5-column SELECT
list, the real bench predicate (no stub), against a live Postgres
connection.

The two lane queries themselves are NOT hand-copied into this test: both
are imported from ``jobcannon.engine.careers_crawler`` (``_lane1_query_sql``
/ ``_lane2_query_sql``) and called with the real ``select_cols`` and the
real ``build_bench_predicate_sql()`` output, so the WHERE clause executed
here is byte-identical to the one ``crawl_careers_batch`` executes in
production (#380 review round 1, finding B1: an earlier revision
hand-copied a comment-stripped duplicate of the WHERE clause, which
silently diverged from production and did not catch the in-SQL-comment
regression B1 describes).

Both lanes place the bench predicate's own ``?`` placeholder at a DIFFERENT
position relative to their other placeholder (Lane 1: freshness placeholder
comes first in the WHERE text; Lane 2: bench placeholder comes first,
``LIMIT ?`` trails) — see ``__init__.py``'s own comments at each lane's
call site. A param-tuple swap between the two is silent (both values are
plain ints, no type error), so
``test_lane2_bench_and_limit_params_bind_to_correct_placeholders`` below
specifically discriminates the two orderings rather than merely asserting
one plausible-looking result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from jobcannon.engine.careers_crawler import _lane1_query_sql, _lane2_query_sql
from jobcannon.engine.careers_crawler._bench_predicate import (
    BENCH_CRAWLER_SOURCE,
    BENCH_STRIKE_THRESHOLD,
    BENCH_UNATTRIBUTED_ZERO_HIT_REASON,
    build_bench_predicate_sql,
)
from jobcannon.engine.json_utils import utc_now_iso
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def _days_ago_iso(days: int) -> str:
    """A naive-UTC ISO8601 string *days* in the past, matching
    utc_now_iso()'s storage convention (bound as a plain parameter, not a
    SQL datetime() call — the same shape record_scan_outcome's own
    scanned_at default already uses against this exact column)."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None).isoformat()


@pytest.fixture()
def wired_crawler_services():
    """Own throwaway database + pool, mirroring
    ``test_m0028_careers_crawl_flag_reason.py``'s ``wired_crawler_services``
    fixture: needs an isolated database rather than the shared
    session-scoped ``postgres_test_dsn`` every rollback-isolated test in
    this directory reads."""
    from jobcannon.db import _companies, _jd_full, _jobs
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.engine import services

    dsn, db_name = create_throwaway_db("jobcannon_crawl_batch_pg")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        services.set_services(
            services.ScanServices(
                connection_factory=pool_mod.connection_factory,
                upsert_job=_jobs.upsert_job,
                set_jd_full=_jd_full.set_jd_full,
                upsert_company=_companies.upsert_company,
                config={},
                get_secret=lambda name, *, config=None: None,
                jd_storage_max_chars=50_000,
            )
        )
        yield services.get_services()
    finally:
        services.clear_services()
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def _insert_company(
    conn,
    *,
    name,
    careers_url,
    flag_reason=None,
    careers_crawl_last_at=None,
    careers_api_endpoint=None,
    careers_crawl_tier=None,
):
    row = conn.execute(
        "INSERT INTO companies (name, name_raw, careers_url, careers_scan_enabled, "
        "ats_probe_status, careers_crawl_flag_reason, careers_crawl_last_at, "
        "careers_api_endpoint, careers_crawl_tier) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            name,
            name,
            careers_url,
            True,
            "pending",
            flag_reason,
            careers_crawl_last_at,
            careers_api_endpoint,
            careers_crawl_tier,
        ),
    ).fetchone()
    return row[0]


def _insert_qualifying_posting(conn, *, company_id, dedup_key):
    """A posting classified 'apply', matching the crawler lane queries'
    ``j.classification IN ('apply', 'consider')`` EXISTS/NOT EXISTS
    predicate (``jobs`` table-rewritten to ``postings`` by
    ``db/compat.py``'s ``_TABLE_REWRITES``)."""
    conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, classification) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dedup_key, company_id, "Engineer", "Co", "jd text", "apply"),
    )


def _insert_scan_log_row(
    conn,
    *,
    company_id,
    jobs_matched=None,
    failure_reason=None,
    source=BENCH_CRAWLER_SOURCE,
    scanned_at=None,
):
    """A ``company_scan_log`` row for benching setup. ``scanned_at`` defaults
    to :func:`utc_now_iso` (matching ``record_scan_outcome``'s own default),
    well inside any decay window this file exercises."""
    conn.execute(
        "INSERT INTO company_scan_log (company_id, source, jobs_matched, failure_reason, "
        "scanned_at) VALUES (?, ?, ?, ?, ?)",
        (company_id, source, jobs_matched, failure_reason, scanned_at or utc_now_iso()),
    )


# Lane queries, executed exactly as production builds them (imported above),
# with the real SELECT list and the real (un-stubbed) bench predicate — both
# public #385/#347 (SELECT columns) and #386 (bench predicate) are required
# for this module to even import cleanly against a live schema.
_SELECT_COLS = "c.id, c.name_raw, c.careers_url, c.careers_api_endpoint, c.careers_crawl_tier"
_BENCH_SQL, _BENCH_PARAMS = build_bench_predicate_sql()  # decay_days=21 default
_LANE1_WHERE_SQL = _lane1_query_sql(_SELECT_COLS, _BENCH_SQL)
_LANE2_WHERE_SQL = _lane2_query_sql(_SELECT_COLS, _BENCH_SQL)


def test_lane1_rediscovery_selects_due_row_excludes_flagged_row(wired_crawler_services):
    svc = wired_crawler_services
    with svc.connection_factory() as conn:
        due_id = _insert_company(conn, name="DueCo", careers_url="https://due.example/careers")
        _insert_qualifying_posting(conn, company_id=due_id, dedup_key="dk-due")

        flagged_id = _insert_company(
            conn,
            name="FlaggedCo",
            careers_url="https://flagged.example/careers",
            flag_reason="aggregator_suspected",
        )
        _insert_qualifying_posting(conn, company_id=flagged_id, dedup_key="dk-flagged")
        conn.commit()

        # Lane 1's freshness placeholder precedes the bench predicate's own
        # placeholder in the WHERE text (see __init__.py's comment at this
        # lane's call site), so params are (freshness_days, *bench_params).
        rows = conn.execute(_LANE1_WHERE_SQL, (14, *_BENCH_PARAMS)).fetchall()

    ids = {r[0] for r in rows}
    assert due_id in ids
    assert flagged_id not in ids


def test_lane2_origination_selects_never_crawled_excludes_company_with_existing_job(
    wired_crawler_services,
):
    svc = wired_crawler_services
    with svc.connection_factory() as conn:
        origination_id = _insert_company(
            conn, name="NewCo", careers_url="https://new.example/careers"
        )
        already_scored_id = _insert_company(
            conn, name="HasJobCo", careers_url="https://hasjob.example/careers"
        )
        _insert_qualifying_posting(conn, company_id=already_scored_id, dedup_key="dk-hasjob")
        conn.commit()

        # Lane 2's bench predicate placeholder precedes the trailing
        # `LIMIT ?` (see __init__.py's comment at this lane's call site), so
        # params are (*bench_params, origination_limit).
        rows = conn.execute(_LANE2_WHERE_SQL, (*_BENCH_PARAMS, 10)).fetchall()

    ids = {r[0] for r in rows}
    assert origination_id in ids
    assert already_scored_id not in ids


def test_lane2_bench_and_limit_params_bind_to_correct_placeholders(wired_crawler_services):
    """Discriminates Lane 2's (*bench_params, origination_limit) ordering
    from its swap (origination_limit, *bench_params) — a swap is otherwise
    silent, since both values are plain ints and no type error results.

    Setup: a benched company (5 crawler-origin strikes, no hits, all scanned
    5 days ago) plus two clean, never-crawled companies inserted in
    ascending id order. decay_days=21 and origination_limit=1 are chosen so
    a swap flips BOTH halves of the result, not just one:

    Correct binding: decay_days=21 reaches the bench predicate — a 5-day-old
    strike is well inside a 21-day decay window, so it still counts and the
    company stays benched (excluded). origination_limit=1 reaches LIMIT —
    only the lowest-id clean company is returned. Result: exactly 1 row,
    the smaller-id clean company.

    Swapped binding: decay_days would receive 1 — a 5-day-old strike is
    OLDER than a 1-day decay window, so it decays away, strikes drop to 0,
    and the company is no longer benched (included). origination_limit
    would receive 21 — comfortably above the 3 eligible rows, so LIMIT no
    longer trims anything. Result under the swap: all 3 companies (including
    the wrongly-un-benched one), not 1 — this test fails loudly either way
    a swap occurs, with no timing race (the 5-day offset is computed once,
    well clear of either decay window's boundary).
    """
    svc = wired_crawler_services
    with svc.connection_factory() as conn:
        benched_id = _insert_company(
            conn, name="BenchedCo", careers_url="https://benched.example/careers"
        )
        five_days_ago = _days_ago_iso(5)
        for _ in range(BENCH_STRIKE_THRESHOLD):
            _insert_scan_log_row(
                conn,
                company_id=benched_id,
                jobs_matched=0,
                failure_reason=BENCH_UNATTRIBUTED_ZERO_HIT_REASON,
                scanned_at=five_days_ago,
            )
        clean_low_id = _insert_company(
            conn, name="CleanLowCo", careers_url="https://clean-low.example/careers"
        )
        clean_high_id = _insert_company(
            conn, name="CleanHighCo", careers_url="https://clean-high.example/careers"
        )
        conn.commit()

        assert clean_low_id < clean_high_id  # ORDER BY c.id ASC relies on this

        sql, params = build_bench_predicate_sql(21)
        rows = conn.execute(_lane2_query_sql(_SELECT_COLS, sql), (*params, 1)).fetchall()

    ids = [r[0] for r in rows]
    assert benched_id not in ids  # decay_days=21 correctly bound to the predicate
    assert ids == [clean_low_id]  # origination_limit=1 correctly bound to LIMIT


def test_bench_predicate_hits_clears_benching_even_with_strikes(wired_crawler_services):
    """Positive control for the ``hits = 0`` half of the predicate: a
    company with >= BENCH_STRIKE_THRESHOLD strikes but at least one
    successful scan (jobs_matched > 0) must NOT be benched."""
    svc = wired_crawler_services
    with svc.connection_factory() as conn:
        company_id = _insert_company(
            conn, name="RecoveredCo", careers_url="https://recovered.example/careers"
        )
        for _ in range(BENCH_STRIKE_THRESHOLD):
            _insert_scan_log_row(
                conn,
                company_id=company_id,
                jobs_matched=0,
                failure_reason=BENCH_UNATTRIBUTED_ZERO_HIT_REASON,
            )
        _insert_scan_log_row(conn, company_id=company_id, jobs_matched=3)
        conn.commit()

        sql, params = build_bench_predicate_sql(21)
        row = conn.execute(
            f"SELECT {sql} FROM companies c WHERE c.id = ?", (*params, company_id)
        ).fetchone()

    # The predicate SQL is `NOT EXISTS(<is-benched>)`, so True means the
    # company is eligible (NOT benched) — one hit clears it despite 5 strikes.
    assert row[0] is True


def test_careers_api_endpoint_and_crawl_tier_round_trip_through_real_select(wired_crawler_services):
    """Confirms _api_cache.py's writer (careers_api_endpoint) and
    _escalation.py's readers (both columns) now work against real Postgres
    (#385, #347): the real lane SELECT list returns the values written."""
    svc = wired_crawler_services
    with svc.connection_factory() as conn:
        company_id = _insert_company(
            conn,
            name="WiredCo",
            careers_url="https://wired.example/careers",
            careers_api_endpoint="https://wired.example/api/jobs",
            careers_crawl_tier="static",
        )
        conn.commit()

        rows = conn.execute(_LANE2_WHERE_SQL, (*_BENCH_PARAMS, 10)).fetchall()

    row = next(r for r in rows if r[0] == company_id)
    assert row[3] == "https://wired.example/api/jobs"
    assert row[4] == "static"


def test_ats_probe_status_is_not_hit_would_have_raised_syntax_error_on_postgres(
    wired_crawler_services,
):
    """Positive control (mirrors test_ats_prober_dialect.py's
    test_integer_literal_would_have_raised_on_postgres): proves the
    original ``IS NOT 'hit'`` predicate this PR replaces is not merely
    untested against Postgres, it is an actual syntax error there --
    empirically the same statement shape both lane queries carried before
    this fix, same schema, same connection stack the tests above exercise
    with ``IS DISTINCT FROM 'hit'`` instead."""
    svc = wired_crawler_services
    with svc.connection_factory() as conn:
        with pytest.raises(psycopg.errors.SyntaxError):
            conn.execute("SELECT 1 FROM companies WHERE ats_probe_status IS NOT 'hit'")


def test_datetime_now_days_param_would_not_have_been_translated_on_postgres(
    wired_crawler_services,
):
    """Positive control: the pre-fix bound-parameter shape
    (``datetime('now', ? || ' days')`` bound with a pre-negated string
    like ``"-14"``) does not match db/compat.py's ``_DATETIME_REWRITES``,
    so it reaches Postgres as a literal ``datetime(...)`` function call,
    which does not exist there."""
    svc = wired_crawler_services
    with svc.connection_factory() as conn:
        with pytest.raises(psycopg.errors.UndefinedFunction):
            conn.execute(
                "SELECT 1 FROM companies WHERE careers_crawl_last_at < datetime('now', ? || ' days')",
                ("-14",),
            )

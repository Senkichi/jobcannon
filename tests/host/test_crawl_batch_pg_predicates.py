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
are ALSO undefined on the hosted schema — confirmed empirically while
building this test (``UndefinedColumn: column c.careers_api_endpoint does
not exist``). Unlike the WHERE-clause items above, these are read
downstream by ``_escalation.py``'s per-company worker
(``company["careers_api_endpoint"]``, ``company["careers_crawl_tier"]``),
so dropping them from the SELECT would trade ``UndefinedColumn`` for a
``KeyError`` rather than fix anything — that needs a schema migration and
a downstream-consumer change, not a query edit, and is out of this PR's
scope. ``careers_crawl_tier`` is tracked at #347 (filing a comment there:
its "not an active bug, no reader exists yet" claim is now stale now that
``crawl_careers_batch`` reads the column). ``careers_api_endpoint`` has no
existing tracker; filed as a new follow-up issue.

Because of the above, ``crawl_careers_batch`` itself still cannot be
called end-to-end against Postgres. Mirroring
``test_m0028_careers_crawl_flag_reason.py``'s scope precedent (landed one
commit before this test, same file, same tension), this test exercises
the WHERE clause of each lane directly — with a SELECT list restricted to
columns that exist — rather than the full function. The bench-decay
fragment (``_bench_predicate.py``'s ``build_bench_predicate_sql``, whose
``datetime(scanned_at) < datetime('now', '-N days')`` shape is also
untranslated by compat.py — a third, separate gap, also filed as a
follow-up) is stubbed to ``TRUE`` here; ``_bench_predicate.py`` itself is
not touched by this PR.

The two lane queries themselves are NOT hand-copied into this test: both
are imported from ``jobcannon.engine.careers_crawler`` (``_lane1_query_sql``
/ ``_lane2_query_sql``) and called with a restricted SELECT list and the
stubbed bench predicate, so the WHERE clause executed here is byte-identical
to the one ``crawl_careers_batch`` executes in production (#380 review round
1, finding B1: an earlier revision hand-copied a comment-stripped duplicate
of the WHERE clause, which silently diverged from production and did not
catch the in-SQL-comment regression B1 describes).
"""

from __future__ import annotations

import psycopg
import pytest

from jobcannon.engine.careers_crawler import _lane1_query_sql, _lane2_query_sql
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


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


def _insert_company(conn, *, name, careers_url, flag_reason=None, careers_crawl_last_at=None):
    row = conn.execute(
        "INSERT INTO companies (name, name_raw, careers_url, careers_scan_enabled, "
        "ats_probe_status, careers_crawl_flag_reason, careers_crawl_last_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (name, name, careers_url, True, "pending", flag_reason, careers_crawl_last_at),
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


# Lane queries, executed exactly as production builds them (imported above),
# with the SELECT list restricted to columns that exist on the hosted schema
# and the bench-decay fragment stubbed to TRUE (see module docstring).
_LANE1_WHERE_SQL = _lane1_query_sql("c.id, c.name_raw, c.careers_url", "TRUE")
_LANE2_WHERE_SQL = _lane2_query_sql("c.id, c.name_raw, c.careers_url", "TRUE")


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

        rows = conn.execute(_LANE1_WHERE_SQL, (14,)).fetchall()

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

        rows = conn.execute(_LANE2_WHERE_SQL, (10,)).fetchall()

    ids = {r[0] for r in rows}
    assert origination_id in ids
    assert already_scored_id not in ids


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

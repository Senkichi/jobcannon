# PORTED from job_finder/web/careers_crawler/_bench_predicate.py @ a071c405a6908b6af417eef7aab6ec0b795a7d27 (private job-cannon). Ledger L-0463.
"""5-strike benching predicate for ``company_scan_log`` — single source of truth.

A company is **benched** when ``company_scan_log`` shows
``>= BENCH_STRIKE_THRESHOLD`` **crawler-origin** scans (rows where
``source = 'careers_crawler'``) that are **strikes** — zero-hit rows whose
``failure_reason`` indicates a *broken* attempt — and **no successful scan**
(no row where ``jobs_matched > 0``). This is the "5-strike penalty box" gate
that ``crawl_careers_batch`` uses to exclude a company from crawling, and the
same gate ``heal_pipeline`` checks to skip healing a ``careers:*`` source whose
company can never earn a fresh success (#1496).

**Lane scoping via explicit ``source`` column (#1550, W1).** ATS-scanner
writers (``ats_scanner/_run.py``, ``_run_html.py``, ``_run_playwright.py``)
insert ``company_scan_log`` rows with ``source = 'ats_scanner'``; the crawler
writer (``careers_crawler/_persistence.py``) inserts with
``source = 'careers_crawler'``. The predicate counts only crawler-origin rows.
Before the ``source`` column existed, the predicate counted **all** rows and
ATS rows (93.9% of scan_log) poisoned the strike counter — benching companies
the crawler never even failed on. Per plan decision D2, the lane is a fact
(``source``), not an inference (``jobs_matched IS NOT NULL``); the proxy was
acceptable only for the one-time backfill migration, never as runtime logic.

**Reason-aware strike semantics (#1725, W4).** W1 fixed *which rows* are
counted (crawler only). W4 fixes *which zero-hit rows count as a strike*. A
bare ``jobs_matched = 0`` conflates two unrelated states (#1550's titular
complaint):

1. **Scanner is broken** — recipe navigated to the wrong page, selector rotted,
   page 403s / bot-blocked. Benching is correct.
2. **Board had no role matching ``profile.target_titles`` on that particular
   night** — the recipe navigated fine to a live job board, the unfiltered
   extraction is job-shaped, but no title matched. Benching is wrong, and
   permanent: the exclusion is a ``NOT EXISTS`` on history, so a company whose
   openings rotate out of target titles for five consecutive crawls is excluded
   forever with no path back.

W4 keys the strike counter on the per-crawl ``failure_reason`` column (added by
migration 209009471, the W2 prerequisite). A zero-hit row counts as a strike
only when its ``failure_reason`` is **not** a clean reason — i.e. it indicates a
broken attempt (or is NULL: legacy rows and any future writer that does not
attribute its zero-hit). A row carrying a clean reason
(:data:`BENCH_CLEAN_FAILURE_REASONS`, e.g. ``no_title_match`` — the navigator
reached a live job board whose current openings just don't match the profile's
target titles) does **not** count as a strike, so a company whose openings
rotate out of target titles is no longer permanently benched for it.

The ``hits = 0`` gate (no row with ``jobs_matched > 0``) is preserved: a single
successful scan still clears benching, exactly as before. W4 only narrows
*which* zero-hit rows accumulate toward the threshold; it does not let a broken
company with one ancient success stay benched, nor un-bench one.

**Strike decay (T2.3, D9).** D4 rejected time-based decay *as a replacement*
for reason-aware semantics — decay alone would un-bench a company on a
calendar interval regardless of whether the scanner is still broken, the
wrong axis for W4's clean-vs-broken question. T2.3 adds decay *on top of* the
reason-aware counter instead of replacing it: a strike (a zero-hit row that
already survived the clean-reason filter above) stops counting once it is
older than :data:`BENCH_STRIKE_DECAY_DAYS`, derived from the row's own
``scanned_at`` timestamp — no migration, ``company_scan_log.scanned_at`` has
existed since the table's baseline. This closes the "no decay/reset path"
half of D9 (496/2385 companies permanently boxed): a company whose scanner
was broken once but has since been fixed (or whose entry stopped being
crawled for other reasons) ages back out of the box instead of staying
excluded forever, while a company with 5 strikes *inside* the window stays
boxed exactly as before. The ``hits`` gate is intentionally NOT decayed: a
scan that ever succeeded, however long ago, still clears benching — decaying
that too would let a company oscillate back into the box on old history,
which is a different (and out of scope) question from "should an old broken
streak still count."

Both the SQL fragment (for set-based crawler selection) and the Python helper
(for per-company heal checks) derive from the same threshold + source-scoping +
clean-reason + decay-window definition so the predicate cannot drift between
the two call sites. Before this module the predicate was hand-copied in three
places (two crawler lanes + the heal gate), which is exactly the
single-point-of-enforcement gap that caused #1496. The decay *window value*
itself is also resolved from config in exactly one place
(:func:`resolve_bench_decay_days`) and threaded into both
``crawl_careers_batch`` and ``heal_pipeline._careers_source_benched`` — reading
``config['careers_crawl']['bench_strike_decay_days']`` independently at each
call site would reopen the same drift risk one level up (e.g. a bad/blank
config value silently disabling decay at one site but not the other).
"""

from __future__ import annotations

import sqlite3

#: Minimum total crawler scans before the benching gate can fire.
BENCH_STRIKE_THRESHOLD = 5

#: Default strike-decay window in days (T2.3, D9). A zero-hit row that would
#: otherwise count as a strike (see :data:`BENCH_CLEAN_FAILURE_REASONS`) stops
#: counting once its ``scanned_at`` is older than this many days. Overridable
#: per-call (``build_bench_predicate_sql`` / ``is_company_benched``); the
#: crawler's main selection path (``crawl_careers_batch``) reads
#: ``config['careers_crawl']['bench_strike_decay_days']`` and falls back to
#: this constant when the key is absent. 21 days sits in the plan's suggested
#: 14-30 day range: long enough that a company needs a *sustained* run of
#: recent failures (not just old history) to be boxed, short enough that a
#: fixed scanner or a company that ages out of "5 strikes in the window"
#: reliably resurfaces within a few crawl cycles (crawl cadence is daily per
#: ``careers_crawl.freshness_days``).
BENCH_STRIKE_DECAY_DAYS = 21

#: The ``source`` value that identifies crawler-origin ``company_scan_log``
#: rows. ATS-scanner rows (``source = 'ats_scanner'``) are excluded from the
#: strike count — they populate ``jobs_found`` / ``skipped_title_filter`` but
#: never ``jobs_matched``, so under the old unscoped predicate they counted
#: toward ``total`` while never registering as a hit (#1550, F1).
BENCH_CRAWLER_SOURCE = "careers_crawler"

#: ``failure_reason`` values that indicate a **clean** zero-hit — the crawl
#: navigated fine to a live job board whose current openings just don't match
#: ``profile.target_titles``. Such rows do **not** count as a strike (#1725,
#: W4). The set is the single source of truth for "clean vs broken"; both the
#: SQL fragment and the Python helper derive their IN-clause / membership test
#: from it so the two call sites cannot drift.
#:
#: ``no_title_match`` — the ai_nav tier's verdict that the recipe ran to
#: completion (``steps_executed == steps_total > 0``) and the unfiltered
#: extraction is job-shaped, i.e. the board is live and the recipe is good;
#: the zero is a title-relevance outcome, not a navigation defect (see
#: ``ai_career_navigator.discover_navigation_recipe``'s job-shaped branch and
#: ``careers_crawler._ai_nav_tier._try_ai_navigation``'s reason_sink surfacing
#: of it).
BENCH_CLEAN_FAILURE_REASONS: frozenset[str] = frozenset({"no_title_match"})

#: Sentinel ``failure_reason`` recorded for crawler-origin zero-hit rows the
#: crawler cannot attribute to a specific ai_nav verdict (T2.3, D9) — i.e. a
#: zero-hit from a non-ai_nav tier (static/sitemap/playwright/etc.) or from
#: ai_nav with ``reason_sink`` left empty. Pre-T2.3 these rows wrote NULL,
#: which is auditable only as "unknown"; writing this sentinel instead makes
#: the strike auditable (D9: "record a reason") without changing strike
#: semantics — it is deliberately NOT a member of
#: :data:`BENCH_CLEAN_FAILURE_REASONS`, so it still counts as a strike exactly
#: like NULL did (conservative). NULL itself is still treated as a strike
#: (see the predicate below) for any pre-existing row or any writer that has
#: not been updated to pass this sentinel.
BENCH_UNATTRIBUTED_ZERO_HIT_REASON = "unattributed_zero_hit"

#: SQL IN-list rendered from :data:`BENCH_CLEAN_FAILURE_REASONS`, interpolated
#: into the predicate SQL so the SQL and the Python helper share one definition
#: of "clean". Sorted for deterministic output (the set has one member today;
#: the sort keeps the generated SQL stable as members are added).
_CLEAN_REASONS_SQL = ", ".join(f"'{r}'" for r in sorted(BENCH_CLEAN_FAILURE_REASONS))


def resolve_bench_decay_days(config: dict) -> int:
    """Resolve the configured strike-decay window, coercing bad values safely.

    Both ``crawl_careers_batch`` and ``heal_pipeline._careers_source_benched``
    call this so the two gates cannot silently disagree (T2.3 review note):
    the config key is read in exactly one place. ``config['careers_crawl']
    ['bench_strike_decay_days']`` is missing for most users (falls back to
    :data:`BENCH_STRIKE_DECAY_DAYS`) — that is the expected, silent case. A
    *present but malformed* value (blank YAML scalar -> ``None``, a non-numeric
    string, a negative number) must not silently disable decay by making every
    ``datetime(scanned_at) < datetime('now', '-None days')`` comparison NULL
    (which the CASE's ``ELSE 1`` would then treat as "still a strike" for
    every row, reverting to pre-T2.3 behaviour with no error surfaced) — so
    coerce with a fallback instead of interpolating the raw config value.
    """
    raw = (config.get("careers_crawl", {}) or {}).get(
        "bench_strike_decay_days", BENCH_STRIKE_DECAY_DAYS
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return BENCH_STRIKE_DECAY_DAYS
    return value if value > 0 else BENCH_STRIKE_DECAY_DAYS


def build_bench_predicate_sql(
    decay_days: int = BENCH_STRIKE_DECAY_DAYS,
) -> tuple[str, tuple[int]]:
    """Build the ``NOT EXISTS`` benching predicate SQL fragment.

    Uses the outer-query alias ``c.id`` (the alias both selection lanes in
    ``crawl_careers_batch`` use for the ``companies`` row). The threshold is
    interpolated from :data:`BENCH_STRIKE_THRESHOLD`, the clean-reason IN-list
    from :data:`BENCH_CLEAN_FAILURE_REASONS`, and the decay window from
    *decay_days* (default :data:`BENCH_STRIKE_DECAY_DAYS`) so the SQL and the
    Python helper (:func:`is_company_benched`) share one definition. Only
    crawler-origin rows (``source = 'careers_crawler'``) are counted —
    ATS-scanner rows must not poison the strike counter (#1550, W1/D2). A
    zero-hit row counts as a strike only when its ``failure_reason`` is not a
    clean reason (NULL is a strike — conservative, preserves pre-W4 behaviour
    for unattributed rows) AND its ``scanned_at`` is within the decay window
    (T2.3, D9) — an old strike ages out even if its reason was never clean. A
    single successful scan (``jobs_matched > 0``), at any age, still clears
    benching (#1725, W4; decay does not apply to hits — see module docstring).

    Returns ``(sql, params)``. The decay-window comparison is bound via a
    single ``?`` placeholder using the canonical parameterized shape
    ``datetime('now', '-' || ? || ' days')`` (public #386) — the same shape
    ``ats_scanner/_run.py``'s ``_dormancy_gate_clause`` uses and the only one
    ``jobcannon/db/compat.py``'s ``_DATETIME_REWRITES`` translates for
    Postgres — bound with *decay_days* itself (a plain positive int), not a
    pre-negated string. Callers that splice this fragment into a larger query
    (both ``crawl_careers_batch`` lanes) must append ``params`` to their own
    param tuple at the position this fragment's ``?`` falls in the final SQL
    text — see ``__init__.py``'s lane call sites for the exact ordering.
    """
    sql = (
        "NOT EXISTS (\n"
        "                    SELECT 1 FROM (\n"
        "                        SELECT SUM(CASE WHEN jobs_matched > 0 THEN 1 ELSE 0 END) AS hits,\n"
        "                               SUM(CASE WHEN jobs_matched > 0 THEN 0\n"
        f"                                        WHEN failure_reason IN ({_CLEAN_REASONS_SQL}) THEN 0\n"
        "                                        WHEN datetime(scanned_at) < datetime('now', '-' || ? || ' days') THEN 0\n"
        "                                        ELSE 1 END) AS strikes\n"
        "                        FROM company_scan_log\n"
        "                        WHERE company_id = c.id\n"
        f"                          AND source = '{BENCH_CRAWLER_SOURCE}'\n"
        f"                    ) s WHERE s.hits = 0 AND s.strikes >= {BENCH_STRIKE_THRESHOLD}\n"
        "                )"
    )
    return sql, (decay_days,)


#: The default-decay predicate fragment + its bind params, for callers that
#: don't need a config-driven decay window (e.g. tests asserting the fragment
#: shape). ``crawl_careers_batch`` builds its own via
#: :func:`build_bench_predicate_sql` with the configured decay window instead
#: of importing these constants. ``BENCH_PREDICATE_SQL`` carries one bare
#: ``?`` placeholder — interpolating it into a query without also binding
#: ``BENCH_PREDICATE_PARAMS`` at the matching position raises at execute time.
BENCH_PREDICATE_SQL, BENCH_PREDICATE_PARAMS = build_bench_predicate_sql()


def is_company_benched(
    conn: sqlite3.Connection,
    company_id: int,
    decay_days: int = BENCH_STRIKE_DECAY_DAYS,
) -> bool:
    """True when *company_id* is benched (``>= BENCH_STRIKE_THRESHOLD``
    crawler-origin strikes within the decay window, 0 hits).

    The Python equivalent of :func:`build_bench_predicate_sql` — used by
    ``heal_pipeline`` for per-company checks where a set-based SQL fragment is
    not practical (the heal gate resolves companies by hostname, not by a
    ``companies`` table alias). Only crawler-origin rows
    (``source = 'careers_crawler'``) are counted, matching the SQL fragment
    exactly (#1550, W1/D2). A zero-hit row counts as a strike only when its
    ``failure_reason`` is not a clean reason (NULL is a strike — conservative)
    AND its ``scanned_at`` is within *decay_days* (T2.3, D9); a single
    successful scan (``jobs_matched > 0``), at any age, still clears benching
    (#1725, W4).

    Uses the same canonical ``datetime('now', '-' || ? || ' days')`` shape as
    :func:`build_bench_predicate_sql` (public #386), bound with *decay_days*
    directly rather than a pre-negated string.
    """
    hits, strikes = conn.execute(
        "SELECT SUM(CASE WHEN jobs_matched > 0 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN jobs_matched > 0 THEN 0 "
        f"WHEN failure_reason IN ({_CLEAN_REASONS_SQL}) THEN 0 "
        "WHEN datetime(scanned_at) < datetime('now', '-' || ? || ' days') THEN 0 "
        "ELSE 1 END) "
        "FROM company_scan_log WHERE company_id = ? "
        f"AND source = '{BENCH_CRAWLER_SOURCE}'",
        (decay_days, company_id),
    ).fetchone()
    return int(hits or 0) == 0 and int(strikes or 0) >= BENCH_STRIKE_THRESHOLD

"""SQLite-dialect compatibility shim for ENGINE-AUTHORED SQL only.

The engine's inline SQL (ats_scanner/_run.py, stale_detector.py) uses qmark
placeholders and reaches this layer verbatim through connection_factory
connections. Host-authored SQL must use psycopg placeholders directly and
must NOT route through this translation.

Table rewrite: the engine's inline SQL (verified 2026-07-17) addresses a
`jobs` table — `ats_scanner/_run.py`:
  - :1247-1249  SELECT jd_full FROM jobs WHERE dedup_key = ?
  - :1273-1276  UPDATE jobs SET comp_data_json = ? WHERE dedup_key = ?
  - :1293-1296  UPDATE jobs SET is_remote = ?, employment_type = ?,
                department = ? WHERE dedup_key = ?
  - :1313-1316  UPDATE jobs SET ats_refreshed_at = COALESCE(?,
                ats_refreshed_at) WHERE dedup_key = ?
but the hosted schema's postings table is named `postings` (m0001). All four
sites are covered by a single (FROM|UPDATE|INTO|JOIN) jobs -> postings
rewrite.

Date-function rewrite (PR 5b): the engine writes two SQLite-only date-function
shapes in `ats_scanner/_run.py` / `ats_scanner/_run_playwright.py` — kept in
SQLite dialect deliberately, since tests/engine/ exercises this exact SQL
directly against a bare sqlite3 connection with no translation layer (see
tests/engine/test_dormancy_cadence.py, tests/engine/test_run_playwright.py).
This is the ONLY place either shape is rewritten for the hosted path:
  - `datetime('now', '-' || ? || ' days')` (the dormancy gate's interval
    arithmetic, `_dormancy_gate_clause`) -> `now() - make_interval(days => ?)`
  - bare `datetime('now')` (the retry-eligibility clauses) -> `now()`
Host-authored SQL never calls SQLite's datetime(), so both rewrites are a
no-op for it.

KNOWN-UNSUPPORTED (Wave-2 / scan-orchestration PR work — recorded here so
this shim's coverage claim stays honest). qmark translation, the table
rewrite, and the date-function rewrite above make engine SQL *parse and run*
against Postgres, but the following engine surfaces are NOT yet runnable on
the hosted schema even after translation, because they depend on columns
m0001 does not carry or on gates with no representable Postgres form:
  (a) RESOLVED in PR 5b: `run_ats_scan`'s eligibility clauses in
      `ats_scanner/_run.py` (and the sibling call sites in
      `ats_scanner/_run_html.py` / `ats_scanner/_run_playwright.py` that
      import them) — the dormancy gate and retry-eligibility `datetime('now')`
      calls are now translated by this module's date-function rewrite (above);
      the high-score-history gate is neutralized to `TRUE` in-engine (no
      representable multi-tenant sub_scores_json target in 1B — see
      `_high_score_history_clause`'s docstring in `ats_scanner/_run.py`); and
      every `scan_enabled = 1`/`= 0` integer literal against the `boolean`
      `scan_enabled` column (caught live by the Postgres smoke test in
      tests/host/test_run_scan_once_smoke.py — `operator does not exist:
      boolean = integer`) is rewritten in-engine to `TRUE`/`FALSE`, which both
      Postgres and SQLite accept natively (no compat-layer rewrite needed).
  (b) `stale_detector.py`'s audit-trail writes (:295, :400) INSERT into
      `pipeline_events`, a table with no hosted equivalent in m0001.
  (c) `ats_scanner/_probe.py`'s speculative-probe column
      `ats_probe_attempted_at` (eligibility reads :280-281; probe-outcome
      writes :408-:705 — re-verified 2026-08-13) is the only remaining
      `run_ats_scan`-adjacent column gap: m0003 (PR 5a, merged) added the
      other seven companies columns that `ats_scanner/_run.py` (:193-293)
      and `ats_scanner/_run_playwright.py` (:250, :298) reference —
      `name_raw`, `retry_count`, `retry_after`, `miss_reason`,
      `careers_crawl_last_at`, `jobs_found_total`, `last_scan_postings_json`,
      `last_scan_cached_at` — and PR 5b (this PR) makes `upsert_company`
      populate `name_raw` on insert (previously written nowhere, m0003's
      own docstring flagged this as deferred to "a later PR"). `_probe.py`'s
      `probe_ats_slugs` is a separate scan orchestrator from `run_ats_scan`
      and is not exercised by this PR.
  (d) `has_subcountry_constraint` — unlike (a)-(c), not a blocked engine
      surface but a private-schema delta, recorded so the resync surface
      stays complete. A private-schema `jobs` column (private migration
      `m207454240_add_has_subcountry_constraint_to_jobs`: `ALTER TABLE
      jobs ADD COLUMN has_subcountry_constraint INTEGER`), written solely
      by the private location-enrichment chain (`enrichment_tiers.py:818`
      -> `data_enricher.py:1008` -> the single DB write at
      `data_enricher.py:1196`), none of which is on the port surface. The
      hosted schema carries no such column, no hosted migration adds one,
      and no engine code reads or writes it (verified 2026-08-13: zero
      hits repo-wide, tracked and untracked, with positive controls —
      `location_fit`, `enrichment_tier`, and `jd_full` all hit). If a
      future port brings the location-enrichment writer chain across,
      this entry must be resolved (hosted column + migration) before that
      port merges.

What IS verified end-to-end on Postgres: the `_upsert_one_ats_api_job` write
path (INSERT/UPDATE against `postings`, including the translated
`jobs`->`postings` qmark SQL above), exercised by the Step 11 contract test
driving it through a live connection (tests/host/test_scan_services_contract.py).
"""

from __future__ import annotations

import re


def qmark_to_format(sql: str) -> str:
    """Translate '?' placeholders to '%s' and escape literal '%' to '%%',
    skipping single-quoted string literals (standard SQL '' escaping)."""
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_string:
            if ch == "'":
                # '' inside a string is an escaped quote, stay in-string
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                in_string = False
            out.append("%%" if ch == "%" else ch)
            i += 1
            continue
        if ch == "'":
            in_string = True
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_TABLE_REWRITES = (
    (re.compile(r"\b(FROM|UPDATE|INTO|JOIN)\s+jobs\b", re.IGNORECASE), r"\1 postings"),
)

# SQLite-only date-function shapes -> Postgres equivalents (module docstring
# has the full rationale). Order matters only for readability here: the two
# patterns can't collide (the interval form's ',' right after 'now' can never
# match the bare form's required immediate ')'). Both run BEFORE
# qmark_to_format so the `?` inside the interval form's rewritten output is
# still a bare qmark when the subsequent qmark_to_format pass converts every
# remaining `?` (this one plus the clause's own leading `consecutive_empty_
# scans <= ?`) to `%s` in one consistent left-to-right pass — no reordering
# of bind parameters relative to the params tuple callers build.
_DATETIME_REWRITES = (
    (
        re.compile(r"datetime\(\s*'now'\s*,\s*'-'\s*\|\|\s*\?\s*\|\|\s*'\s*days'\s*\)"),
        "now() - make_interval(days => ?)",
    ),
    (re.compile(r"datetime\(\s*'now'\s*\)"), "now()"),
)


def engine_sql_to_host(sql: str) -> str:
    """Date-function rewrite + qmark translation + engine `jobs` -> host
    `postings` table rewrite.

    This is what EngineCompatConnection.execute() actually runs. Host-
    authored SQL naming `postings` directly passes through unchanged (the
    table regex only matches the literal token `jobs`), and host-authored SQL
    never calls SQLite's datetime() either, so both rewrites are a no-op for
    it.
    """
    out = sql
    for pattern, repl in _DATETIME_REWRITES:
        out = pattern.sub(repl, out)
    out = qmark_to_format(out)
    for pattern, repl in _TABLE_REWRITES:
        out = pattern.sub(repl, out)
    return out

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

KNOWN-UNSUPPORTED (Wave-2 / scan-orchestration PR work — recorded here so
this shim's coverage claim stays honest). qmark translation and the table
rewrite make engine SQL *parse* against Postgres, but the following engine
surfaces are NOT yet runnable on the hosted schema even after translation,
because they depend on SQLite-only functions or on columns m0001 does not
carry:
  (a) `run_ats_scan`'s eligibility clauses in `ats_scanner/_run.py` —
      `_dormancy_gate_clause` (:233-237) uses SQLite's
      `datetime('now', '-' || ? || ' days')`, and
      `_high_score_history_gate_clause` (:189-207) uses `json_extract(...)`
      against `sub_scores_json`, a column the hosted `postings` schema does
      not carry (structural axes live in `structural_axes` instead).
  (b) `stale_detector.py`'s audit-trail writes (:295, :400) INSERT into
      `pipeline_events`, a table with no hosted equivalent in m0001.
  (c) prober/Playwright company columns referenced across
      `ats_scanner/_probe.py` (:277-358), `ats_scanner/_run.py` (:193-293),
      and `ats_scanner/_run_playwright.py` (:250, :298) — `name_raw`,
      `retry_after`, `miss_reason`, `ats_probe_attempted_at`,
      `jobs_found_total` — none of which m0001's `companies` table defines.

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


def engine_sql_to_host(sql: str) -> str:
    """qmark translation + engine `jobs` -> host `postings` table rewrite.

    This is what EngineCompatConnection.execute() actually runs. Host-
    authored SQL naming `postings` directly passes through unchanged (the
    regex only matches the literal token `jobs`).
    """
    out = qmark_to_format(sql)
    for pattern, repl in _TABLE_REWRITES:
        out = pattern.sub(repl, out)
    return out

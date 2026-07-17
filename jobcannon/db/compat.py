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
rewrite; no other engine-authored SQL diverges from Postgres dialect once the
table name and qmark placeholders are translated (verified empirically via
the Step 11 contract test driving _upsert_one_ats_api_job end to end).
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

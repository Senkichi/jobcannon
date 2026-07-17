"""SQLite-dialect compatibility shim for ENGINE-AUTHORED SQL only.

The engine's inline SQL (ats_scanner/_run.py, stale_detector.py) uses qmark
placeholders and reaches this layer verbatim through connection_factory
connections. Host-authored SQL must use psycopg placeholders directly and
must NOT route through this translation.
"""

from __future__ import annotations


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

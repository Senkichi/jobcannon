"""AST guard: jobcannon/db/_events.py is the ONLY module allowed to write to
the events table OR the users.analytics_consent column.

Best-effort STATIC lint over string LITERALS. It cannot see SQL assembled at
runtime (f-strings / concatenation / .format() split the table name across
separate literal nodes and evade this scan) — that residual gap is covered at
the write boundary instead: events_schema.validate_payload is called inside
jobcannon.db._events.insert_event, so any write that DOES go through the
sanctioned writer is validated regardless of how its SQL was assembled. A
DB-level trigger/role GRANT on the events table is the airtight future
hardening. jobcannon/db/_events.py (the sanctioned writer) and
jobcannon/db/migrations/ (legitimate schema DDL for the events table +
analytics_consent columns) are exempt from the scan.
"""

import ast
import pathlib
import re

ROOTS = ["jobcannon/host", "jobcannon/web", "jobcannon/db"]
_EXEMPT_SUBSTRINGS = ("jobcannon/db/_events.py", "/migrations/")


def _is_forbidden_events_sql(s: str) -> bool:
    # Normalize: lowercase, collapse whitespace, drop identifier quoting so
    # `INSERT INTO "events"`, `insert into  public.events`, and newline-split
    # variants all reduce to a detectable phrase.
    n = re.sub(r"\s+", " ", s.lower()).replace('"', "").replace("`", "")
    if re.search(r"\binsert\s+into\s+(?:public\.)?events\b", n):
        return True
    if re.search(r"\bupdate\s+(?:public\.)?events\b", n):
        return True
    # analytics_consent has a single sanctioned writer; any write to it in a
    # scanned root is a violation. Word-boundaried so the identifier
    # `analytics_consent_updated_at` in schema DDL does NOT trip `\bupdate\b`.
    if "analytics_consent" in n and re.search(r"\b(?:update|insert\s+into)\b", n):
        return True
    return False


def test_only_events_module_writes_events_table():
    offenders = []
    for root in ROOTS:
        for path in pathlib.Path(root).rglob("*.py"):
            posix = str(path).replace("\\", "/")
            if any(ex in posix for ex in _EXEMPT_SUBSTRINGS):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if _is_forbidden_events_sql(node.value):
                        offenders.append(str(path))
    assert not offenders, f"raw events-table SQL outside _events.py: {offenders}"


def test_forbidden_events_sql_predicate():
    assert _is_forbidden_events_sql("INSERT INTO events (user_id) VALUES (%s)")
    assert _is_forbidden_events_sql("UPDATE events SET payload = %s")
    assert _is_forbidden_events_sql("UPDATE users SET analytics_consent = true WHERE id = %s")
    # A read of the column is fine (the sanctioned reader lives in db/_events.py):
    assert not _is_forbidden_events_sql("SELECT analytics_consent FROM users WHERE id = %s")
    assert not _is_forbidden_events_sql("SELECT * FROM postings")
    # normalization: quoted / schema-qualified / newline variants still caught
    assert _is_forbidden_events_sql('INSERT INTO "events" (user_id) VALUES (%s)')
    assert _is_forbidden_events_sql("INSERT INTO public.events (user_id) VALUES (%s)")
    assert _is_forbidden_events_sql("INSERT INTO\n  events (user_id) VALUES (%s)")
    # word-boundary: schema DDL for analytics_consent_updated_at must NOT trip the update check
    assert not _is_forbidden_events_sql(
        "ALTER TABLE users ADD COLUMN analytics_consent_updated_at timestamptz"
    )

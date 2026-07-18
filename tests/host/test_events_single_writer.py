"""AST guard: jobcannon/db/_events.py is the ONLY module allowed to write to
the events table OR the users.analytics_consent column. jobcannon/db is
deliberately excluded from ROOTS — that is where the sanctioned raw SQL
legitimately lives (_events.py's own INSERT/UPDATE statements would
otherwise trip this scan on itself)."""

import ast
import pathlib

ROOTS = ["jobcannon/host", "jobcannon/web"]


def _is_forbidden_events_sql(s: str) -> bool:
    s = s.lower()
    if "insert into events" in s or "update events" in s:
        return True
    # analytics_consent has a single sanctioned writer (_events.record_consent,
    # in jobcannon/db which is excluded from ROOTS); any write to it in host/web
    # is a single-writer violation.
    if "analytics_consent" in s and ("update" in s or "insert into" in s):
        return True
    return False


def test_only_events_module_writes_events_table():
    offenders = []
    for root in ROOTS:
        for path in pathlib.Path(root).rglob("*.py"):
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

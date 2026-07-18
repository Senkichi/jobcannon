"""AST guard: jobcannon/db/_events.py is the ONLY module allowed to write to
the events table. jobcannon/db is deliberately excluded from ROOTS — that is
where the sanctioned raw SQL legitimately lives (_events.py's own INSERT/
UPDATE statements would otherwise trip this scan on itself)."""

import ast
import pathlib

ROOTS = ["jobcannon/host", "jobcannon/web"]


def test_only_events_module_writes_events_table():
    offenders = []
    for root in ROOTS:
        for path in pathlib.Path(root).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    s = node.value.lower()
                    if "insert into events" in s or "update events" in s:
                        offenders.append(str(path))
    assert not offenders, f"raw events-table SQL outside _events.py: {offenders}"

"""AST guard: `feed_state` is read-only. No module under jobcannon/host,
jobcannon/web, jobcannon/db, or jobcannon/worker writes to it — the table
exists in the schema (m0001) but has zero writers anywhere in the app; the
honest default state of the feed is "every row unranked" until a ranker
ships. Modeled on tests/host/test_events_single_writer.py.

Best-effort STATIC lint over string LITERALS. It cannot see SQL assembled at
runtime (f-strings / concatenation / .format() split the table name across
separate literal nodes and evade this scan). jobcannon/db/migrations/ is
exempt as legitimate schema DDL for the feed_state table.
"""

import ast
import pathlib
import re

ROOTS = ["jobcannon/host", "jobcannon/web", "jobcannon/db", "jobcannon/worker"]
_EXEMPT_SUBSTRINGS = ("/migrations/",)


def _is_forbidden_feed_state_sql(s: str) -> bool:
    # Normalize the same way test_events_single_writer.py does: lowercase,
    # collapse whitespace, drop identifier quoting so `INSERT INTO
    # "feed_state"`, `update  public.feed_state`, and newline-split variants
    # all reduce to a detectable phrase.
    n = re.sub(r"\s+", " ", s.lower()).replace('"', "").replace("`", "")
    if re.search(r"\binsert\s+into\s+(?:public\.)?feed_state\b", n):
        return True
    if re.search(r"\bupdate\s+(?:public\.)?feed_state\b", n):
        return True
    return False


def _iter_scanned_files():
    for root in ROOTS:
        for path in pathlib.Path(root).rglob("*.py"):
            posix = str(path).replace("\\", "/")
            if any(ex in posix for ex in _EXEMPT_SUBSTRINGS):
                continue
            yield path


def test_no_feed_state_writes_outside_migrations():
    offenders = []
    files_walked = 0
    for path in _iter_scanned_files():
        files_walked += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _is_forbidden_feed_state_sql(node.value):
                    offenders.append(str(path))
    # A guard that silently walks zero files (e.g. run from the wrong cwd)
    # would pass vacuously — assert real coverage, not just a clean result.
    assert files_walked > 0, "guard walked zero files — run pytest from the repo root"
    assert not offenders, f"raw feed_state writes outside migrations: {offenders}"


def test_guard_walked_a_nonzero_number_of_files():
    assert sum(1 for _ in _iter_scanned_files()) > 0


def test_forbidden_feed_state_sql_predicate():
    assert _is_forbidden_feed_state_sql("INSERT INTO feed_state (user_id) VALUES (%s)")
    assert _is_forbidden_feed_state_sql("UPDATE feed_state SET rank_score = %s")
    assert not _is_forbidden_feed_state_sql("SELECT * FROM feed_state WHERE user_id = %s")
    assert not _is_forbidden_feed_state_sql("SELECT * FROM postings")
    # normalization: quoted / schema-qualified / newline variants still caught
    assert _is_forbidden_feed_state_sql('INSERT INTO "feed_state" (user_id) VALUES (%s)')
    assert _is_forbidden_feed_state_sql("INSERT INTO public.feed_state (user_id) VALUES (%s)")
    assert _is_forbidden_feed_state_sql("UPDATE\n  feed_state SET rank_score = %s")

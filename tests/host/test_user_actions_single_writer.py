"""AST guard: jobcannon/db/_user_actions.py is the ONLY module allowed to
write to the `watchlists` or `pipeline_status` tables.

Modeled directly on tests/host/test_events_single_writer.py, with one
addition that guard lacks: an explicit non-zero-files-walked assertion. That
guard's own docstring names the gap it left open — "discovery is cwd-relative
with no non-zero-file assertion — run from any other directory it passes
vacuously" — so this guard closes it for the tables it protects, rather than
repeating the same silent-pass hazard in a second file.

Best-effort STATIC lint over string literals, same caveats as the events
guard: it cannot see SQL assembled at runtime (f-strings / concatenation
split the table name across separate literal nodes and evade this scan).
jobcannon/db/_user_actions.py (the sanctioned writer) and
jobcannon/db/migrations/ (legitimate schema DDL) are exempt from the scan.
"""

import ast
import pathlib
import re

ROOTS = ["jobcannon/host", "jobcannon/web", "jobcannon/db", "jobcannon/worker"]
_EXEMPT_SUBSTRINGS = ("jobcannon/db/_user_actions.py", "/migrations/")


def _is_forbidden_write(s: str) -> bool:
    # Normalize the same way the events guard does: lowercase, collapse
    # whitespace, drop identifier quoting so `INSERT INTO "watchlists"`,
    # `insert into  public.pipeline_status`, and newline-split variants all
    # reduce to a detectable phrase.
    n = re.sub(r"\s+", " ", s.lower()).replace('"', "").replace("`", "")
    if re.search(r"\binsert\s+into\s+(?:public\.)?(?:watchlists|pipeline_status)\b", n):
        return True
    if re.search(r"\bupdate\s+(?:public\.)?(?:watchlists|pipeline_status)\b", n):
        return True
    return False


def _walk_scanned_files() -> list[pathlib.Path]:
    return [path for root in ROOTS for path in pathlib.Path(root).rglob("*.py")]


def test_no_watchlist_or_pipeline_writes_outside_the_dal():
    files = _walk_scanned_files()
    assert files, "guard walked zero files — run from the repo root, not a subdirectory"

    offenders = []
    for path in files:
        posix = str(path).replace("\\", "/")
        if any(ex in posix for ex in _EXEMPT_SUBSTRINGS):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _is_forbidden_write(node.value):
                    offenders.append(str(path))
    assert not offenders, (
        f"raw watchlists/pipeline_status SQL outside _user_actions.py: {offenders}"
    )


def test_guard_walked_a_nonzero_number_of_files():
    assert len(_walk_scanned_files()) > 0


def test_forbidden_write_predicate():
    assert _is_forbidden_write("INSERT INTO watchlists (user_id) VALUES (%s)")
    assert _is_forbidden_write("INSERT INTO pipeline_status (user_id) VALUES (%s)")
    assert _is_forbidden_write("UPDATE watchlists SET notes = %s")
    assert _is_forbidden_write("UPDATE pipeline_status SET status = %s")
    assert not _is_forbidden_write("SELECT * FROM watchlists WHERE user_id = %s")
    assert not _is_forbidden_write("SELECT * FROM pipeline_status WHERE user_id = %s")
    assert not _is_forbidden_write("SELECT * FROM postings")
    # normalization: quoted / schema-qualified / newline variants still caught
    assert _is_forbidden_write('INSERT INTO "watchlists" (user_id) VALUES (%s)')
    assert _is_forbidden_write("INSERT INTO public.pipeline_status (user_id) VALUES (%s)")
    assert _is_forbidden_write("INSERT INTO\n  watchlists (user_id) VALUES (%s)")
    # the ON CONFLICT ... DO UPDATE SET form used inside _user_actions.py
    # itself is an INSERT literal, not a bare UPDATE literal, and must not
    # false-positive as a second kind of forbidden statement:
    assert not _is_forbidden_write("UPDATE SET status = EXCLUDED.status")

"""PORT-SEAM: new file, not in the private original (private had no
equivalent AST-scan guard for _state.py -- its equivalent invariant was a
single state.json file plus a Win32 file lock, not a shared table). Mirrors
tests/host/test_score_audits_single_writer.py's shape exactly, retargeted at
the nightly_monitor_state table and its sole sanctioned writer,
jobcannon/host/nightly/state.py (ledger L-0471) -- both state.py's own
module docstring and jobcannon/db/migrations/m0027_nightly_monitor_state.py's
docstring assert this guard exists; this file is that assertion made real.

AST guard: jobcannon/host/nightly/state.py is the ONLY module allowed to
write (INSERT/UPDATE) to the nightly_monitor_state table.

Best-effort STATIC lint over string LITERALS -- see
test_events_single_writer.py's own docstring for the residual
runtime-assembled-SQL gap and why the write boundary (the row-level
SELECT ... FOR UPDATE + three-way merge inside save_state) covers it
regardless of how a caller's SQL was assembled, for any caller that DOES go
through the sanctioned writer.

ROOTS/positive-control shape copied verbatim from
test_events_single_writer.py (see that file for the #45 rationale on
deriving ROOTS from the package layout rather than a hand-maintained list).
"""

import ast
import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "jobcannon"
ROOTS = sorted(
    p.relative_to(_REPO_ROOT).as_posix()
    for p in _PACKAGE_ROOT.iterdir()
    if p.is_dir() and (p / "__init__.py").is_file()
)
_EXEMPT_SUBSTRINGS = ("jobcannon/host/nightly/state.py", "/migrations/")
_SANCTIONED_WRITER = "jobcannon/host/nightly/state.py"


def _is_forbidden_nightly_state_sql(s: str) -> bool:
    n = re.sub(r"\s+", " ", s.lower()).replace('"', "").replace("`", "")
    if re.search(r"\binsert\s+into\s+(?:public\.)?nightly_monitor_state\b", n):
        return True
    if re.search(r"\bupdate\s+(?:public\.)?nightly_monitor_state\b", n):
        return True
    return False


def test_only_nightly_state_module_writes_nightly_monitor_state_table():
    offenders = []
    found_sanctioned_writer = False
    for root in ROOTS:
        for path in (_REPO_ROOT / root).rglob("*.py"):
            posix = path.relative_to(_REPO_ROOT).as_posix()
            if posix == _SANCTIONED_WRITER:
                found_sanctioned_writer = True
            if any(ex in posix for ex in _EXEMPT_SUBSTRINGS):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if _is_forbidden_nightly_state_sql(node.value):
                        offenders.append(posix)
    assert found_sanctioned_writer, (
        f"walk never visited {_SANCTIONED_WRITER} under ROOTS={ROOTS} "
        f"(repo root resolved to {_REPO_ROOT}) -- scan is vacuous, not clean"
    )
    assert not offenders, f"raw nightly_monitor_state-table SQL outside state.py: {offenders}"


def test_forbidden_nightly_state_sql_predicate():
    assert _is_forbidden_nightly_state_sql(
        "INSERT INTO nightly_monitor_state (key, value) VALUES (%s, %s)"
    )
    assert _is_forbidden_nightly_state_sql("UPDATE nightly_monitor_state SET value = %s")
    # A read of the table is fine (the sanctioned reader lives in state.py):
    assert not _is_forbidden_nightly_state_sql("SELECT * FROM nightly_monitor_state WHERE key = %s")
    assert not _is_forbidden_nightly_state_sql("SELECT * FROM scan_health_log")
    # normalization: quoted / schema-qualified / newline variants still caught
    assert _is_forbidden_nightly_state_sql(
        'INSERT INTO "nightly_monitor_state" (key, value) VALUES (%s, %s)'
    )
    assert _is_forbidden_nightly_state_sql(
        "INSERT INTO public.nightly_monitor_state (key, value) VALUES (%s, %s)"
    )
    assert _is_forbidden_nightly_state_sql(
        "INSERT INTO\n  nightly_monitor_state (key, value) VALUES (%s, %s)"
    )

"""PORT-SEAM: new file, not in the private original (private had no
equivalent AST-scan guard for _score_audits.py). Mirrors
tests/host/test_events_single_writer.py's shape exactly, retargeted at the
score_audits table and its sole sanctioned writer,
jobcannon/db/_score_audits.py (ledger L-0079/L-0282).

AST guard: jobcannon/db/_score_audits.py is the ONLY module allowed to write
(INSERT/UPDATE) to the score_audits table.

Best-effort STATIC lint over string LITERALS -- see
test_events_single_writer.py's own docstring for the residual
runtime-assembled-SQL gap and why the write boundary (the ValueError guard
on `verdict` inside record_score_audit) covers it regardless of how a
caller's SQL was assembled, for any caller that DOES go through the
sanctioned writer.

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
_EXEMPT_SUBSTRINGS = ("jobcannon/db/_score_audits.py", "/migrations/")
_SANCTIONED_WRITER = "jobcannon/db/_score_audits.py"


def _is_forbidden_score_audits_sql(s: str) -> bool:
    n = re.sub(r"\s+", " ", s.lower()).replace('"', "").replace("`", "")
    if re.search(r"\binsert\s+into\s+(?:public\.)?score_audits\b", n):
        return True
    if re.search(r"\bupdate\s+(?:public\.)?score_audits\b", n):
        return True
    return False


def test_only_score_audits_module_writes_score_audits_table():
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
                    if _is_forbidden_score_audits_sql(node.value):
                        offenders.append(posix)
    assert found_sanctioned_writer, (
        f"walk never visited {_SANCTIONED_WRITER} under ROOTS={ROOTS} "
        f"(repo root resolved to {_REPO_ROOT}) -- scan is vacuous, not clean"
    )
    assert not offenders, f"raw score_audits-table SQL outside _score_audits.py: {offenders}"


def test_forbidden_score_audits_sql_predicate():
    assert _is_forbidden_score_audits_sql("INSERT INTO score_audits (dedup_key) VALUES (%s)")
    assert _is_forbidden_score_audits_sql("UPDATE score_audits SET notes = %s")
    # A read of the table is fine (the sanctioned reader lives in db/_score_audits.py):
    assert not _is_forbidden_score_audits_sql("SELECT * FROM score_audits WHERE id = %s")
    assert not _is_forbidden_score_audits_sql("SELECT * FROM postings")
    # normalization: quoted / schema-qualified / newline variants still caught
    assert _is_forbidden_score_audits_sql('INSERT INTO "score_audits" (dedup_key) VALUES (%s)')
    assert _is_forbidden_score_audits_sql("INSERT INTO public.score_audits (dedup_key) VALUES (%s)")
    assert _is_forbidden_score_audits_sql("INSERT INTO\n  score_audits (dedup_key) VALUES (%s)")

"""Structural CI guard (issue #183): scoring must not be wired into any
host/web/worker/db module unless a non-NULL ``jd_adjudicated_version``
writer exists under ``jobcannon/db/``.

## The failure mode this guard prevents

``jobcannon.engine.job_scorer.scoring_precheck`` returns
``"awaiting_jd_adjudication"`` for any non-CLEAN jd-content verdict without
an adjudication (job_scorer.py:441-461). Per ``tests/host/test_jd_full.py``,
an ordinary JD body stamps AMBIGUOUS, not CLEAN -- most real postings hit
this branch. Today **nothing writes ``jd_adjudicated_version`` to a
non-NULL value anywhere in this repo**: the only writes are m0009's
``ADD COLUMN`` (NULL default) and ``jobcannon/db/_jd_full.py``'s own
``CASE ... THEN NULL ELSE jd_adjudicated_version END`` invalidation, which
never stamps a value. If a future PR wires ``scoring_precheck`` (or
``score_job``, or a host's ``score_and_persist_job`` hook) into a live
scoring path *without also* shipping a writer, every AMBIGUOUS-verdict
posting -- the majority of real bodies -- silently and permanently drops
out of scoring, with no error surfaced anywhere. #183's three acceptable
resolutions (quoted in the assertion below) are: ship an adjudicator, gate
the AMBIGUOUS branch behind a config flag, or re-scope the gate to
REJECT-only until an adjudicator exists. **This assertion structurally
enforces resolution (1) only** -- it accepts "a writer exists" as the sole
escape hatch. Taking resolution (2) or (3) instead means editing
``scoring_precheck`` itself *and* this assertion's escape condition in the
same PR; a green run here never certifies (2)/(3) on its own, and the
failure message says so explicitly so "Closes #183" stays accurate for
whichever resolution actually ships.

See also: the one-line pointers this same PR adds to
``jobcannon/engine/job_scorer.py``'s module docstring and
``jobcannon/db/migrations/m0009_postings_jd_content.py``'s docstring.

## WIRED: true Python-AST scan (not regex on source)

A hand-list of "scoring entrypoint" names would silently go stale the
moment the engine's surface changes. ``_entrypoint_names()`` instead reads
``jobcannon.engine.job_scorer.__all__`` at import time and keeps only the
callables (``score_job``, ``scoring_precheck`` today -- excludes the data
export ``JOB_ASSESSMENT_SCHEMA`` and the return-type dataclass
``ScoringResult``, neither of which is an entrypoint). It also probes for a
``jobcannon.engine.scoring_orchestrator`` module (the private repo's name
for the not-yet-ported orchestrator referenced in ``services.py``'s
docstring) and folds in its public callables if that module ever lands --
a no-op today, zero edits required later.

The scan set itself is derived from the repo's actual directory tree
(``_repo_python_files_outside_engine()``, ``os.walk`` pruned in place --
never ``Path.rglob``, which cannot skip descending into ``.venv``), not
from a hand-maintained list of package names: every tracked-shape ``*.py``
file in the repo except ``tests/`` and ``jobcannon/engine/`` itself. This
is what put ``scripts/`` (``scripts/run_scan_once.py`` is a real scan
entrypoint) and ``analyses/`` in scope with zero edits to this file, fixing
a prior version of this guard that iterated only ``jobcannon/`` subpackages
and silently missed both. A dedicated test cross-checks the walked set
against an independent ``git ls-files``-based oracle (the same idiom
``scripts/leak_guard.py`` uses) so the two enumeration approaches can never
silently diverge.

``_WiringVisitor`` walks the real ``ast`` tree (``ast.parse`` + a
``NodeVisitor``, never a regex over source text) for every scanned file and
flags:

  (a) a ``score_and_persist_job=`` keyword argument with a non-``None``
      value on ANY call -- covers ``ScanServices(score_and_persist_job=fn)``
      directly and also ``dataclasses.replace(services,
      score_and_persist_job=fn)`` (the correct way to mutate a frozen
      dataclass field), without needing to resolve the callee's identity;
  (b) a plain attribute assignment ``x.score_and_persist_job = <non-None>``;
  (c) an ``ImportFrom`` of an entrypoint name, or any ``Attribute``/``Name``
      node referencing one -- deliberately broad (a bare reference, not
      just a call, still counts as "wired") because a guard that can be
      evaded by assigning the callable to a variable before calling it is
      worthless.

Because this is real AST, not source-text regex, it correctly ignores the
plain-English mentions of ``scoring_precheck`` inside
``jobcannon/db/_jd_full.py`` and ``m0009``'s docstrings (verified: those are
string literal *contents*, never ``Name``/``Attribute``/``ImportFrom``
nodes) -- a regex-over-source approach would false-positive on exactly
those lines.

**Documented, accepted scope boundaries** (checked, not closed -- static
analysis of code that was never executed has hard limits): a call
constructing a services object purely via dict-splat
(``ScanServices(**some_dict)``) carries no visible ``score_and_persist_job``
keyword AST node and evades (a); ``getattr(module, "scoring_precheck")``
string-keyed lookups evade (c) the same way any dynamic-dispatch pattern
evades static analysis. Both are pathological ways to wire scoring that no
code in this repo uses today (grep-confirmed) and that would themselves be
a code-review red flag independent of this guard.

## WRITER: best-effort static lint over SQL string literals

Follows the established idiom in ``tests/host/test_feed_state_not_written.py``
/ ``tests/host/test_events_single_writer.py``: ``ast.walk`` finds every
string ``Constant`` under ``jobcannon/db/`` (this AST walk also transparently
folds adjacent-literal concatenation -- the exact style ``_jd_full.py`` uses
for its multi-line UPDATE -- into one node) that is NOT a docstring (the
first statement of a module/class/function body, identified by
``ast.get_docstring``'s own data model -- see ``_docstring_constant_ids``),
then a normalized-text classifier decides whether it sets
``jd_adjudicated_version`` to a non-NULL value inside a ``SET`` (including
``ON CONFLICT ... DO UPDATE SET``) clause of an ``UPDATE``/``INSERT``
statement -- never a ``WHERE``-clause filter on the same column, and never
prose that merely mentions the column.

**The classifier is fail-closed by design** (PR #238 review, HIGH finding:
an earlier version defaulted an unrecognized RHS shape to "counts as a
writer", which silently disarmed the guard for any SQL shape nobody had
thought to test -- confirmed exploitable via a flipped ``CASE`` branch
order, a ``COALESCE``-wrapped self-reference, and a docstring sentence one
``=`` away from self-disarming). The current rule requires POSITIVE
evidence that the right-hand side of ``jd_adjudicated_version = ...``
produces a real value: a ``%s``/``%(name)s`` placeholder, a bare integer
literal, an ``EXCLUDED.<col>`` upsert reference, or -- for a ``CASE``
expression -- at least one ``THEN`` branch (not ``ELSE``; see
``_case_value_branches``) that is itself one of those same three shapes.
Anything else -- a bare self-reference, a ``COALESCE``-wrapped
self-reference, an unrecognized shape, prose -- is NOT treated as a writer.
The normalized statement is additionally anchored on
``^(update|insert)\\b`` so a ``SET``-shaped phrase inside some other
statement type can't count. Direction check: an unrecognized shape means
``writer_exists=False``, which only matters (fails the guard) if scoring is
also wired -- a false negative here is loud CI red, never a silent pass.
Same static-analysis limits as the existing idiom otherwise: cannot see SQL
assembled at runtime (f-strings / concatenation that splits the column name
across separate literal nodes), and a positional
``INSERT INTO t (..., jd_adjudicated_version, ...) VALUES (..., %(v)s, ...)``
column-list write (no ``column = value`` phrase at all) is a documented gap
this detector does not cover -- no such shape exists in the repo today
(grep-confirmed), and under the fail-closed classifier this gap is now
fail-safe rather than fail-open: an undetected positional writer just means
the guard can't see a writer that exists, which (per the direction check
above) produces a spurious failure if scoring is ever wired, not a silent
pass.
"""

from __future__ import annotations

import ast
import inspect
import os
import pathlib
import re
import subprocess

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WRITER_ROOT = "jobcannon/db"

RESOLUTIONS = (
    "(1) ship an adjudicator (or equivalent) that can set "
    "jd_adjudicated_version non-NULL for a legitimately-AMBIGUOUS-but-"
    "scorable body, or (2) gate the AMBIGUOUS branch of the stamp behind a "
    "config flag until an adjudicator exists, or (3) explicitly re-scope "
    "the gate to REJECT-only until an adjudicator lands -- see issue #183. "
    "This assertion structurally enforces (1) only; taking (2) or (3) "
    "requires editing scoring_precheck AND this assertion's escape "
    "condition in the same PR."
)


def _entrypoint_names() -> frozenset[str]:
    """Derive scoring-entrypoint names from the engine's own public surface.

    Pulls the callables out of ``job_scorer.__all__`` (filters to
    ``inspect.isfunction`` so data/type exports in ``__all__`` don't count),
    then folds in ``scoring_orchestrator``'s public surface if that module
    exists (it does not yet in this repo -- see module docstring).
    """
    from jobcannon.engine import job_scorer

    names: set[str] = set()
    for name in getattr(job_scorer, "__all__", ()):
        obj = getattr(job_scorer, name, None)
        if inspect.isfunction(obj):
            names.add(name)

    try:
        from jobcannon.engine import scoring_orchestrator as _so
    except ImportError:
        _so = None
    if _so is not None:
        public = getattr(_so, "__all__", None) or [n for n in dir(_so) if not n.startswith("_")]
        for name in public:
            obj = getattr(_so, name, None)
            if inspect.isfunction(obj):
                names.add(name)
    return frozenset(names)


def _is_none_constant(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


class _WiringVisitor(ast.NodeVisitor):
    """See module docstring's "WIRED" section for what each rule covers."""

    def __init__(self, entrypoint_names: frozenset[str]):
        self.entrypoint_names = entrypoint_names
        self.hits: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg == "score_and_persist_job" and not _is_none_constant(kw.value):
                self.hits.append("score_and_persist_job= keyword (non-None)")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "score_and_persist_job":
                if not _is_none_constant(node.value):
                    self.hits.append("score_and_persist_job attribute assignment (non-None)")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in self.entrypoint_names:
                self.hits.append(f"import of entrypoint {alias.name!r}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self.entrypoint_names:
            self.hits.append(f"attribute use .{node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.entrypoint_names:
            self.hits.append(f"name reference {node.id!r}")
        self.generic_visit(node)


def _scan_path_for_wiring(path: pathlib.Path, entrypoint_names: frozenset[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _WiringVisitor(entrypoint_names)
    visitor.visit(tree)
    return visitor.hits


def scan_for_wiring(root: pathlib.Path, entrypoint_names: frozenset[str]) -> dict[str, list[str]]:
    """Pure function: AST-walk every *.py under root; return
    {path: [hit descriptions]} for files exhibiting a wiring signal. Reads
    files but never imports or executes the scanned code."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        hits = _scan_path_for_wiring(path, entrypoint_names)
        if hits:
            offenders[str(path)] = hits
    return offenders


def scan_files_for_wiring(
    paths: list[pathlib.Path], entrypoint_names: frozenset[str]
) -> dict[str, list[str]]:
    """Same detector as scan_for_wiring, over an explicit file list rather
    than a directory root -- what the main guard uses, since the scan set
    is now the whole repo minus jobcannon/engine/ and tests/ (see
    _repo_python_files_outside_engine), not a handful of package roots."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(paths):
        hits = _scan_path_for_wiring(path, entrypoint_names)
        if hits:
            offenders[str(path)] = hits
    return offenders


# ---- repo-wide scan-set derivation (not a hand-maintained package list) --

_EXCLUDED_DIR_NAMES = frozenset({"__pycache__", "node_modules"})


def _repo_python_files_outside_engine() -> list[pathlib.Path]:
    """Every *.py file in the repo except tests/ and jobcannon/engine/ --
    derived from the actual directory tree (os.walk, pruned in place so
    .venv/.git/other dot-dirs are never descended into), not from a
    hand-maintained list of "the packages I remember exist". This is what
    puts scripts/, analyses/, and any future top-level package
    automatically in-scope with zero edits to this file."""
    matches: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        rel_parts = pathlib.Path(dirpath).relative_to(_REPO_ROOT).parts
        if rel_parts[:1] == ("tests",) or rel_parts[:2] == ("jobcannon", "engine"):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames if d not in _EXCLUDED_DIR_NAMES and not d.startswith(".")
        ]
        for name in filenames:
            if name.endswith(".py"):
                matches.append(pathlib.Path(dirpath) / name)
    return sorted(matches)


def _tracked_python_files_outside_engine_and_tests() -> set[str]:
    """Independent cross-check: every *.py git considers part of the repo
    (tracked, plus untracked-but-not-gitignored -- same two-command idiom
    scripts/leak_guard.py uses) outside jobcannon/engine/ and tests/, as
    repo-relative posix paths. If this set and the os.walk-derived set ever
    disagree, one of the two file-enumeration approaches has a bug -- a
    plain "non-empty" check alone couldn't catch that."""
    tracked = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    paths = {p.replace("\\", "/") for p in (*tracked, *untracked) if p}
    return {
        p for p in paths if not p.startswith("tests/") and not p.startswith("jobcannon/engine/")
    }


# ---- writer detection ------------------------------------------------------

_STATEMENT_ANCHOR_RE = re.compile(r"^\s*(update|insert)\b", re.IGNORECASE)
_CASE_PREFIX_RE = re.compile(r"^case\s+when\b", re.IGNORECASE)
_THEN_BRANCH_RE = re.compile(
    r"\bthen\b(.*?)(?=\bwhen\b|\belse\b|\bend\b)", re.IGNORECASE | re.DOTALL
)

# Positive evidence ONLY: a placeholder, a bare integer literal, or an
# EXCLUDED.<col> upsert reference. Fail-closed -- anything not matching one
# of these exactly is NOT treated as write evidence (see module docstring's
# WRITER section for why this must never default to True).
_POSITIVE_SIMPLE_RHS_RE = re.compile(
    r"^(?:%\([a-zA-Z_][a-zA-Z0-9_]*\)s|%s|-?\d+|excluded\.[a-zA-Z_][a-zA-Z0-9_]*)$",
    re.IGNORECASE,
)


def _is_positive_write_evidence(rhs: str) -> bool:
    """True only if *rhs* is POSITIVE evidence of a value-producing
    right-hand side. Anything else (a bare column self-reference, a
    COALESCE-wrapped self-reference, prose, an unknown shape) is NOT
    treated as a writer -- an unrecognized shape must read as "not proven
    to write", never as "assumed to write"."""
    return bool(_POSITIVE_SIMPLE_RHS_RE.match(rhs.strip()))


def _case_value_branches(case_text: str) -> list[str]:
    """Every THEN-branch value inside a CASE WHEN ... END blob. WHEN
    conditions and the ELSE fallback are deliberately excluded: per #183's
    fail-closed contract, only an explicit THEN on a matched condition
    counts as an assignment this detector will trust -- treating ELSE as
    equally strong evidence would accept a CASE whose sole affirmative
    branch is NULL and whose only apparent "value" lives in the fallback,
    which is exactly the ambiguous shape this guard exists to catch, not
    wave through."""
    return [m.group(1).strip() for m in _THEN_BRANCH_RE.finditer(case_text)]


def _segment_sets_non_null(segment: str) -> bool:
    """True only if *segment* (the RHS text right after
    `jd_adjudicated_version =` inside a SET clause) is POSITIVE evidence of
    a non-NULL value. Fail-closed: an unrecognized shape returns False,
    never True."""
    seg = segment.strip()
    if _CASE_PREFIX_RE.match(seg):
        return any(_is_positive_write_evidence(b) for b in _case_value_branches(seg))
    return _is_positive_write_evidence(seg)


def _set_clause_assignments(sql: str) -> list[str]:
    """Yield the RHS text of every `jd_adjudicated_version = ...` assignment
    that appears in a SET (incl. ON CONFLICT ... DO UPDATE SET) clause of
    *sql* -- never one appearing in a WHERE filter on the same column."""
    norm = re.sub(r"\s+", " ", sql).strip()
    segments: list[str] = []
    for m in re.finditer(r"jd_adjudicated_version\s*=\s*", norm, re.IGNORECASE):
        prefix = norm[: m.start()]
        set_positions = [p.start() for p in re.finditer(r"\bset\b", prefix, re.IGNORECASE)]
        where_positions = [p.start() for p in re.finditer(r"\bwhere\b", prefix, re.IGNORECASE)]
        if not set_positions:
            continue  # not inside a SET clause at all (e.g. a WHERE filter)
        if where_positions and where_positions[-1] > set_positions[-1]:
            continue  # already past a WHERE relative to the nearest SET
        rest = norm[m.end() :]
        case_m = re.match(r"case\s+when\b.*?\bend\b", rest, re.IGNORECASE | re.DOTALL)
        if case_m:
            segments.append(case_m.group(0))
        else:
            stop_m = re.search(r",|;|\bwhere\b", rest, re.IGNORECASE)
            segments.append(rest[: stop_m.start()] if stop_m else rest)
    return segments


def _sql_writes_jd_adjudicated_version_non_null(sql: str) -> bool:
    norm = re.sub(r"\s+", " ", sql).strip()
    if not _STATEMENT_ANCHOR_RE.match(norm):
        return False
    return any(_segment_sets_non_null(seg) for seg in _set_clause_assignments(sql))


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """id() of every Constant node that constitutes a docstring: the first
    statement of the module body, or of a class/function body, when that
    statement is a bare string-literal expression -- Python's own data
    model for "docstring" (matches what ast.get_docstring recognizes).
    Prose in a docstring must never be mistaken for a SQL string literal
    (PR #238 review, HIGH finding: a docstring sentence mentioning the
    column by name was one edit away from self-disarming the guard)."""
    ids: set[int] = set()
    owners: list[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            owners.append(node)
    for owner in owners:
        body = getattr(owner, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def scan_for_writer(root: pathlib.Path) -> list[str]:
    """Pure function: AST-walk every *.py under root for a non-docstring
    string literal whose SQL sets jd_adjudicated_version to a non-NULL
    value; return the matching file paths."""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstring_ids = _docstring_constant_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids
            ):
                if _sql_writes_jd_adjudicated_version_non_null(node.value):
                    offenders.append(str(path))
                    break
    return offenders


# ---- the guard -----------------------------------------------------------


def test_scoring_not_wired_without_jd_adjudicated_version_writer():
    entrypoint_names = _entrypoint_names()
    assert entrypoint_names, (
        "no scoring entrypoints derived from job_scorer.__all__ -- the "
        "derivation is broken, not proof there's nothing to guard"
    )

    scan_files = _repo_python_files_outside_engine()
    # Positive control: a broken repo-root resolution would silently walk
    # zero files and pass vacuously.
    assert scan_files, "guard walked zero files -- run pytest from the repo root"

    wiring_hits = scan_files_for_wiring(scan_files, entrypoint_names)
    writer_hits = scan_for_writer(_REPO_ROOT / WRITER_ROOT)

    wired = bool(wiring_hits)
    writer_exists = bool(writer_hits)
    assert not wired or writer_exists, (
        f"scoring appears wired ({sorted(wiring_hits)}) with no "
        f"jd_adjudicated_version writer under {WRITER_ROOT}/ -- this "
        f"silently and permanently starves every AMBIGUOUS-verdict posting "
        f"from scoring (#183). Resolution (1) (ship a writer) is what this "
        f"assertion structurally enforces. If you're instead taking "
        f"resolution (2) (config-flag the AMBIGUOUS branch) or (3) "
        f"(re-scope scoring_precheck to REJECT-only), you must edit "
        f"scoring_precheck itself AND this assertion's escape condition in "
        f"the same PR -- a green run here does not certify (2)/(3), only "
        f"(1). Full text: {RESOLUTIONS}"
    )


# ---- positive controls: the guard must be able to fire -------------------


def test_detector_fires_on_fake_wiring_via_scanservices_keyword(tmp_path):
    entrypoint_names = _entrypoint_names()
    (tmp_path / "fake_host.py").write_text(
        "from jobcannon.engine.services import ScanServices\n"
        "\n"
        "def build():\n"
        "    return ScanServices(score_and_persist_job=lambda *a, **k: None)\n",
        encoding="utf-8",
    )
    hits = scan_for_wiring(tmp_path, entrypoint_names)
    assert hits, "detector failed to fire on ScanServices(score_and_persist_job=...) wiring"


def test_detector_fires_on_dataclasses_replace_keyword(tmp_path):
    """Pins the visit_Call rule's breadth: it must fire on ANY call's
    keywords, not just a ScanServices(...) call specifically."""
    entrypoint_names = _entrypoint_names()
    (tmp_path / "fake_host.py").write_text(
        "import dataclasses\n"
        "from jobcannon.engine.services import ScanServices\n"
        "\n"
        "def rewire(existing: ScanServices):\n"
        "    return dataclasses.replace(existing, score_and_persist_job=lambda *a, **k: None)\n",
        encoding="utf-8",
    )
    hits = scan_for_wiring(tmp_path, entrypoint_names)
    assert hits, "detector failed to fire on dataclasses.replace(..., score_and_persist_job=...)"


def test_detector_fires_on_attribute_assignment(tmp_path):
    """Pins the visit_Assign rule."""
    entrypoint_names = _entrypoint_names()
    (tmp_path / "fake_host.py").write_text(
        "def rewire(services):\n    services.score_and_persist_job = lambda *a, **k: None\n",
        encoding="utf-8",
    )
    hits = scan_for_wiring(tmp_path, entrypoint_names)
    assert hits, "detector failed to fire on a plain score_and_persist_job attribute assignment"


def test_detector_fires_on_importfrom_entrypoint(tmp_path):
    """Pins the visit_ImportFrom rule."""
    entrypoint_names = _entrypoint_names()
    (tmp_path / "fake_web.py").write_text(
        "from jobcannon.engine.job_scorer import scoring_precheck\n",
        encoding="utf-8",
    )
    hits = scan_for_wiring(tmp_path, entrypoint_names)
    assert hits, "detector failed to fire on `from ... import scoring_precheck`"


def test_detector_fires_on_aliased_attribute_reference(tmp_path):
    """Pins the visit_Attribute rule."""
    entrypoint_names = _entrypoint_names()
    (tmp_path / "fake_web.py").write_text(
        "import jobcannon.engine.job_scorer as js\n\ndef handler():\n    return js.scoring_precheck\n",
        encoding="utf-8",
    )
    hits = scan_for_wiring(tmp_path, entrypoint_names)
    assert hits, "detector failed to fire on an aliased-module attribute reference"


def test_detector_fires_on_bare_name_reference_via_functools_partial(tmp_path):
    """Pins the visit_Name rule."""
    entrypoint_names = _entrypoint_names()
    (tmp_path / "fake_worker.py").write_text(
        "import functools\n\ndef build_task():\n    return functools.partial(score_job, force=True)\n",
        encoding="utf-8",
    )
    hits = scan_for_wiring(tmp_path, entrypoint_names)
    assert hits, "detector failed to fire on a bare entrypoint-name reference"


def test_detector_fires_on_fake_writer(tmp_path):
    (tmp_path / "fake_db.py").write_text(
        'SQL = "UPDATE postings SET jd_adjudicated_version = %(v)s WHERE dedup_key = %(k)s"\n',
        encoding="utf-8",
    )
    offenders = scan_for_writer(tmp_path)
    assert offenders, "detector failed to fire on a real jd_adjudicated_version writer"


def test_detector_stays_quiet_for_null_and_case_invalidation_shapes(tmp_path):
    (tmp_path / "fake_db.py").write_text(
        'NULL_SET = "UPDATE postings SET jd_adjudicated_version = NULL WHERE dedup_key = %(k)s"\n'
        "CASE_INVALIDATE = (\n"
        '    "UPDATE postings SET "\n'
        '    "jd_adjudicated_version = CASE WHEN jd_full IS DISTINCT FROM %(text)s "\n'
        '    "THEN NULL ELSE jd_adjudicated_version END "\n'
        '    "WHERE dedup_key = %(dedup_key)s"\n'
        ")\n",
        encoding="utf-8",
    )
    offenders = scan_for_writer(tmp_path)
    assert not offenders, f"detector false-fired on NULL/CASE invalidation shapes: {offenders}"


def test_detector_skips_docstrings_even_when_they_mimic_a_bare_writer_statement(tmp_path):
    """Adversarial: a module docstring that starts with "UPDATE" and ends
    exactly at a non-NULL-looking RHS (no trailing WHERE) is structurally
    indistinguishable from real SQL once matched in isolation -- nothing in
    the fail-closed whitelist itself tells prose apart from SQL. This is
    what makes the AST-level docstring-node skip a genuinely necessary,
    independent layer (PR #238 review, HIGH finding) rather than redundant
    with the anchor/whitelist changes."""
    (tmp_path / "fake_db.py").write_text(
        '"""UPDATE note: earlier code used to SET jd_adjudicated_version = 1"""\n'
        "\n"
        "def helper():\n"
        "    pass\n",
        encoding="utf-8",
    )
    offenders = scan_for_writer(tmp_path)
    assert not offenders, f"detector treated a module docstring as a SQL writer: {offenders}"


def test_guard_scan_set_derived_not_hand_pinned_and_covers_scripts():
    """Replaces a former hand-maintained WIRING_ROOTS allowlist (LOW
    finding, PR #238 review) that pinned {db,host,web,worker} and silently
    missed scripts/, analyses/, and any future top-level package. The scan
    set is now cross-checked against an independent git-based oracle
    (mirrors scripts/leak_guard.py's tracked + untracked-not-ignored idiom)
    so the os.walk traversal and git's own view of the repo can never
    silently diverge."""
    walked = {p.relative_to(_REPO_ROOT).as_posix() for p in _repo_python_files_outside_engine()}
    oracle = _tracked_python_files_outside_engine_and_tests()
    assert walked, "os.walk-derived scan set resolved empty -- traversal is broken"
    assert not any(p.startswith("jobcannon/engine/") for p in walked)
    assert not any(p.startswith("tests/") for p in walked)
    assert walked == oracle, (
        f"os.walk scan set disagrees with git's view of the repo: "
        f"walked-only={sorted(walked - oracle)} git-only={sorted(oracle - walked)}"
    )
    assert any(p.startswith("scripts/") for p in walked), (
        "scripts/ is a real scan-entrypoint location (scripts/run_scan_once.py) "
        "and must be in-scope without naming that file specifically"
    )


# ---- predicate unit tests -------------------------------------------------


def test_writer_predicate_shapes():
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = %(v)s WHERE dedup_key = %(k)s"
    )
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = EXCLUDED.jd_adjudicated_version"
    )
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = 3 WHERE dedup_key = %(k)s"
    )
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = NULL WHERE dedup_key = %(k)s"
    )
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET "
        "jd_adjudicated_version = CASE WHEN jd_full IS DISTINCT FROM %(text)s "
        "THEN NULL ELSE jd_adjudicated_version END "
        "WHERE dedup_key = %(dedup_key)s"
    )
    # a WHERE-clause filter on the column is a read, not a write
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "SELECT * FROM postings WHERE jd_adjudicated_version = %(v)s"
    )
    # exact real-tree shape, verbatim from jobcannon/db/_jd_full.py
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_full = %(text)s, unresolved_reasons = %(reasons)s, "
        "jd_content_verdict = CASE WHEN jd_full IS DISTINCT FROM %(text)s "
        "OR jd_content_verdict IS NULL THEN %(verdict)s ELSE jd_content_verdict END, "
        "jd_content_signal = CASE WHEN jd_full IS DISTINCT FROM %(text)s "
        "OR jd_content_verdict IS NULL THEN %(signal)s ELSE jd_content_signal END, "
        "jd_adjudicated_version = CASE WHEN jd_full IS DISTINCT FROM %(text)s "
        "THEN NULL ELSE jd_adjudicated_version END "
        "WHERE dedup_key = %(dedup_key)s"
    )


def test_writer_classifier_rejects_flipped_case_self_reference():
    """Refuter probe (HIGH finding): a CASE whose THEN branch echoes the
    column itself must not count as a writer -- only the old default-True
    fallback made this look like one."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = "
        "CASE WHEN jd_full IS DISTINCT FROM %(text)s "
        "THEN jd_adjudicated_version ELSE NULL END "
        "WHERE dedup_key = %(dedup_key)s"
    )


def test_writer_classifier_rejects_coalesce_wrapped_self_reference():
    """Refuter probe (HIGH finding): COALESCE(jd_adjudicated_version, 0) in
    an ELSE branch is not positive evidence -- THEN-only evaluation and the
    fail-closed whitelist both reject it."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = "
        "CASE WHEN jd_full IS DISTINCT FROM %(text)s "
        "THEN NULL ELSE COALESCE(jd_adjudicated_version, 0) END "
        "WHERE dedup_key = %(dedup_key)s"
    )


def test_writer_classifier_rejects_self_assign_noop():
    """Devin probe (HIGH finding): a no-op self-assignment is not a writer."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = jd_adjudicated_version "
        "WHERE dedup_key = %(dedup_key)s"
    )


def test_writer_classifier_rejects_non_update_insert_statement():
    """The ^(update|insert) anchor: a SET-shaped phrase inside a statement
    that isn't an UPDATE/INSERT this detector models must not count."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "CREATE TRIGGER t AFTER UPDATE ON postings BEGIN SET jd_adjudicated_version = 1; END"
    )

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
REJECT-only until an adjudicator exists. This test makes "don't merge the
wiring PR without one of those" a CI-enforced precondition instead of a
comment someone has to remember.

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

``_WiringVisitor`` walks the real ``ast`` tree (``ast.parse`` + a
``NodeVisitor``, never a regex over source text) for every ``*.py`` under
every top-level ``jobcannon/`` subpackage EXCEPT ``jobcannon/engine``
itself (derived from the package layout, same idiom as
``tests/host/test_events_single_writer.py``'s ``ROOTS``) and flags:

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

## WRITER: best-effort static lint over SQL string literals

Follows the established idiom in ``tests/host/test_feed_state_not_written.py``
/ ``tests/host/test_events_single_writer.py``: ``ast.walk`` finds every
string ``Constant`` under ``jobcannon/db/`` (this AST walk also transparently
folds adjacent-literal concatenation -- the exact style ``_jd_full.py`` uses
for its multi-line UPDATE -- into one node), then a normalized-text
classifier decides whether it sets ``jd_adjudicated_version`` to a non-NULL
value inside a ``SET`` (including ``ON CONFLICT ... DO UPDATE SET``) clause
-- never a ``WHERE``-clause filter on the same column. Per #183's spec,
``= %(param)s``, ``= EXCLUDED.col``, and ``= <integer>`` all count as a
writer; a bare ``= NULL`` and the exact
``CASE WHEN ... THEN NULL ELSE jd_adjudicated_version END`` invalidation
shape ``_jd_full.py`` uses do not. Same static-analysis limits as the
existing idiom: cannot see SQL assembled at runtime (f-strings /
concatenation that splits the column name across separate literal nodes),
and a positional ``INSERT INTO t (..., jd_adjudicated_version, ...) VALUES
(..., %(v)s, ...)`` column-list write (no ``column = value`` phrase at all)
is a documented gap this detector does not cover -- no such shape exists in
the repo today (grep-confirmed).
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _REPO_ROOT / "jobcannon"

# Derived from the package layout (every jobcannon/ subpackage with an
# __init__.py, minus jobcannon/engine) -- never a hand-maintained list, so a
# future new top-level package is automatically covered. Mirrors
# tests/host/test_events_single_writer.py's ROOTS idiom.
WIRING_ROOTS = sorted(
    p.relative_to(_REPO_ROOT).as_posix()
    for p in _PACKAGE_ROOT.iterdir()
    if p.is_dir() and (p / "__init__.py").is_file() and p.name != "engine"
)
WRITER_ROOT = "jobcannon/db"

RESOLUTIONS = (
    "(1) ship an adjudicator (or equivalent) that can set "
    "jd_adjudicated_version non-NULL for a legitimately-AMBIGUOUS-but-"
    "scorable body, or (2) gate the AMBIGUOUS branch of the stamp behind a "
    "config flag until an adjudicator exists, or (3) explicitly re-scope "
    "the gate to REJECT-only until an adjudicator lands -- see issue #183."
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


def scan_for_wiring(root: pathlib.Path, entrypoint_names: frozenset[str]) -> dict[str, list[str]]:
    """Pure function: AST-walk every *.py under root; return
    {path: [hit descriptions]} for files exhibiting a wiring signal. Reads
    files but never imports or executes the scanned code."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _WiringVisitor(entrypoint_names)
        visitor.visit(tree)
        if visitor.hits:
            offenders[str(path)] = visitor.hits
    return offenders


# ---- writer detection --------------------------------------------------

_INVALIDATION_CASE_RE = re.compile(
    r"^case\s+when\s+.+?\s+then\s+null\s+else\s+jd_adjudicated_version\s+end$",
    re.IGNORECASE | re.DOTALL,
)
_NULL_ONLY_RE = re.compile(r"^null$", re.IGNORECASE)


def _segment_sets_non_null(segment: str) -> bool:
    """True if *segment* (RHS text right after `jd_adjudicated_version =`
    inside a SET clause) sets the column to something other than NULL.
    Excludes exactly a bare NULL and the CASE...THEN NULL ELSE
    jd_adjudicated_version END invalidation idiom -- any OTHER CASE shape
    (e.g. one assigning a real value on some branch) still counts."""
    seg = segment.strip()
    if _NULL_ONLY_RE.match(seg):
        return False
    if _INVALIDATION_CASE_RE.match(seg):
        return False
    return True


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
    return any(_segment_sets_non_null(seg) for seg in _set_clause_assignments(sql))


def scan_for_writer(root: pathlib.Path) -> list[str]:
    """Pure function: AST-walk every *.py under root for a string literal
    whose SQL sets jd_adjudicated_version to a non-NULL value; return the
    matching file paths."""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
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

    wiring_hits: dict[str, list[str]] = {}
    files_walked = 0
    for root in WIRING_ROOTS:
        root_path = _REPO_ROOT / root
        files_walked += sum(1 for _ in root_path.rglob("*.py"))
        wiring_hits.update(scan_for_wiring(root_path, entrypoint_names))
    # Positive control: a broken WIRING_ROOTS/repo-root resolution would
    # silently walk zero files and pass vacuously.
    assert files_walked > 0, "guard walked zero files -- run pytest from the repo root"

    writer_hits = scan_for_writer(_REPO_ROOT / WRITER_ROOT)

    wired = bool(wiring_hits)
    writer_exists = bool(writer_hits)
    assert not wired or writer_exists, (
        f"scoring appears wired ({sorted(wiring_hits)}) with no "
        f"jd_adjudicated_version writer under {WRITER_ROOT}/ -- this "
        f"silently and permanently starves every AMBIGUOUS-verdict posting "
        f"from scoring (#183). Resolve one of: {RESOLUTIONS}"
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


def test_guard_roots_exclude_engine_and_are_nonempty():
    assert "jobcannon/engine" not in WIRING_ROOTS
    assert WIRING_ROOTS, "WIRING_ROOTS resolved empty -- package layout probe is broken"
    assert set(WIRING_ROOTS) <= {
        "jobcannon/db",
        "jobcannon/host",
        "jobcannon/web",
        "jobcannon/worker",
    }

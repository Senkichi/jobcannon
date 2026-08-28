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

The scan set (``_scan_scope_files()``) is git's own view of the repo --
tracked files plus untracked-but-not-``.gitignore``d ones (the same
``git ls-files`` / ``git ls-files --others --exclude-standard`` idiom
``scripts/leak_guard.py`` uses), filtered to exclude ``tests/`` and
``jobcannon/engine/``. This is what put ``scripts/``
(``scripts/run_scan_once.py`` is a real scan entrypoint) and ``analyses/``
in scope with zero edits to this file, fixing a prior version of this guard
that iterated only ``jobcannon/`` subpackages and silently missed both.
Git's own view is trusted directly, with no second, independently-derived
``os.walk``-based file list cross-checked against it (PR #238 re-review
round 4, LOW finding: an earlier version kept exactly such a cross-check
as a "superset" sanity assertion, but ``os.walk`` prunes every
dot-directory to avoid descending into ``.venv``/``.git`` -- which also
silently drops a *tracked*, in-scope file like a ``.github/scripts/*.py``
CI helper, so the cross-check went CI-red on an ordinary repo change
completely unrelated to the guard's own purpose, on a branch that itself
edits ``.github/workflows/ci.yml``. Deleted rather than patched to also
prune dot-dirs from the oracle: the walk could only ever be a strict
subset of git's view, never real defense-in-depth). The one exclusion
that does matter -- a gitignored ``.py`` outside any dot-directory, e.g. a
local ``uv build``'s ``dist/*.py`` artifact -- git's own view already
handles correctly: git tracks it never, so it is never in scope (PR #238
re-review, LOW finding).

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
literal, an ``EXCLUDED.<col>`` upsert reference, a ``COALESCE(<one of
those three>, ...)`` wrapper (first argument only -- this repo's own idiom
for exactly this kind of write, e.g. ``jobcannon/db/_companies.py``'s
``SET homepage_url = COALESCE(%s, homepage_url)`` at lines 143 and 150; PR
#238 re-review, LOW finding), or -- for a ``CASE`` expression -- at least
one ``THEN`` branch (not ``ELSE``; see ``_case_value_branches``) that is
itself one of those shapes. Anything else -- a bare self-reference, a
``COALESCE``-wrapped self-reference, an unrecognized shape, prose -- is NOT
treated as a writer. A ``CASE`` whose captured text itself contains a
*second* ``case`` token (i.e. a nested ``CASE`` inside a ``WHEN``
condition, such as ``CASE WHEN (CASE WHEN x THEN 1 ELSE 0 END) = 1 THEN
NULL ELSE col END``) is refused outright rather than parsed: the
non-greedy capture that finds a segment's ``END`` stops at the *inner*
``END``, so a naively-extracted ``THEN`` branch could belong to the inner,
condition-only ``CASE`` and never the outer value -- PR #238 re-review, LOW
finding. Refusing beats mis-parsing here; the fail-closed default (not a
writer) already covers it correctly.

The normalized statement is anchored not just on the ``UPDATE``/``INSERT``
keyword but on the surrounding SQL **shape**:
``^(update\\s+(only\\s+)?[\\w."]+\\s+set|insert\\s+into)\\b`` (allows
``UPDATE ONLY`` and a schema-qualified ``update public.t set``). This is
what actually closes the prose vector at its root (PR #238 re-review, LOW
finding: the keyword-only anchor let a plain-English sentence like
``"UPDATE flow: we SET jd_adjudicated_version = 1, then commit"`` -- a
realistic non-docstring string, e.g. a migration's ``description=`` kwarg
constant -- read as a real ``UPDATE ... SET`` statement, since nothing
about the RHS whitelist alone tells "prose that happens to end at a
plausible value" apart from real SQL). **The AST-level docstring-node skip
(``_docstring_constant_ids``) is a separate, independent defence-in-depth
layer, not the fix for prose in general** -- it is currently INERT on this
repo's real tree (``scan_for_writer(jobcannon/)`` returns the same ``[]``
whether or not the skip runs), because nothing under ``jobcannon/db/``
today puts writer-shaped prose in a first-statement string. It stays
because an adversarial docstring built specifically to also satisfy the
anchor and the whitelist (ending exactly at a bare, comma-free numeral with
no trailing ``WHERE``) is still only caught by this layer -- see
``test_detector_skips_docstrings_even_when_they_mimic_a_bare_writer_statement``.

A trailing ``RETURNING ...`` or ``UPDATE ... FROM ...`` clause immediately
after the target assignment, with no intervening ``WHERE``, is correctly
handled (PR #238 re-review round 4, LOW finding: ``_top_level_segment_stop``
now also stops at a top-level, word-bounded ``RETURNING``/``FROM``, the same
treatment it already gave ``WHERE`` -- paren-depth-0 only, so
``EXTRACT(epoch FROM ts)`` and a ``CASE WHEN ... IS DISTINCT FROM ...``
condition, both real idioms in this repo, are unaffected). The
statement-splitting itself (see ``_sql_writes_jd_adjudicated_version_non_null``)
iterates rather than recurses across ``;``-separated statements, so an
arbitrarily long semicolon-batched string cannot raise ``RecursionError``.

Direction check: an unrecognized shape means ``writer_exists=False``, which
only matters (fails the guard) if scoring is also wired -- a false
negative here is loud CI red, never a silent pass. Documented fail-closed
gaps -- static-analysis limits this detector accepts rather than chases,
because they are all safe-direction (false negative, not false positive)
and none exists in the repo today (grep-confirmed): SQL assembled at
runtime (f-strings / ``.format`` / ``psycopg.sql.SQL`` composition that
splits the column name across separate literal nodes); a
``WITH ... UPDATE`` CTE-prefixed statement (the anchor requires ``update``
at the very start); a leading SQL comment before ``UPDATE``/``INSERT``
(same reason); ``$1``-style positional parameters (not in the RHS
whitelist); a row-constructor ``SET (a, b) = (1, %s)`` write (no
``column = value`` phrase for the regex to find at all); the simple-form
``CASE col WHEN value THEN ...`` (vs. this repo's own
``CASE WHEN <condition> THEN ...`` idiom -- ``_CASE_PREFIX_RE`` only
recognizes the latter); the already-documented positional
``INSERT INTO t (..., jd_adjudicated_version, ...) VALUES (..., %(v)s, ...)``
column-list write; a ``$$...$$`` dollar-quoted string body (the
paren+quote-aware boundary scanner tracks single-quoted literals only, not
Postgres dollar-quoting, so a ``;`` or comma inside one can be misread as a
top-level boundary); an apostrophe inside a ``--`` line comment or
``/* */`` block comment (the same single-quote tracking has no comment
awareness, so it flips quote-state on that apostrophe -- safe-direction
truncation of the segment being scanned, never a fail-open); and a table
alias directly after the target table name (e.g.
``UPDATE t SET col = %s FROM other o WHERE o.id = t.id``, the standard
alias form of ``UPDATE ... FROM``) -- the anchor requires exactly one
``[\\w."]+`` token between ``UPDATE [ONLY]`` and ``SET``, so an alias
token in between fails the match entirely (PR #238 re-review round 4:
surfaced by the refuter's own probe while verifying the RETURNING/FROM
fix above; the alias-free form,
``UPDATE postings SET col = %s FROM other o WHERE o.id = postings.id``,
is unaffected and classifies correctly). Under the
fail-closed classifier every one of these is fail-safe: an undetected
writer just means the guard can't see a writer that exists, which produces
a spurious failure if scoring is ever wired, never a silent pass.
"""

from __future__ import annotations

import ast
import inspect
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
    _scan_scope_files), not a handful of package roots."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(paths):
        hits = _scan_path_for_wiring(path, entrypoint_names)
        if hits:
            offenders[str(path)] = hits
    return offenders


# ---- repo-wide scan-set derivation (not a hand-maintained package list) --


def _scan_scope_files() -> list[pathlib.Path]:
    """The actual scan set the guard trusts: git's own view of the repo
    (see module docstring). This is git's own authoritative "every *.py we
    own" set -- tracked plus untracked-but-not-gitignored -- so there is no
    second, independently-derived file list to cross-check it against; a
    plain ``os.walk`` traversal cannot see any file this doesn't (the only
    files a walk reaches that git doesn't are gitignored, which the guard
    must never scan) and DOES see files this deliberately excludes (any
    dot-directory, e.g. ``.venv``, ``.git`` -- but also a legitimately
    tracked one like ``.github/scripts/*.py``, which git-oracle correctly
    keeps in scope and a dot-dir-pruning walk would not). A former
    ``os.walk``-based superset cross-check was deleted (PR #238 re-review
    round 4, LOW finding) rather than patched to also prune dot-dirs from
    this set: the walk could only ever be a strict subset of what git
    tracks, never a meaningful independent check, so keeping it made an
    invalid state representable (a green repo turning CI-red the moment
    anyone added an ordinary tracked ``.py`` under any dot-directory, e.g.
    a ``.github/scripts/`` CI helper) for zero real defense-in-depth."""
    return sorted(_REPO_ROOT / p for p in _tracked_python_files_outside_engine_and_tests())


def _tracked_python_files_outside_engine_and_tests() -> set[str]:
    """The authoritative scan-set derivation (see _scan_scope_files, which
    is a thin wrapper over this): every *.py git considers part of the
    repo (tracked, plus untracked-but-not-gitignored -- same two-command
    idiom scripts/leak_guard.py uses) outside jobcannon/engine/ and
    tests/, as repo-relative posix paths. No second, independently-derived
    file-enumeration approach exists to cross-check this against (PR #238
    re-review round 4 deleted the former os.walk-based one -- see module
    docstring's WIRED section for why)."""
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

# Anchored on SQL SHAPE, not just the keyword: requires an actual `... SET`
# phrase after UPDATE (optionally `UPDATE ONLY`, optionally schema-qualified
# `db.table`) or a literal `INSERT INTO`. A bare `^(update|insert)\b` let
# plain-English prose starting with "UPDATE ..." read as SQL (PR #238
# re-review, LOW finding) -- see module docstring's WRITER section.
_STATEMENT_ANCHOR_RE = re.compile(
    r'^(update\s+(only\s+)?[\w."]+\s+set|insert\s+into)\b', re.IGNORECASE
)
_CASE_PREFIX_RE = re.compile(r"^case\s+when\b", re.IGNORECASE)
_CASE_TOKEN_RE = re.compile(r"\bcase\b", re.IGNORECASE)
_THEN_BRANCH_RE = re.compile(
    r"\bthen\b(.*?)(?=\bwhen\b|\belse\b|\bend\b)", re.IGNORECASE | re.DOTALL
)

# Positive evidence: a placeholder, a bare integer literal, an
# EXCLUDED.<col> upsert reference, or a COALESCE(...) wrapping one of those
# three as its FIRST argument (this repo's own idiom for exactly this kind
# of write -- see module docstring's WRITER section). Fail-closed --
# anything not matching one of these exactly is NOT treated as write
# evidence (see module docstring's WRITER section for why this must never
# default to True).
_POSITIVE_BASE_RHS = r"%\([a-zA-Z_][a-zA-Z0-9_]*\)s|%s|-?\d+|excluded\.[a-zA-Z_][a-zA-Z0-9_]*"
_POSITIVE_SIMPLE_RHS_RE = re.compile(
    rf"^(?:{_POSITIVE_BASE_RHS}|coalesce\(\s*(?:{_POSITIVE_BASE_RHS})\s*(?:,.*)?\))$",
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
        if len(_CASE_TOKEN_RE.findall(seg)) > 1:
            # Nested CASE inside a WHEN condition: _set_clause_assignments'
            # non-greedy capture stops at the INNER `end`, so any THEN
            # branch pulled from this text could belong to the inner,
            # condition-only CASE rather than the outer value. Refuse to
            # parse it at all rather than risk misreading it (PR #238
            # re-review, LOW finding) -- fail-closed default (not a writer)
            # already covers this correctly.
            return False
        return any(_is_positive_write_evidence(b) for b in _case_value_branches(seg))
    return _is_positive_write_evidence(seg)


def _top_level_positions(text: str) -> set[int]:
    """Indices in *text* that sit at paren-DEPTH-ZERO and OUTSIDE a
    single-quoted SQL string literal (a `''`-doubled quote escapes without
    ending the literal). The only positions where a comma/semicolon/WHERE
    keyword may legitimately act as a clause or statement boundary -- one
    nested inside a function call's own argument list (e.g. the comma
    inside `COALESCE(%s, jd_adjudicated_version)`) or inside a quoted
    string literal (e.g. the `;` inside `'note: a;b'`) must never count
    (PR #238 re-review round 2: the original per-consumer scans tracked
    paren depth but not quote state, and only bounded a SET-clause
    segment's END, never a whole STATEMENT's end at the next `;` -- see
    _first_top_level_semicolon)."""
    positions: set[int] = set()
    depth = 0
    in_quote = False
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if in_quote:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth <= 0:
            positions.add(i)
        i += 1
    return positions


_SEGMENT_STOP_KEYWORDS = ("where", "returning", "from")


def _top_level_segment_stop(rest: str) -> int | None:
    """Index in *rest* where a (non-CASE) SET-clause segment ends: the
    first paren-DEPTH-ZERO, outside-quotes comma/semicolon, or one of
    WHERE/RETURNING/FROM as a whole word. A trailing `RETURNING id` or
    `FROM other WHERE ...` right after the target assignment, with no
    intervening WHERE, used to leave that keyword (and everything after
    it) inside the RHS text handed to the whitelist -- which then
    correctly failed to recognize a real write as one of the fixed
    positive shapes, a fail-closed false negative (PR #238 re-review round
    4). The word-boundary + paren-depth-0 check applies identically to all
    three keywords, so `EXTRACT(epoch FROM ts)` / `SUBSTRING(x FROM 1)`
    inside a function call's own argument list are never mistaken for a
    statement-level FROM (nested inside parens, never at depth 0) -- and a
    CASE-prefixed segment (e.g. a `WHEN ... IS DISTINCT FROM ...`
    condition, this repo's own idiom) never reaches this function at all,
    since _set_clause_assignments routes CASE segments through a separate
    regex match before this one ever runs."""
    top_level = _top_level_positions(rest)
    n = len(rest)
    for i in range(n):
        if i not in top_level:
            continue
        ch = rest[i]
        if ch in ",;":
            return i
        for kw in _SEGMENT_STOP_KEYWORDS:
            klen = len(kw)
            if (
                rest[i : i + klen].lower() == kw
                and (i == 0 or not rest[i - 1].isalnum())
                and (i + klen == n or not rest[i + klen].isalnum())
            ):
                return i
    return None


def _first_top_level_semicolon(text: str) -> int | None:
    """Index of the first `;` in *text* that sits at paren-DEPTH-ZERO and
    outside a quoted string literal -- the boundary between one SQL
    statement and the next. A `;` inside a string literal or nested inside
    parentheses never counts (PR #238 re-review round 2: the
    semicolon-batched multi-statement fail-open the refuter's diagnostic
    probe surfaced -- see _sql_writes_jd_adjudicated_version_non_null)."""
    top_level = _top_level_positions(text)
    for i, ch in enumerate(text):
        if ch == ";" and i in top_level:
            return i
    return None


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
            stop_idx = _top_level_segment_stop(rest)
            segments.append(rest[:stop_idx] if stop_idx is not None else rest)
    return segments


def _sql_writes_jd_adjudicated_version_non_null(sql: str) -> bool:
    """Fail-closed, and bounded ONE STATEMENT AT A TIME: a semicolon-
    batched multi-statement string is walked one top-level `;`-separated
    segment at a time (paren-depth-0, outside-quotes -- see
    _first_top_level_semicolon), and only the text up to each boundary --
    that segment's own text -- is searched for its SET-clause assignment.
    Text past a boundary belongs to a DIFFERENT statement and is never
    attributed to the segment before it; it is instead re-anchored and
    classified independently as its own segment on the next loop iteration
    (PR #238 re-review round 2, closing the residual the refuter's
    diagnostic-only probe flagged but left out of its actionable findings
    that round): `"UPDATE postings SET jd_full = %(t)s; SELECT
    jd_adjudicated_version = 1"` -- an UPDATE that never touches the
    column, followed by an unrelated SELECT whose operand happens to look
    like positive evidence -- used to fire True because the old version
    scanned the WHOLE remaining string with no boundary at the `;` at all.
    Conversely `"SELECT 1; UPDATE postings SET jd_adjudicated_version =
    %(v)s"` must still fire True: the second statement is a real,
    independently-anchored write, and re-anchoring (not just bounding) is
    what keeps that case correct.

    Iterative, not recursive (PR #238 re-review round 4, INFO finding): a
    pathological string batching thousands of top-level `;`-separated
    statements would blow Python's default recursion limit under a
    once-per-`;` recursive split -- fail-loud, and worse than the
    fail-closed default this detector otherwise guarantees. Same semantics
    either way (each segment re-anchored and classified independently, any
    True wins), just an explicit loop instead of a call stack."""
    norm = re.sub(r"\s+", " ", sql).strip()
    while norm:
        boundary = _first_top_level_semicolon(norm)
        head = norm[:boundary] if boundary is not None else norm
        if _STATEMENT_ANCHOR_RE.match(head) and any(
            _segment_sets_non_null(seg) for seg in _set_clause_assignments(head)
        ):
            return True
        if boundary is None:
            return False
        norm = norm[boundary + 1 :].strip()
    return False


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


def test_scoring_not_wired_without_jd_adjudicated_version_writer(tmp_path_factory):
    entrypoint_names = _entrypoint_names()
    assert entrypoint_names, (
        "no scoring entrypoints derived from job_scorer.__all__ -- the "
        "derivation is broken, not proof there's nothing to guard"
    )

    scan_files = _scan_scope_files()
    # Positive control: a broken repo-root resolution would silently scan
    # zero files and pass vacuously.
    assert scan_files, "guard scanned zero files -- run pytest from the repo root"

    # Defensive tripwire, not a live concern today: several sibling tests in
    # this file write deliberately-wired fixture files
    # (ScanServices(score_and_persist_job=...), etc.) under their own
    # tmp_path. Now that the scan set is git's own view of the repo
    # (_scan_scope_files -> git ls-files, cwd=_REPO_ROOT), a path outside
    # the repo's working tree -- which pytest's tmp_path always is -- cannot
    # appear in it by construction; `git ls-files` has no way to report a
    # file it was never asked about. Kept as a regression guard in case a
    # future change ever swaps the scan source back to a plain filesystem
    # walk (which DOES reach outside paths if _REPO_ROOT nests inside the
    # runner's tmp tree -- see feedback_path_guard_tests_vs_runner_workspace).
    pytest_tmp_root = tmp_path_factory.getbasetemp()
    leaked = [p for p in scan_files if pytest_tmp_root in p.resolve().parents]
    assert not leaked, (
        f"scan swept up pytest's own tmp_path tree ({pytest_tmp_root}) -- "
        f"{leaked[:3]} -- the scan source is no longer isolated from the "
        f"runner's temp dir, so sibling tests' deliberately-wired fixture "
        f"files can flip `wired` for the wrong reason; fix the scan source "
        f"before trusting this assertion's verdict"
    )

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
    missed scripts/, analyses/, and any future top-level package. The
    actual scan set (_scan_scope_files) is git's own view of the repo --
    no separate os.walk-based cross-check exists any more (PR #238
    re-review round 4, LOW finding: the walk necessarily excludes every
    dot-directory, which also silently drops a legitimately tracked file
    like a future .github/scripts/*.py CI helper, turning a superset
    assertion CI-red for a reason unrelated to this guard's own purpose --
    see test_scan_set_excludes_gitignored_and_includes_tracked_dotdir_py
    for the regression coverage that finding needed instead)."""
    oracle = {p.relative_to(_REPO_ROOT).as_posix() for p in _scan_scope_files()}
    assert oracle, "git-oracle scan set resolved empty -- git ls-files is broken"
    assert not any(p.startswith("jobcannon/engine/") for p in oracle)
    assert not any(p.startswith("tests/") for p in oracle)
    assert any(p.startswith("scripts/") for p in oracle), (
        "scripts/ is a real scan-entrypoint location (scripts/run_scan_once.py) "
        "and must be in-scope without naming that file specifically"
    )


def test_scan_set_excludes_gitignored_and_includes_tracked_dotdir_py(tmp_path):
    """The actual scan set (the git oracle) must NOT include a gitignored
    *.py outside a dot-dir -- e.g. a local `uv build`'s dist/*.py artifact
    -- and MUST include a tracked *.py under a dot-directory, e.g. a
    `.github/scripts/*.py` CI helper (PR #238 re-review round 4, LOW
    finding: a former os.walk-based cross-check pruned every dot-dir,
    including legitimately tracked ones, and would have gone CI-red on
    exactly this ordinary addition -- deleted rather than patched; this is
    the regression coverage that used to live implicitly in that
    cross-check's superset assertion, now asserted directly against the
    git-oracle scan set itself). Synthetic repo, never the real one;
    mutates the module-global _REPO_ROOT for the duration of this test
    only."""
    global _REPO_ROOT
    repo = tmp_path / "synthrepo"
    (repo / "jobcannon" / "engine").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "scripts").mkdir()
    (repo / "dist").mkdir()
    (repo / ".gitignore").write_text("dist/\n__pycache__/\n", encoding="utf-8")
    (repo / "scripts" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "jobcannon" / "engine" / "e.py").write_text("y = 1\n", encoding="utf-8")
    (repo / "tests" / "t.py").write_text("z = 1\n", encoding="utf-8")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "i"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    orig_root = _REPO_ROOT
    _REPO_ROOT = repo
    try:
        scan_files = {p.relative_to(repo).as_posix() for p in _scan_scope_files()}
        assert scan_files == {"scripts/a.py"}, f"control failed: {scan_files}"

        # A local `uv build` shape: a gitignored *.py lands outside any dot-dir.
        (repo / "dist" / "generated.py").write_text("w = 1\n", encoding="utf-8")
        scan_files_after_ignore = {p.relative_to(repo).as_posix() for p in _scan_scope_files()}
        assert scan_files_after_ignore == {"scripts/a.py"}, (
            f"gitignored dist/*.py leaked into the actual scan set: "
            f"{scan_files_after_ignore} -- a local `uv build` would now make "
            f"the CI guard scan a build artifact"
        )

        # An ordinary tracked CI helper under a dot-dir -- exactly the kind
        # of file this very PR's branch (which edits .github/workflows/ci.yml)
        # would plausibly add. Left untracked-but-not-gitignored, same as the
        # dist/generated.py case above: the git-oracle union (tracked +
        # untracked-not-ignored) must include it either way.
        (repo / ".github" / "scripts").mkdir(parents=True)
        (repo / ".github" / "scripts" / "foo.py").write_text("v = 1\n", encoding="utf-8")
        scan_files_after_dotdir = {p.relative_to(repo).as_posix() for p in _scan_scope_files()}
        assert scan_files_after_dotdir == {"scripts/a.py", ".github/scripts/foo.py"}, (
            f"a tracked .py under a dot-dir was dropped from the actual scan "
            f"set: {scan_files_after_dotdir} -- a real .github/scripts/*.py CI "
            f"helper would now silently evade the WIRED scan"
        )
    finally:
        _REPO_ROOT = orig_root


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
    """The statement-shape anchor: a SET-shaped phrase inside a statement
    that isn't an UPDATE/INSERT this detector models must not count."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "CREATE TRIGGER t AFTER UPDATE ON postings BEGIN SET jd_adjudicated_version = 1; END"
    )


def test_writer_classifier_anchor_preserves_update_only_and_schema_qualified():
    """The tightened SQL-shape anchor (PR #238 re-review, LOW finding) must
    still accept the two legitimate variants it was designed to keep."""
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE ONLY postings SET jd_adjudicated_version = %(v)s WHERE dedup_key = %(k)s"
    )
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE public.postings SET jd_adjudicated_version = %(v)s WHERE dedup_key = %(k)s"
    )


def test_writer_classifier_rejects_prose_that_reaches_a_plausible_value():
    """Refuter probe (PR #238 re-review, LOW finding): plain-English prose
    starting with "UPDATE"/"INSERT" and ending at a value-shaped token used
    to classify as a writer under the old keyword-only anchor. The
    SQL-shape anchor now rejects it directly -- these are raw strings, not
    embedded in a docstring, so the AST docstring-node skip (a separate,
    independent layer) is not what is being tested here."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE flow: we SET jd_adjudicated_version = 1, then commit"
    )
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "INSERT path: SET jd_adjudicated_version = %(v)s, and log it"
    )


def test_writer_classifier_rejects_migration_description_kwarg_prose(tmp_path):
    """Real-world shape of the prose vector: a non-docstring string
    constant such as a Migration's `description=` kwarg (m0009's own
    docstring already names the column this way). Exercises the full
    scan_for_writer AST path, not just the SQL-string classifier, to prove
    the anchor -- not the docstring skip -- is what closes this."""
    (tmp_path / "fake_migration.py").write_text(
        "class Migration:\n"
        "    def __init__(self, description):\n"
        "        self.description = description\n"
        "\n"
        'M = Migration(description="UPDATE flow: we SET jd_adjudicated_version = 1, '
        'then commit")\n',
        encoding="utf-8",
    )
    offenders = scan_for_writer(tmp_path)
    assert not offenders, f"prose in a non-docstring kwarg constant false-fired: {offenders}"


def test_writer_classifier_rejects_nested_case_in_when_condition():
    """Refuter probe (PR #238 re-review, LOW finding): a CASE nested inside
    an outer CASE's WHEN condition must not be parsed as if the inner
    THEN were the outer's value -- see _segment_sets_non_null's nested-case
    refusal."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = "
        "CASE WHEN (CASE WHEN jd_full IS NULL THEN 1 ELSE 0 END) = 1 "
        "THEN NULL ELSE jd_adjudicated_version END WHERE dedup_key = %(k)s"
    )


def test_writer_classifier_accepts_plain_case_when_not_nested():
    """Control for the nested-CASE refusal above: an ordinary, non-nested
    CASE WHEN ... THEN <value> ELSE <self> END must still classify True."""
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = "
        "CASE WHEN x THEN %(v)s ELSE jd_adjudicated_version END "
        "WHERE dedup_key = %(k)s"
    )


def test_writer_classifier_accepts_coalesce_wrapping_positive_evidence():
    """PR #238 re-review, LOW finding: this repo's own idiom for exactly
    this kind of write is COALESCE(<new value>, <self>) (verbatim shape at
    jobcannon/db/_companies.py:143 and :150) -- the most likely future
    adjudicator statement would otherwise be a spurious CI red."""
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = "
        "COALESCE(%(v)s, jd_adjudicated_version) WHERE dedup_key = %(k)s"
    )
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = COALESCE(%(v)s, 0) WHERE dedup_key = %(k)s"
    )
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = COALESCE(%(v)s) WHERE dedup_key = %(k)s"
    )


def test_writer_classifier_rejects_coalesce_self_reference_first_arg():
    """The COALESCE widening must require the POSITIVE shape as the FIRST
    argument -- a self-reference first arg must stay rejected regardless of
    what follows."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = "
        "COALESCE(jd_adjudicated_version, 0) WHERE dedup_key = %(k)s"
    )


def test_writer_classifier_rejects_coalesce_null_first_arg():
    """A NULL first argument is not positive evidence even though a
    self-reference sits in the second argument."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = "
        "COALESCE(NULL, jd_adjudicated_version) WHERE dedup_key = %(k)s"
    )


def test_writer_classifier_rejects_write_after_a_semicolon_boundary():
    """PR #238 re-review round 2: the exact residual the refuter's own
    diagnostic-only ADVERSARIAL probe surfaced and flagged out of scope
    last round. A genuine UPDATE that only touches jd_full, followed by an
    unrelated SELECT whose operand happens to be `jd_adjudicated_version =
    1`, must NOT classify as a writer -- the SELECT is a different
    statement, past the top-level `;` boundary, never part of the
    anchored UPDATE's SET clause."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_full = %(t)s; SELECT jd_adjudicated_version = 1"
    )


def test_writer_classifier_reanchors_a_write_after_a_semicolon_boundary():
    """Control for the test above: bounding the search at the first `;`
    must not blind the detector to a REAL write that happens to sit in the
    second statement of a batch -- the text after the boundary is
    re-anchored and classified independently, not simply discarded."""
    assert _sql_writes_jd_adjudicated_version_non_null(
        "SELECT 1; UPDATE postings SET jd_adjudicated_version = %(v)s"
    )


def test_writer_classifier_semicolon_inside_quoted_literal_is_not_a_boundary():
    """A `;` inside a single-quoted SQL string literal must not be treated
    as a statement boundary -- otherwise a real write whose OWN statement
    merely contains a semicolon-bearing string value earlier in the SET
    list would be wrongly truncated away and missed (a false negative:
    exactly the direction this guard exists to prevent)."""
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_full = 'note: a;b', jd_adjudicated_version = 1 "
        "WHERE dedup_key = %(k)s"
    )


def test_writer_classifier_semicolon_inside_parens_is_not_a_boundary():
    """A `;` nested inside a function call's argument list (paren-depth
    > 0) must not be treated as a statement boundary either -- same false-
    negative risk as the quoted-literal case above, via the paren-depth
    side of the boundary scan instead of the quote-tracking side."""
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_full = func(1; 2), jd_adjudicated_version = 1 "
        "WHERE dedup_key = %(k)s"
    )


def test_writer_classifier_returning_and_from_close_the_trailing_clause_gap():
    """PR #238 re-review round 4, LOW finding: a real value-producing write
    followed immediately by RETURNING, or an UPDATE ... FROM ... WHERE ...
    clause, with no WHERE ahead of the trailing keyword, used to classify
    False -- _top_level_segment_stop stopped only at a top-level
    comma/semicolon/WHERE, so RETURNING/FROM (and everything after) stayed
    inside the RHS text handed to the whitelist, which then correctly
    failed to recognize it as one of the fixed positive shapes."""
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = %(v)s RETURNING id"
    )
    assert _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = %(v)s FROM other o WHERE o.id = postings.id"
    )


def test_writer_classifier_extract_from_inside_parens_not_widened():
    """Control for the RETURNING/FROM fix above: a FROM that sits inside a
    function call's own argument list (paren-depth > 0) -- EXTRACT(epoch
    FROM ts) is a real Postgres idiom -- must not be mistaken for the new
    top-level FROM stop token. This shape is not in the positive-RHS
    whitelist regardless and stays False -- documented as a known gap, not
    widened into the whitelist."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = EXTRACT(epoch FROM ts) "
        "WHERE dedup_key = %(k)s"
    )


def test_writer_classifier_is_distinct_from_condition_unaffected_by_from_stop():
    """PR #238 re-review round 4, refuter's caution alongside the FROM stop
    token: IS DISTINCT FROM is a live repo idiom (jobcannon/db/_jd_full.py's
    own CASE invalidation). A CASE-shaped segment takes the
    _CASE_PREFIX_RE path in _set_clause_assignments and never reaches
    _top_level_segment_stop at all, so a top-level FROM stop token cannot
    regress it -- this must classify exactly as before: False (the CASE
    only ever produces NULL or the prior value, never a real write)."""
    assert not _sql_writes_jd_adjudicated_version_non_null(
        "UPDATE postings SET jd_adjudicated_version = "
        "CASE WHEN jd_full IS DISTINCT FROM %(text)s "
        "THEN NULL ELSE jd_adjudicated_version END WHERE dedup_key = %(dedup_key)s"
    )


def test_writer_classifier_iterates_not_recurses_on_many_semicolons():
    """PR #238 re-review round 4, INFO finding: the statement split used to
    recurse once per top-level `;` -- a pathological string batching
    thousands of top-level `;`-separated statements would raise
    RecursionError well past Python's default recursion depth, fail-loud
    rather than fail-closed. Converted to an iterative loop with identical
    semantics (each segment re-anchored and classified independently, any
    True wins); 2,000 statements is comfortably past the danger zone that
    would have tripped the old recursive version."""
    batch = "; ".join(f"SELECT {i}" for i in range(2000))
    sql = f"{batch}; UPDATE postings SET jd_adjudicated_version = %(v)s"
    assert _sql_writes_jd_adjudicated_version_non_null(sql)

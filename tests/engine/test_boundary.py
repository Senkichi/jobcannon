"""Boundary guards for jobcannon's engine/host split.

Two enforcement functions live here: test_engine_has_no_private_or_host_imports
asserts jobcannon.engine is host-agnostic (no job_finder/flask/apscheduler/
psycopg imports at module level); test_host_has_no_private_or_scheduler_imports
asserts the surrounding host packages (jobcannon.db/host/web) may import
flask/psycopg but must never import job_finder (private repo) or apscheduler
(retired scheduler). The remaining tests below are phantom-import/-name
scanners that catch jobcannon.engine references the two import-boundary
checks above cannot see (function bodies, TYPE_CHECKING blocks, star-imports).
"""

import ast
import pathlib
import re

ENGINE_FORBIDDEN = re.compile(
    r"^\s*(?:from|import)\s+(job_finder|flask|apscheduler|psycopg)\b", re.M
)
HOST_FORBIDDEN = re.compile(r"^\s*(?:from|import)\s+(job_finder|apscheduler)\b", re.M)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_engine_has_no_private_or_host_imports():
    engine = REPO_ROOT / "jobcannon" / "engine"
    assert engine.is_dir(), "jobcannon.engine package missing"
    offenders = []
    for py in sorted(engine.rglob("*.py")):
        m = ENGINE_FORBIDDEN.search(py.read_text(encoding="utf-8"))
        if m:
            offenders.append(f"{py.relative_to(REPO_ROOT)}: {m.group(0).strip()}")
    assert not offenders, "engine boundary violations:\n" + "\n".join(offenders)


def test_host_has_no_private_or_scheduler_imports():
    """Host packages (db/, host/, web/) may import flask/psycopg, but NEVER
    job_finder (private repo) or apscheduler (retired scheduler)."""
    engine = REPO_ROOT / "jobcannon" / "engine"
    offenders = []
    for py in sorted((REPO_ROOT / "jobcannon").rglob("*.py")):
        if engine in py.parents:
            continue
        m = HOST_FORBIDDEN.search(py.read_text(encoding="utf-8"))
        if m:
            offenders.append(f"{py.relative_to(REPO_ROOT)}: {m.group(0).strip()}")
    assert not offenders, "host boundary violations:\n" + "\n".join(offenders)


def test_every_engine_module_imports():
    """Phantom-module guard: the blanket job_finder.web.->jobcannon.engine.
    rewrite can produce imports of engine modules that don't exist, which the
    regex above cannot see. Importing every module catches them loudly.

    Also a completeness guard: this loop's collector (pkg.rglob("*.py")
    rooted at jobcannon/, not jobcannon/engine/) must walk every .py file
    under jobcannon/engine — counted here by tallying how many of the
    modules it actually imports live under jobcannon/engine, and comparing
    that tally against an independent filesystem walk of jobcannon/engine
    itself. A module the collector silently skips (e.g. a future rglob
    pattern change, a symlink rglob doesn't follow) would otherwise shrink
    coverage without failing anything.
    """
    import importlib

    pkg = REPO_ROOT / "jobcannon"
    engine_dir = pkg / "engine"
    failures = []
    engine_modules_imported = 0
    for py in sorted(pkg.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).with_suffix("")
        mod = ".".join(rel.parts).removesuffix(".__init__")
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 — any import failure is a defect
            failures.append(f"{mod}: {type(exc).__name__}: {exc}")
            continue
        if engine_dir in py.parents:
            engine_modules_imported += 1
    assert not failures, "engine modules failed to import:\n" + "\n".join(failures)

    # Deliberately os.walk, NOT a second rglob: the collector above already
    # walks with rglob, so a shared rglob blindspot (e.g. symlink handling)
    # would shrink both sides in lockstep and hide itself. A different walk
    # primitive keeps the comparison honest.
    import os

    engine_files_on_disk = [
        os.path.join(root, f)
        for root, _dirs, files in os.walk(engine_dir)
        if "__pycache__" not in pathlib.Path(root).parts
        for f in files
        if f.endswith(".py")
    ]
    assert engine_modules_imported == len(engine_files_on_disk), (
        f"collector imported {engine_modules_imported} jobcannon.engine modules but "
        f"{len(engine_files_on_disk)} .py files exist on disk under jobcannon/engine — "
        "the collector is missing (or double-counting) files"
    )


# ---------------------------------------------------------------------------
# Static any-indentation phantom-import/-name scan
# ---------------------------------------------------------------------------
#
# test_every_engine_module_imports above only executes module-LEVEL code: it
# cannot see a phantom jobcannon.engine.* reference inside a function body or
# an `if TYPE_CHECKING:` block, because those statements are never reached by
# a bare `import_module()` call. The scan below parses every file under
# jobcannon/ with `ast` (so it sees import statements at ANY nesting depth —
# function bodies, conditionals, TYPE_CHECKING guards — without ever
# executing them) and resolves every jobcannon.engine-referencing import
# against the filesystem:
#
#   * `import jobcannon.engine.foo.bar [as x]` — the dotted module path must
#     resolve to a real module file or package.
#   * `from jobcannon.engine.foo import a, b as c` — the module path must
#     resolve, AND when it resolves to a PACKAGE, each imported NAME (the
#     original name, not its `as` alias) must resolve to one of: a submodule
#     file, a subpackage, or a top-level symbol actually defined/assigned/
#     imported in the package's __init__.py (parsed with `ast`, never
#     imported/executed). When the module resolves to a plain module FILE
#     (not a package), imported names are NOT individually verified — that
#     would require parsing arbitrary top-level statements in every engine
#     module, which is out of scope here (documented residual limit).
#
# `from jobcannon.engine.foo import *` cannot be resolved statically (its
# exported names depend on executing the module), so rather than silently
# skip it this scanner treats any star-import under jobcannon/ as an
# offender in its own right — i.e. it asserts one never appears.
#
# Everything below is derived from the filesystem/AST at test time; nothing
# is hardcoded.


def _resolve_import_target(dotted: str, root: pathlib.Path) -> tuple[str, pathlib.Path | None]:
    """Resolve a dotted module path against *root*.

    Returns ("module", path) for a plain .py file, ("package", init_path) for
    a package directory, or ("missing", None) if neither exists.
    """
    candidate = root.joinpath(*dotted.split("."))
    module_file = candidate.with_suffix(".py")
    if module_file.is_file():
        return "module", module_file
    init_file = candidate / "__init__.py"
    if init_file.is_file():
        return "package", init_file
    return "missing", None


def _package_top_level_symbols(init_py: pathlib.Path) -> set[str]:
    """Parse a package's __init__.py with `ast` (never imported/executed) and
    return every name it defines, assigns, or imports at module top level."""
    tree = ast.parse(init_py.read_text(encoding="utf-8"), filename=str(init_py))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Tuple | ast.List):
                    names.update(elt.id for elt in target.elts if isinstance(elt, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
    return names


def _is_engine_ref(dotted: str | None) -> bool:
    return dotted is not None and (
        dotted == "jobcannon.engine" or dotted.startswith("jobcannon.engine.")
    )


def _scan_file_for_phantom_imports(py_path: pathlib.Path, root: pathlib.Path) -> list[str]:
    """Return offender strings for phantom jobcannon.engine references in
    *py_path*, resolved against *root*. See module comment above for the
    exact forms this catches and its documented residual limit."""
    tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    rel = py_path.relative_to(root)
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_engine_ref(alias.name):
                    continue
                kind, _ = _resolve_import_target(alias.name, root)
                if kind == "missing":
                    offenders.append(f"{rel}:{node.lineno}: import {alias.name} -> no such module")

        elif isinstance(node, ast.ImportFrom):
            if node.level or not _is_engine_ref(node.module):
                continue  # relative import, or not a jobcannon.engine reference
            kind, target = _resolve_import_target(node.module, root)
            if kind == "missing":
                offenders.append(
                    f"{rel}:{node.lineno}: from {node.module} import ... -> no such module"
                )
                continue
            for alias in node.names:
                if alias.name == "*":
                    offenders.append(
                        f"{rel}:{node.lineno}: from {node.module} import * -> star-imports are "
                        "not statically resolvable; not allowed under jobcannon/"
                    )
                    continue
                if kind == "module":
                    continue  # plain module file: name-level check out of scope, see module comment
                sub_kind, _ = _resolve_import_target(f"{node.module}.{alias.name}", root)
                if sub_kind != "missing":
                    continue  # submodule or subpackage of the target package
                if alias.name in _package_top_level_symbols(target):
                    continue  # defined/assigned/imported at __init__.py top level
                offenders.append(
                    f"{rel}:{node.lineno}: from {node.module} import {alias.name} -> "
                    "no such submodule, subpackage, or __init__.py symbol"
                )
    return offenders


def test_no_phantom_jobcannon_engine_imports_at_any_indentation():
    """Static-scan phantom-module/-name guard covering what importlib can't see.

    See the module comment above _resolve_import_target for the exact forms
    covered and the documented residual limit (module-file targets don't get
    per-name validation).
    """
    pkg = REPO_ROOT / "jobcannon"
    offenders = []
    for py in sorted(pkg.rglob("*.py")):
        offenders.extend(_scan_file_for_phantom_imports(py, REPO_ROOT))
    assert not offenders, "phantom jobcannon.engine imports:\n" + "\n".join(offenders)


def test_phantom_import_scanner_catches_synthetic_name_gap(tmp_path):
    """Negative self-test for the gap this scanner exists to close: a
    `from jobcannon.engine import nonexistent_name` inside a function body,
    where jobcannon.engine resolves via its own (here, empty) __init__.py so
    the OLD module-path-only check would have passed it silently."""
    engine_dir = tmp_path / "jobcannon" / "engine"
    engine_dir.mkdir(parents=True)
    (tmp_path / "jobcannon" / "__init__.py").write_text("", encoding="utf-8")
    (engine_dir / "__init__.py").write_text("", encoding="utf-8")
    (engine_dir / "real_module.py").write_text("REAL = 1\n", encoding="utf-8")

    caller = engine_dir / "caller.py"
    caller.write_text(
        "def f():\n"
        "    from jobcannon.engine import nonexistent_name\n"
        "    return nonexistent_name\n",
        encoding="utf-8",
    )

    offenders = _scan_file_for_phantom_imports(caller, tmp_path)
    assert any("nonexistent_name" in o for o in offenders), offenders


def test_phantom_import_scanner_accepts_real_submodule_and_init_symbol(tmp_path):
    """Companion positive case: a real submodule name, an aliased real
    submodule name, and a name actually defined in __init__.py must all be
    accepted — the scanner must not false-positive on legitimate imports."""
    engine_dir = tmp_path / "jobcannon" / "engine"
    engine_dir.mkdir(parents=True)
    (tmp_path / "jobcannon" / "__init__.py").write_text("", encoding="utf-8")
    (engine_dir / "__init__.py").write_text("EXPORTED = 1\n", encoding="utf-8")
    (engine_dir / "real_module.py").write_text("REAL = 1\n", encoding="utf-8")

    caller = engine_dir / "caller.py"
    caller.write_text(
        "if True:\n    from jobcannon.engine import real_module as rm, EXPORTED\n",
        encoding="utf-8",
    )

    offenders = _scan_file_for_phantom_imports(caller, tmp_path)
    assert offenders == []

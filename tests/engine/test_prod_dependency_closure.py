"""Guard against #406: a module imported unconditionally at import time by
jobcannon/ code, but whose distribution lives only in uv.lock's dev
dependency group (or nowhere), not in `[project] dependencies`.

Render's build/pre-deploy path is `uv sync --locked --no-dev` (render.yaml),
which installs ONLY the main dependency closure. CI, by contrast, always
installs the dev group, so a module-level `import jsonschema` (or anything
like it) passes every test and every CI run while quietly being unimportable
in production -- exactly what broke jobcannon.host.model_provider after
698960c (2026-09-03): `from jsonschema import ValidationError, validate` at
module level with jsonschema declared only under `[dependency-groups] dev`.

This test never runs `uv sync` and never touches the network: it parses
uv.lock (already-resolved, checked-in) with `tomllib`, parses every
jobcannon/**/*.py file with `ast` (never executing any of it), and checks
each top-level import's owning distribution against uv.lock's dependency
graph rooted at jobcannon's own `dependencies` array -- the same data
`uv sync --locked --no-dev` would install from. It answers "would this
import work in a --no-dev install" without ever performing one.

Deliberately excluded, matching what --no-dev also excludes:
  * `[dependency-groups]` (dev group) and any other named group.
  * `[project.optional-dependencies]` extras jobcannon does not itself
    request (Render's buildCommand never passes `--extra`).

Deliberately NOT flagged, because it does not execute at import time either
in this checker or in a real `--no-dev` deploy:
  * Imports inside a function/class body (lazy -- e.g. the guarded
    `from playwright.sync_api import sync_playwright` inside
    jobcannon/engine/ats_scanner/_run_playwright.py, which is intentionally
    NOT in [project.dependencies]).
  * Imports nested inside `try:`, `if TYPE_CHECKING:`, or any other compound
    statement at module level -- a module-level try/except ImportError is a
    deliberate "tolerate this being absent" pattern (see
    jobcannon/engine/providers/gemini_provider.py), the opposite of the bug
    class this guard exists to catch.
Only bare top-level `import x` / `from x import y` statements -- direct
children of the module body -- count, because those are the only imports
that unconditionally execute (and can unconditionally crash the import
chain) the moment the module is loaded.
"""

from __future__ import annotations

import ast
import importlib.metadata
import pathlib
import re
import sys
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / "uv.lock"
JOBCANNON_ROOT = REPO_ROOT / "jobcannon"
ROOT_PACKAGE_NAME = "jobcannon"


def _normalize(name: str) -> str:
    """PEP 503 distribution-name normalization: uv.lock names are already
    lowercase-hyphenated, but `importlib.metadata` distribution names keep
    their original casing/separators (e.g. "PyYAML", "IMAPClient",
    "Flask-WTF") -- both sides must go through this to compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_lock_packages(lock_path: pathlib.Path) -> dict[str, dict]:
    """name -> that package's `[[package]]` table, from the checked-in lock."""
    with lock_path.open("rb") as f:
        data = tomllib.load(f)
    return {pkg["name"]: pkg for pkg in data["package"]}


def _main_dependency_closure(packages: dict[str, dict], root: str = ROOT_PACKAGE_NAME) -> set[str]:
    """BFS over uv.lock dependency edges starting at *root*'s own main
    `dependencies` array ONLY -- never its `dev-dependencies` groups nor
    `optional-dependencies` extras it doesn't itself request. Each visited
    package's own `dependencies` are pulled in transitively (marker-gated or
    not -- a marker narrows WHERE a dep applies, it doesn't remove it from
    "what --no-dev installs"). An `extra` on a dependency edge (e.g.
    `psycopg[binary]`) additionally pulls that extra's
    `optional-dependencies` entries for the depended-upon package.
    """
    root_pkg = packages[root]
    closure: set[str] = set()
    visited_for_deps: set[str] = set()
    stack: list[tuple[str, tuple[str, ...]]] = [
        (dep["name"], tuple(dep.get("extra", []))) for dep in root_pkg.get("dependencies", [])
    ]
    seen_extra_edges: set[tuple[str, str]] = set()

    while stack:
        name, extras = stack.pop()
        closure.add(_normalize(name))
        pkg = packages.get(name)
        if pkg is None:
            continue  # not resolvable further (shouldn't happen in a valid lock)

        if name not in visited_for_deps:
            visited_for_deps.add(name)
            for dep in pkg.get("dependencies", []):
                stack.append((dep["name"], tuple(dep.get("extra", []))))

        opt_deps = pkg.get("optional-dependencies", {})
        for extra in extras:
            edge = (name, extra)
            if edge in seen_extra_edges:
                continue
            seen_extra_edges.add(edge)
            for dep in opt_deps.get(extra, []):
                stack.append((dep["name"], tuple(dep.get("extra", []))))

    return closure


def _collect_top_level_import_roots(py_root: pathlib.Path) -> dict[str, list[str]]:
    """top-level imported module name -> ["relpath:lineno", ...] for every
    bare module-level `import x[.y]` / `from x[.y] import z` statement under
    *py_root* -- direct children of each file's module body only. See the
    module docstring for exactly what this deliberately does not walk into
    (function bodies, try/except, TYPE_CHECKING, any other compound stmt)."""
    occurrences: dict[str, list[str]] = {}
    for py in sorted(py_root.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        rel = py.relative_to(REPO_ROOT)
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    occurrences.setdefault(top, []).append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                if node.level or node.module is None:
                    continue  # relative import -- always intra-jobcannon
                top = node.module.split(".")[0]
                occurrences.setdefault(top, []).append(f"{rel}:{node.lineno}")
    return occurrences


def _find_closure_violations(
    imports: dict[str, list[str]],
    closure: set[str],
    distributions: dict[str, list[str]],
) -> list[str]:
    """Return one formatted offender string per module not covered by
    *closure*, each naming the module and its first use site."""
    offenders = []
    stdlib = sys.stdlib_module_names
    for module, locations in sorted(imports.items()):
        if module in stdlib or module == ROOT_PACKAGE_NAME:
            continue
        candidates = distributions.get(module) or [module]
        normalized_candidates = [_normalize(c) for c in candidates]
        if any(candidate in closure for candidate in normalized_candidates):
            continue
        offenders.append(
            f"{module} (candidate distributions: {normalized_candidates}) "
            f"-> {locations[0]} -- not in uv.lock's main [project.dependencies] "
            "closure; a `uv sync --locked --no-dev` production install cannot "
            "import this module"
        )
    return offenders


def test_no_prod_dependency_closure_violations():
    """Every module-level import under jobcannon/ must resolve inside the
    main-dependencies closure a production (--no-dev) install actually gets.
    See the module docstring for the #406 incident this reproduces and the
    exact scope (module-level only; lazy/guarded imports are out of scope by
    design, not by oversight)."""
    packages = _load_lock_packages(LOCK_PATH)
    closure = _main_dependency_closure(packages)
    imports = _collect_top_level_import_roots(JOBCANNON_ROOT)
    distributions = importlib.metadata.packages_distributions()

    offenders = _find_closure_violations(imports, closure, distributions)
    assert not offenders, "production dependency closure violations:\n" + "\n".join(offenders)


def test_closure_violation_detector_catches_synthetic_dev_only_import(tmp_path):
    """Negative self-test for the mechanism itself: a synthetic uv.lock
    where a package lives ONLY in the dev group (mirroring the real
    jsonschema-before-the-fix shape), plus a synthetic module doing a bare
    top-level `import` of it, must be flagged. Without this, the detector
    could silently stop detecting anything and the guard test above would
    pass for the wrong reason (nothing to import, not nothing wrong)."""
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(
        """
version = 1
revision = 3
requires-python = ">=3.12"

[[package]]
name = "jobcannon"
source = { editable = "." }
dependencies = [
    { name = "requests" },
]

[package.dev-dependencies]
dev = [
    { name = "dev-only-pkg" },
]

[[package]]
name = "requests"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "dev-only-pkg"
source = { registry = "https://pypi.org/simple" }
""",
        encoding="utf-8",
    )
    packages = _load_lock_packages(lock_path)
    closure = _main_dependency_closure(packages)
    assert closure == {"requests"}

    imports = {"dev_only_pkg": ["fake/module.py:1"], "requests": ["fake/module.py:2"]}
    distributions = {"dev_only_pkg": ["dev-only-pkg"], "requests": ["requests"]}
    offenders = _find_closure_violations(imports, closure, distributions)

    assert len(offenders) == 1
    assert "dev_only_pkg" in offenders[0]
    assert "fake/module.py:1" in offenders[0]

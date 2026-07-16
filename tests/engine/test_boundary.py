"""Engine purity guard: jobcannon.* must be host-agnostic."""

import pathlib
import re

FORBIDDEN = re.compile(r"^\s*(?:from|import)\s+(job_finder|flask|apscheduler)\b", re.M)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_engine_has_no_private_or_host_imports():
    pkg = REPO_ROOT / "jobcannon"
    assert pkg.is_dir(), "jobcannon package missing"
    offenders = []
    for py in sorted(pkg.rglob("*.py")):
        m = FORBIDDEN.search(py.read_text(encoding="utf-8"))
        if m:
            offenders.append(f"{py.relative_to(REPO_ROOT)}: {m.group(0).strip()}")
    assert not offenders, "engine boundary violations:\n" + "\n".join(offenders)


def test_every_engine_module_imports():
    """Phantom-module guard: the blanket job_finder.web.->jobcannon.engine.
    rewrite can produce imports of engine modules that don't exist, which the
    regex above cannot see. Importing every module catches them loudly."""
    import importlib

    pkg = REPO_ROOT / "jobcannon"
    failures = []
    for py in sorted(pkg.rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).with_suffix("")
        mod = ".".join(rel.parts).removesuffix(".__init__")
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 — any import failure is a defect
            failures.append(f"{mod}: {type(exc).__name__}: {exc}")
    assert not failures, "engine modules failed to import:\n" + "\n".join(failures)


# jobcannon.engine, optionally followed by more dotted segments, with a word
# boundary right after "engine" so "jobcannon.engineFoo" (a different,
# hypothetical top-level name) can never be mistaken for a match.
_JOBCANNON_ENGINE_IMPORT = re.compile(
    r"^\s*(?:"
    r"from\s+(jobcannon\.engine\b(?:\.\w+)*)\s+import\b"
    r"|"
    r"import\s+(jobcannon\.engine\b(?:\.\w+)*)\b"
    r")",
    re.M,
)


def _resolves_to_real_module(dotted: str, repo_root: pathlib.Path) -> bool:
    """True if *dotted* (e.g. 'jobcannon.engine.foo.bar') exists on disk as
    either a module file ('foo/bar.py') or a package ('foo/bar/__init__.py')."""
    candidate = repo_root.joinpath(*dotted.split("."))
    return candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file()


def test_no_phantom_jobcannon_engine_imports_at_any_indentation():
    """Static-scan phantom-module guard covering what importlib can't see.

    test_every_engine_module_imports above only executes module-level code:
    it cannot detect a phantom jobcannon.engine.* path minted by the blanket
    rewrite inside a function body or an `if TYPE_CHECKING:` block, because
    those statements are never reached by a bare `import_module()` call. This
    scan instead greps every from/import statement referencing the
    jobcannon.engine namespace at ANY indentation and resolves each dotted
    path against the filesystem directly — no hardcoded module list, so it
    stays correct as modules are added or removed.
    """
    pkg = REPO_ROOT / "jobcannon"
    offenders = []
    for py in sorted(pkg.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for match in _JOBCANNON_ENGINE_IMPORT.finditer(text):
            dotted = match.group(1) or match.group(2)
            if not _resolves_to_real_module(dotted, REPO_ROOT):
                rel = py.relative_to(REPO_ROOT)
                offenders.append(f"{rel}: {match.group(0).strip()!r} -> no such module {dotted!r}")
    assert not offenders, "phantom jobcannon.engine imports:\n" + "\n".join(offenders)

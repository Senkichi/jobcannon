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

"""Guard test to ensure no bare requests.get/post calls in ats_platforms package.

This test uses AST analysis to verify that all HTTP calls in the ats_platforms
package go through the shared get_session() function, not bare requests.get/post.
This prevents performance regressions where TCP+TLS handshakes are repeated.
"""

import ast
import re
from pathlib import Path

# Matches mock.patch target strings that still reference the migrated bare-requests
# seam on ats_scanner or ats_platforms (any submodule). Deliberately does NOT match
# ats_prober.requests.* — that module intentionally keeps bare `import requests` and
# is out of scope for this migration.
_STALE_PATCH_TARGET_RE = re.compile(
    r"ats_scanner\.requests\.(?:get|post|head|put|delete|patch)"
    r"|ats_platforms(?:\.[A-Za-z_][A-Za-z0-9_]*)?\.requests\.(?:get|post|head|put|delete|patch)"
)


def _is_mock_patch_call(node: ast.Call) -> bool:
    """Return True if node is a call to patch(...)/mock.patch(...)/unittest.mock.patch(...)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute):
        return func.attr == "patch"
    return False


def _is_patch_object_call(node: ast.Call) -> bool:
    """Return True if node is a call to patch.object(...)/mock.patch.object(...)."""
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "object"


def _build_import_map(tree: ast.AST) -> dict[str, str]:
    """Map local names to fully-qualified module paths for every ``from X import Y``
    (or ``... import Y as Z``) statement anywhere in the file, including imports
    nested inside function bodies (common for lazy imports in this test suite)."""
    local_to_fq: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local_name = alias.asname or alias.name
                local_to_fq[local_name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".")[0]
                local_to_fq[local_name] = alias.name
    return local_to_fq


def test_no_bare_requests_calls_in_ats_platforms():
    """Verify no bare requests.get/post calls exist in ats_platforms package.

    This guard ensures all HTTP calls use the shared Session via get_session()
    to avoid repeated TCP+TLS handshakes. Bare requests.get/post calls would
    defeat the connection pooling optimization.
    """
    ats_platforms_dir = (
        Path(__file__).resolve().parents[2] / "jobcannon" / "engine" / "ats_platforms"
    )

    violations = []

    for py_file in ats_platforms_dir.glob("*.py"):
        # Skip __init__.py as it only imports for test compatibility
        if py_file.name == "__init__.py":
            continue

        with open(py_file, encoding="utf-8") as f:
            source = f.read()

        try:
            tree = ast.parse(source)

            for node in ast.walk(tree):
                # Check for bare requests.get calls
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        # Check if it's requests.get or requests.post
                        if isinstance(node.func.value, ast.Name):
                            if node.func.value.id == "requests" and node.func.attr in (
                                "get",
                                "post",
                                "head",
                                "put",
                                "delete",
                                "patch",
                            ):
                                violations.append(
                                    f"{py_file.name}:{node.lineno}: bare requests.{node.func.attr}() call"
                                )
        except SyntaxError:
            # Skip files that can't be parsed (unlikely in production code)
            pass

    if violations:
        violation_msg = "Found bare requests.get/post calls in ats_platforms:\n" + "\n".join(
            violations
        )
        raise AssertionError(violation_msg)


def test_get_session_imported_in_platform_files():
    """Verify that platform files import get_session from _http_session."""
    ats_platforms_dir = (
        Path(__file__).resolve().parents[2] / "jobcannon" / "engine" / "ats_platforms"
    )

    # Files that should have get_session imported (platform scanner files)
    platform_files = [
        "_registry.py",
        "_detail_fetchers.py",
        "_platforms_workday.py",
        "_platforms_smartrecruiters.py",
        "_platforms_oracle_cloud.py",
        "_platforms_ultipro.py",
        "_platforms_successfactors.py",
        "_platforms_personio.py",
        "_platforms_bamboohr.py",
        "_platforms_phenom.py",
        "_platforms_microsoft.py",
        "_platforms_ibm.py",
        "_platforms_eightfold.py",
        "_platforms_amazon.py",
        "_platforms_adp.py",
    ]

    for filename in platform_files:
        filepath = ats_platforms_dir / filename
        if not filepath.exists():
            continue

        with open(filepath, encoding="utf-8") as f:
            source = f.read()

        # Check if get_session is imported from _http_session
        if "from jobcannon.engine.ats_platforms._http_session import get_session" not in source:
            # Some files might not use HTTP (e.g., pure data transformation)
            # Only check files that we know make HTTP calls
            if "get_session()" in source:
                raise AssertionError(
                    f"{filename} uses get_session() but doesn't import it from _http_session"
                )


def test_no_stale_ats_scanner_or_ats_platforms_requests_patches_in_tests():
    """Regression guard: no test anywhere under tests/ may mock.patch the dead
    bare-requests seam on ats_scanner or ats_platforms.

    Both packages were migrated to route HTTP calls through
    a shared pooled requests.Session via get_session(), so `requests.get`/`.post`/etc.
    are no longer called directly on those modules. A test that still does
    `patch("...ats_scanner.requests.get")`,
    `patch("...ats_platforms.<submodule>.requests.post")`, or the equivalent
    `patch.object(<ats_platforms_or_ats_scanner_module>, "requests")` form is patching
    a seam that no longer exists in the production code path -- it will raise
    AttributeError (the attribute doesn't exist post-migration) or, if the module
    still coincidentally carries a `requests` name, silently no-op rather than
    intercept the real HTTP call. This is a zero-tolerance guard: after the full
    migration there should be no such patches left anywhere in tests/, so no
    allowlist is provided here. ats_prober.requests.* patches are unrelated (that
    module intentionally keeps bare `import requests`) and are not matched by the
    checks below.
    """
    tests_dir = Path(__file__).resolve().parent

    violations = []

    for py_file in sorted(tests_dir.rglob("*.py")):
        with open(py_file, encoding="utf-8") as f:
            source = f.read()

        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        import_map = _build_import_map(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            if _is_mock_patch_call(node):
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if _STALE_PATCH_TARGET_RE.search(arg.value):
                            rel_path = py_file.relative_to(tests_dir)
                            violations.append(
                                f"{rel_path}:{node.lineno}: stale patch target '{arg.value}'"
                            )

            elif _is_patch_object_call(node) and node.args:
                # patch.object(<module_or_obj>, "requests", ...) / mock.patch.object(...)
                attr_arg = node.args[1] if len(node.args) > 1 else None
                if attr_arg is None:
                    for kw in node.keywords:
                        if kw.arg == "attribute":
                            attr_arg = kw.value
                            break
                if (
                    isinstance(attr_arg, ast.Constant)
                    and attr_arg.value == "requests"
                    and isinstance(node.args[0], ast.Name)
                ):
                    target_name = node.args[0].id
                    fq_path = import_map.get(target_name, "")
                    if fq_path.startswith("jobcannon.engine.ats_platforms") or fq_path.startswith(
                        "jobcannon.engine.ats_scanner"
                    ):
                        rel_path = py_file.relative_to(tests_dir)
                        violations.append(
                            f"{rel_path}:{node.lineno}: stale patch.object target "
                            f"'{target_name}' (resolves to '{fq_path}') attribute 'requests'"
                        )

    assert violations == [], (
        "Found mock.patch targets still referencing the migrated bare-requests seam "
        "(ats_scanner.requests.* / ats_platforms(.submodule)?.requests.* / "
        'patch.object(<ats_platforms_or_ats_scanner_module>, "requests")):\n'
        + "\n".join(violations)
    )

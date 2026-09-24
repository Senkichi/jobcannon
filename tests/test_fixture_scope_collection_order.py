"""Regression guard for issue #374: order-dependent conftest fixture loss.

The defect is in pytest itself, not in this repo's code, so this test runs a
real ``pytest`` subprocess against a throwaway tree in ``tmp_path`` — the
failure only manifests through collection-argument order, which cannot be
reproduced by calling fixtures or collectors in-process.

On pytest 9.1.x, this invocation shape:

    pytest pkg/test_first.py test_sibling.py pkg/test_second.py

(library file, sibling file, library file — mirroring
``tests/host/…`` + ``tests/…`` + ``tests/host/…``) made ``test_second.py``
error at fixture setup with ``fixture 'shared_fix' not found``: pytest 9.1
(pytest-dev/pytest#14098) defers conftest fixture parsing until the
directory's ``Directory`` collector node is collected and binds the
FixtureDefs to that node *instance*; re-collecting the shared parent for the
sibling file arg produces a fresh ``Directory`` child instance that shadows
the bound one in ``Session._collection_cache`` (pytest-dev/pytest#14635,
fixed by pytest-dev/pytest#14645 for pytest 9.2).

This repo pins ``pytest!=9.1.*`` (pyproject.toml dev group) until 9.2 ships;
this test is the sentinel proving both that the pin is needed and, later,
that a fixed pytest release really fixes it — it fails on 9.1.x and passes
on 9.0.x and (expected) 9.2+.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_TREE = {
    "pkg/__init__.py": "",
    "pkg/conftest.py": textwrap.dedent(
        """\
        import pytest


        @pytest.fixture
        def shared_fix():
            return 1
        """
    ),
    "pkg/test_first.py": "def test_first(shared_fix):\n    assert shared_fix == 1\n",
    "pkg/test_second.py": "def test_second(shared_fix):\n    assert shared_fix == 1\n",
    "test_sibling.py": "def test_sibling():\n    pass\n",
}


@pytest.mark.parametrize(
    "argv",
    [
        # The trigger shape: a file arg from the shared parent directory
        # collected between two file args from the conftest-bearing subdir.
        ["pkg/test_first.py", "test_sibling.py", "pkg/test_second.py"],
        # Control: same files, sibling last — passed even on pytest 9.1.x.
        ["pkg/test_first.py", "pkg/test_second.py", "test_sibling.py"],
    ],
    ids=["sibling-between-subdir-files", "sibling-last"],
)
def test_subdir_conftest_fixtures_survive_arg_order(tmp_path: Path, argv: list[str]):
    for relpath, content in _TREE.items():
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--rootdir", str(tmp_path), "-q", *argv],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, (
        f"inner pytest run failed (order-dependent conftest fixture loss?)\n"
        f"argv: {argv}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "3 passed" in result.stdout

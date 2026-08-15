"""Tests for scripts/leak_guard.py's attribution carve-out (#25).

leak_guard.py shells out to `git ls-files` itself, so it can only be
exercised inside a real git work tree. Each test builds a disposable repo
under tmp_path and runs the guard as a subprocess against it, the same way
an operator invokes it pre-push.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

LEAK_GUARD = Path(__file__).resolve().parent.parent / "scripts" / "leak_guard.py"


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _run_guard(repo: Path, terms_file: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "JOBCANNON_LEAK_TERMS_FILE": str(terms_file)}
    return subprocess.run(
        [sys.executable, str(LEAK_GUARD)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_planted_term_in_non_carved_file_still_trips_guard(tmp_path):
    """Positive control: an ordinary (non-carved-out) file containing a
    planted term must still exit 1. Proves the scan itself is live, so the
    CLA.md carve-out below isn't just masking a guard that never fires."""
    repo = _init_repo(tmp_path)
    (repo / "notes.md").write_text("plantedleakterm shows up here", encoding="utf-8")
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("plantedleakterm\n", encoding="utf-8")

    result = _run_guard(repo, terms_file)

    assert result.returncode == 1
    assert "notes.md" in result.stdout
    assert "plantedleakterm" in result.stdout


def test_cla_md_required_attribution_is_carved_out(tmp_path):
    """CLA.md's required Maintainer-attribution sentence must not trip the
    guard (#25) — same carve-out already granted to LICENSE."""
    repo = _init_repo(tmp_path)
    (repo / "CLA.md").write_text('maintained by Senkichi ("the Maintainer")\n', encoding="utf-8")
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("senkichi\n", encoding="utf-8")

    result = _run_guard(repo, terms_file)

    assert result.returncode == 0
    assert "clean" in result.stdout


def test_carveout_is_exact_basename_not_substring(tmp_path):
    """A file that merely contains "CLA.md" in its path but isn't named
    exactly that must still be scanned — the carve-out is an exact basename
    match, not a path-substring match (mirrors the existing LICENSE
    contract)."""
    repo = _init_repo(tmp_path)
    (repo / "notCLA.md").write_text("plantedleakterm here too", encoding="utf-8")
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("plantedleakterm\n", encoding="utf-8")

    result = _run_guard(repo, terms_file)

    assert result.returncode == 1
    assert "notCLA.md" in result.stdout

"""Tests for scripts/new_migration.py (issue #211).

Loaded by path (matches tests/test_ported_paths_manifest.py's pattern for
scripts/, which has no __init__.py). Subprocess calls to `gh`/`git` are
faked via monkeypatch -- no real network or git-remote access, per the
brief's "unit tests with injected inputs (no network)" bar.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "new_migration.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("new_migration", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nm = _load_module()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_slugify_lowercases_and_joins_on_non_alnum():
    assert nm._slugify("Add Widget Column!!") == "add_widget_column"


def test_slugify_strips_leading_trailing_separators():
    assert nm._slugify("  --add-thing--  ") == "add_thing"


def test_slugify_empty_input_yields_empty_slug():
    assert nm._slugify("   ---   ") == ""


def test_versions_in_filenames_parses_leading_digits():
    names = ["m0001_initial_schema.py", "m0012_workplace_type.py", "types.py", "__init__.py"]
    assert nm._versions_in_filenames(names) == {1, 12}


def test_versions_in_filenames_ignores_non_matching_names():
    assert nm._versions_in_filenames(["README.md", "conftest.py"]) == set()


def test_mint_version_is_max_plus_one():
    assert nm._mint_version({1, 2, 5}) == 6


def test_mint_version_empty_known_set_starts_at_one():
    assert nm._mint_version(set()) == 1


# ---------------------------------------------------------------------------
# _local_versions -- reads a real (tmp_path) migrations dir
# ---------------------------------------------------------------------------


def test_local_versions_scans_directory(tmp_path):
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "m0001_initial_schema.py").write_text("", encoding="utf-8")
    (mig_dir / "m0003_thing.py").write_text("", encoding="utf-8")
    (mig_dir / "types.py").write_text("", encoding="utf-8")

    assert nm._local_versions(mig_dir) == {1, 3}


# ---------------------------------------------------------------------------
# _open_pr_versions -- gh/git are faked; asserts the "unverified" contract
# ---------------------------------------------------------------------------


def test_open_pr_versions_no_gh_binary_is_unverified(monkeypatch, tmp_path):
    monkeypatch.setattr(nm.shutil, "which", lambda name: None)
    versions, verified = nm._open_pr_versions(tmp_path, "Senkichi/jobcannon")
    assert versions == set()
    assert verified is False


def test_open_pr_versions_gh_call_fails_is_unverified(monkeypatch, tmp_path):
    monkeypatch.setattr(nm.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(nm.subprocess, "run", fake_run)
    versions, verified = nm._open_pr_versions(tmp_path, "Senkichi/jobcannon")
    assert versions == set()
    assert verified is False


def test_open_pr_versions_empty_pr_list_is_verified(monkeypatch, tmp_path):
    monkeypatch.setattr(nm.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["gh", "pr"]
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(nm.subprocess, "run", fake_run)
    versions, verified = nm._open_pr_versions(tmp_path, "Senkichi/jobcannon")
    assert versions == set()
    assert verified is True


def test_open_pr_versions_unions_across_prs(monkeypatch, tmp_path):
    """Positive case: two open PRs, one carrying m0013, the other m0014 --
    both must show up in the returned set, verified True."""
    monkeypatch.setattr(nm.shutil, "which", lambda name: "/usr/bin/gh")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"number": 41}, {"number": 42}]), stderr=""
            )
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "ls-tree"]:
            # Distinguish which PR by call order: first fetch->ls-tree pair
            # is PR 41, second is PR 42.
            fetch_calls = [c for c in calls if c[:2] == ["git", "fetch"]]
            if len(fetch_calls) == 1:
                return subprocess.CompletedProcess(cmd, 0, stdout="m0013_add_thing.py\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="m0014_add_other.py\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(nm.subprocess, "run", fake_run)
    versions, verified = nm._open_pr_versions(tmp_path, "Senkichi/jobcannon")
    assert versions == {13, 14}
    assert verified is True


def test_open_pr_versions_one_unreachable_ref_is_unverified(monkeypatch, tmp_path):
    """Negative case: if any single PR's ref can't be fetched, the whole
    result must be flagged unverified -- never a silent partial view."""
    monkeypatch.setattr(nm.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"number": 99}]), stderr=""
            )
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="fatal: couldn't find remote ref"
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(nm.subprocess, "run", fake_run)
    versions, verified = nm._open_pr_versions(tmp_path, "Senkichi/jobcannon")
    assert verified is False


# ---------------------------------------------------------------------------
# main() -- end-to-end against a tmp_path migrations dir
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    mig_dir = tmp_path / "jobcannon" / "db" / "migrations"
    mig_dir.mkdir(parents=True)
    (mig_dir / "m0001_initial_schema.py").write_text("", encoding="utf-8")
    (mig_dir / "m0012_workplace_type.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(nm, "_MIGRATIONS_DIR", mig_dir)
    monkeypatch.setattr(nm, "_REPO_ROOT", tmp_path)
    return mig_dir


def test_main_mints_next_version_and_writes_file(fake_repo, monkeypatch, capsys):
    monkeypatch.setattr(nm.shutil, "which", lambda name: None)  # no gh -> local-only

    rc = nm.main(["add widget column"])

    assert rc == 0
    dest = fake_repo / "m0013_add_widget_column.py"
    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    assert "version=13" in content
    assert "from jobcannon.db.migrations.types import Migration" in content
    out = capsys.readouterr().out
    assert "version = 13" in out


def test_main_prints_loud_warning_when_unverified(fake_repo, monkeypatch, capsys):
    monkeypatch.setattr(nm.shutil, "which", lambda name: None)

    nm.main(["some change"])

    out = capsys.readouterr().out
    assert "WARNING: unverified against open PRs" in out


def test_main_no_warning_when_verified_with_no_open_prs(fake_repo, monkeypatch, capsys):
    monkeypatch.setattr(nm.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["gh", "pr"]
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(nm.subprocess, "run", fake_run)

    nm.main(["some change"])

    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "verified free on origin/main and 0 open PR(s)" in out


def test_main_accounts_for_open_pr_version_beyond_local_max(fake_repo, monkeypatch, capsys):
    """The exact #211 scenario: local disk tops out at m0012, but an open PR
    already carries m0013 -- the minted version must skip past it to 14."""
    monkeypatch.setattr(nm.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["gh", "pr"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"number": 7}]), stderr=""
            )
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "ls-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="m0013_other_branch.py\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(nm.subprocess, "run", fake_run)

    rc = nm.main(["yet another change"])

    assert rc == 0
    dest = fake_repo / "m0014_yet_another_change.py"
    assert dest.exists()


def test_main_rejects_slug_that_collapses_to_empty(fake_repo, monkeypatch):
    monkeypatch.setattr(nm.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        nm.main(["---"])


def test_main_refuses_to_overwrite_existing_file(fake_repo, monkeypatch):
    """A same-version, same-slug file already existing at the destination
    path must never be silently clobbered. In normal operation the version
    scan itself prevents this (an existing m0013_dup.py would already count
    toward `known`, pushing the mint to 14) -- this test forces the race by
    pinning _mint_version so the dest-exists guard's own behavior is
    isolated and verified directly, independent of that scan."""
    monkeypatch.setattr(nm.shutil, "which", lambda name: None)
    monkeypatch.setattr(nm, "_mint_version", lambda known: 13)
    (fake_repo / "m0013_dup.py").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        nm.main(["dup"])

#!/usr/bin/env python
"""Generate a new schema-migration module with a minted, collision-free version.

Usage:
    python scripts/new_migration.py "add_widget_column"
    python scripts/new_migration.py "add widget column"   # spaces -> snake_case

Why this exists (issue #211)
-----------------------------
Two open PRs (#189, #208) each independently minted `m0011` by taking
origin/main's max version-on-disk + 1 -- correct in isolation, since each
branch could only see itself. `jobcannon/db/migrations/__init__.py` fails
closed on a duplicate version at import time, so the collision was only
discovered when the second PR merged, turning the whole suite (and the
pre-deploy migrate step, docs/deploy-runbook.md Sec 3) red until someone
renumbered.

This repo's migrations are small sequential integers (m0001..m0012 today,
not the private repo's epoch-second stamp -- an epoch stamp would break
that convention outright and there's no SQLite user_version cache here to
motivate it). So the fix isn't a different minting scheme, it's a wider
view of "known versions": the union of origin/main's migrations directory
AND every OPEN PR's head, not just whatever this branch forked from. That's
what actually closes the #211 gap -- a version free on disk locally was
never the problem; a version invisible to a sibling PR was.

`gh` (GitHub CLI, must already be authenticated) supplies the open-PR list;
each PR's `refs/pull/<n>/head` is fetched and its migrations directory
listed via `git ls-tree` (works even for a since-deleted source branch,
unlike resolving through `origin/<branch>`). If `gh` is missing, not
authenticated, or any PR's ref can't be fetched, this mints from
origin/main's on-disk state alone and prints an UNMISSABLE warning -- the
version is then only as safe as the CI collision guard
(scripts/check_migration_collisions.py), which still catches a same-cycle
race at merge time regardless of what this script saw.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _REPO_ROOT / "jobcannon" / "db" / "migrations"
_MIGRATIONS_SUBPATH = "jobcannon/db/migrations/"
_FILENAME_RE = re.compile(r"^m(\d+)_")
_GITHUB_REPO = "Senkichi/jobcannon"

_TEMPLATE = '''"""Migration {version} -- {human}."""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version={version},
    description="{human}",
    sql=[
        # Idempotent DDL only: CREATE TABLE IF NOT EXISTS, CREATE INDEX IF
        # NOT EXISTS, guarded ALTER. For filesystem/env state, pass a
        # py=<callable> instead (see jobcannon/db/migrations/types.py).
    ],
)
'''


def _slugify(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


def _versions_in_filenames(names: list[str]) -> set[int]:
    versions: set[int] = set()
    for name in names:
        mo = _FILENAME_RE.match(name)
        if mo:
            versions.add(int(mo.group(1)))
    return versions


def _local_versions(migrations_dir: Path) -> set[int]:
    return _versions_in_filenames([p.name for p in migrations_dir.glob("m*.py")])


def _fetch_pr_head_versions(repo_root: Path, number: int) -> set[int] | None:
    """Versions in `number`'s head migrations directory, or None on any
    failure (unreachable PR ref, git error, timeout)."""
    fetch = subprocess.run(
        ["git", "fetch", "-q", "origin", f"refs/pull/{number}/head"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if fetch.returncode != 0:
        return None
    ls = subprocess.run(
        ["git", "ls-tree", "--name-only", "FETCH_HEAD", "--", _MIGRATIONS_SUBPATH],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if ls.returncode != 0:
        return None
    names = [Path(line).name for line in ls.stdout.splitlines() if line.strip()]
    return _versions_in_filenames(names)


def _open_pr_versions(repo_root: Path, repo: str) -> tuple[set[int], bool]:
    """Returns (versions, verified). verified is False whenever the result
    is NOT provably the full open-PR set -- gh missing/unauthenticated/
    erroring, or any individual PR ref failing to fetch -- so a caller never
    silently mints against a partial view without being told."""
    if shutil.which("gh") is None:
        return set(), False
    try:
        listing = subprocess.run(
            ["gh", "pr", "list", "-R", repo, "--state", "open", "--json", "number"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        prs = json.loads(listing.stdout)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
    ):
        return set(), False

    versions: set[int] = set()
    for pr in prs:
        found = _fetch_pr_head_versions(repo_root, pr["number"])
        if found is None:
            return versions, False
        versions |= found
    return versions, True


def _mint_version(known: set[int]) -> int:
    return max(known, default=0) + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a new schema-migration module with a minted, collision-free version."
    )
    parser.add_argument("slug", help="short description, e.g. 'add_widget_column'")
    args = parser.parse_args(argv)

    slug = _slugify(args.slug)
    if not slug:
        parser.error("slug must contain at least one alphanumeric character")

    local = _local_versions(_MIGRATIONS_DIR)
    pr_versions, verified = _open_pr_versions(_REPO_ROOT, _GITHUB_REPO)
    version = _mint_version(local | pr_versions)
    filename = f"m{version:04d}_{slug}.py"
    dest = _MIGRATIONS_DIR / filename
    if dest.exists():
        parser.error(f"{dest} already exists")

    human = slug.replace("_", " ")
    dest.write_text(_TEMPLATE.format(version=version, human=human), encoding="utf-8")

    rel = dest.relative_to(_REPO_ROOT) if dest.is_relative_to(_REPO_ROOT) else dest
    print(f"Created {rel}")
    print(f"  version = {version}")
    if verified:
        print(f"  verified free on origin/main and {len(pr_versions)} open PR(s)")
    else:
        print("  " + "*" * 72)
        print("  WARNING: unverified against open PRs -- `gh` was unavailable, not")
        print("  authenticated, or a PR ref could not be fetched. This version is")
        print("  only checked against the migrations on disk in THIS branch and")
        print("  may still collide with another open PR. scripts/")
        print("  check_migration_collisions.py is the CI backstop that catches a")
        print("  same-cycle race at merge time -- but fix `gh` and re-run this")
        print("  script before pushing if you can.")
        print("  " + "*" * 72)
    print("Next: fill in sql=[...] (idempotent DDL) and add tests/host/ coverage for it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

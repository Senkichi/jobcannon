#!/usr/bin/env python
"""CI collision guard for schema-migration versions (issue #211, part 2).

`scripts/new_migration.py` mints a version free against origin/main and
every open PR it can see when the migration is CREATED -- but that view can
go stale the moment another PR merges (or a third PR opens) before this one
does. This script narrows that window: run on every pull_request event, it
re-checks, at merge-review time, that the migration version(s) this PR adds
are still free against origin/main's CURRENT head and every OTHER
currently-open PR, and fails naming the exact colliding PR number(s) if not.
It does not fully close the race on its own (two PRs can each pass this
check within the same window and still both merge back to back) --
`jobcannon/db/migrations/__init__.py`'s duplicate-version import check is
the final backstop.

Runs on the self-hosted `jcpub` Windows runners only (.github/workflows/
ci.yml), gated to same-repo PRs the same way the `test` job is. Talks to
the GitHub REST API directly via `urllib` + `GITHUB_TOKEN` -- deliberately
NOT the `gh` CLI, since nothing on this runner's PATH is assumed beyond
what `actions/checkout` + `astral-sh/setup-uv` provide.

Decision logic (parse_versions / added_versions / find_collisions /
format_collision_message) is pure and unit-tested with injected inputs, no
network. The I/O layer (_api_get / fetch_dir_versions / fetch_open_prs /
fetch_merge_base) is a thin, separately-swappable urllib wrapper so main()
can be exercised with a fake transport in tests too.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

_API_ROOT = "https://api.github.com"
_MIGRATIONS_PATH = "jobcannon/db/migrations"
_FILENAME_RE = re.compile(r"^m(\d+)_")

# (method, url) -> parsed JSON body. Swappable in tests; defaults to a real
# urllib call.
Transport = Callable[[str, str, str], object]


# ---------------------------------------------------------------------------
# Pure decision logic -- unit-tested directly, no network involved.
# ---------------------------------------------------------------------------


def parse_versions(filenames: list[str]) -> dict[int, str]:
    """Map version number -> filename for every `m<digits>_*.py` name. If two
    filenames in the SAME list share a version number, this silently keeps
    only the last one -- callers that need to detect that condition (a ref
    that will fail `jobcannon/db/migrations/__init__.py`'s own duplicate
    check at import time) must call `duplicate_versions` on the same
    filenames FIRST, before this collapses the information away."""
    out: dict[int, str] = {}
    for name in filenames:
        mo = _FILENAME_RE.match(name)
        if mo:
            out[int(mo.group(1))] = name
    return out


def duplicate_versions(filenames: list[str]) -> dict[int, list[str]]:
    """Version -> sorted filenames, for every version number carried by MORE
    THAN ONE file within a single ref's directory listing (e.g. a PR branch
    that rebased over a sibling's since-merged migration of the same
    number, keeping both files with no git conflict). A ref in this state
    always fails to import (`migrations/__init__.py` raises on a duplicate
    version) regardless of what `added_versions`/`find_collisions` conclude
    from the collapsed `parse_versions` view -- so this must be checked
    against the raw filenames before that collapse happens."""
    by_version: dict[int, list[str]] = {}
    for name in filenames:
        mo = _FILENAME_RE.match(name)
        if mo:
            by_version.setdefault(int(mo.group(1)), []).append(name)
    return {version: sorted(names) for version, names in by_version.items() if len(names) > 1}


def added_versions(head: dict[int, str], merge_base: dict[int, str]) -> dict[int, str]:
    """Versions present at the PR's head but not at its merge-base with
    origin/main -- i.e. what THIS PR itself is contributing, independent of
    whatever origin/main has done since the branch point."""
    return {version: name for version, name in head.items() if version not in merge_base}


def find_collisions(
    added: dict[int, str], others: dict[str, dict[int, str]]
) -> dict[int, list[str]]:
    """For each version this PR adds, which other sources (labelled
    "origin/main" or "PR #N") also carry that version number. Only versions
    with at least one hit are included."""
    collisions: dict[int, list[str]] = {}
    for version in added:
        hits = sorted(label for label, versions in others.items() if version in versions)
        if hits:
            collisions[version] = hits
    return collisions


def format_collision_message(collisions: dict[int, list[str]], pr_number: int) -> str:
    lines = [f"check_migration_collisions: PR #{pr_number} has migration version collisions:"]
    for version in sorted(collisions):
        sources = ", ".join(collisions[version])
        lines.append(f"  version {version} is also carried by: {sources}")
    lines.append("Renumber this PR's migration(s) with scripts/new_migration.py and re-push.")
    return "\n".join(lines)


def format_duplicate_message(duplicates: dict[int, list[str]], pr_number: int) -> str:
    lines = [
        f"check_migration_collisions: PR #{pr_number}'s own head has duplicate "
        "migration version(s):"
    ]
    for version in sorted(duplicates):
        files = ", ".join(duplicates[version])
        lines.append(f"  version {version} is carried by multiple files: {files}")
    lines.append(
        "This will fail to import at jobcannon/db/migrations/__init__.py -- "
        "renumber with scripts/new_migration.py and re-push."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O layer -- urllib + GITHUB_TOKEN, no `gh` dependency.
# ---------------------------------------------------------------------------


def _default_transport(method: str, url: str, token: str) -> object:
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jobcannon-migration-collision-guard",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- fixed https host
        return json.loads(response.read().decode("utf-8"))


def fetch_dir_entries(repo: str, ref: str, token: str, transport: Transport) -> list[str]:
    """Raw filenames in `ref`'s migrations directory (not yet collapsed to
    a version->filename dict) -- the shape `duplicate_versions` needs."""
    url = f"{_API_ROOT}/repos/{repo}/contents/{_MIGRATIONS_PATH}?ref={ref}"
    entries = transport("GET", url, token)
    if not isinstance(entries, list):
        raise ValueError(
            f"fetch_dir_entries: expected a list of directory entries for "
            f"ref={ref!r}, got {type(entries).__name__}"
        )
    return [
        entry["name"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("type") == "file"
    ]


def fetch_dir_versions(repo: str, ref: str, token: str, transport: Transport) -> dict[int, str]:
    return parse_versions(fetch_dir_entries(repo, ref, token, transport))


def fetch_merge_base(
    repo: str, base_ref: str, head_sha: str, token: str, transport: Transport
) -> str:
    url = f"{_API_ROOT}/repos/{repo}/compare/{base_ref}...{head_sha}"
    data = transport("GET", url, token)
    if not isinstance(data, dict):
        raise ValueError(
            f"fetch_merge_base: expected a dict compare response, got {type(data).__name__}"
        )
    return data["merge_base_commit"]["sha"]


_MAX_OPEN_PR_PAGES = 50  # 5000 open PRs at per_page=100 -- generous headroom.


def fetch_open_prs(
    repo: str, token: str, transport: Transport, exclude_number: int
) -> list[dict[str, Any]]:
    prs: list[dict[str, Any]] = []
    page = 1
    while True:
        if page > _MAX_OPEN_PR_PAGES:
            # Fail closed rather than loop forever: a transport that never
            # returns a short final page (real GitHub always does once the
            # open-PR list is exhausted) means something is wrong with the
            # response, not that this repo genuinely has 5000+ open PRs.
            raise ValueError(
                f"fetch_open_prs: exceeded {_MAX_OPEN_PR_PAGES} pages "
                "without reaching a short final page -- aborting"
            )
        url = f"{_API_ROOT}/repos/{repo}/pulls?state=open&per_page=100&page={page}"
        batch = transport("GET", url, token)
        if not isinstance(batch, list):
            raise ValueError(
                f"fetch_open_prs: expected a list of pull requests, got {type(batch).__name__}"
            )
        prs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return [pr for pr in prs if pr["number"] != exclude_number]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def run(
    *,
    event_name: str,
    repo: str,
    token: str,
    pr_number: int,
    pr_head_sha: str,
    base_ref: str,
    transport: Transport,
    out: Callable[[str], None] = print,
) -> int:
    if event_name != "pull_request":
        out(
            f"check_migration_collisions: event={event_name!r}, not a pull_request event -- skipping"
        )
        return 0

    try:
        merge_base_sha = fetch_merge_base(repo, base_ref, pr_head_sha, token, transport)

        # Check the PR's own head for an intra-ref duplicate version BEFORE
        # parse_versions collapses it to last-wins -- e.g. this PR adds
        # m0013_a.py, a sibling merges m0013_b.py to main, this branch
        # rebases (both files present, no git conflict): head_versions would
        # otherwise silently become {13: "m0013_a.py"} or {13: "m0013_b.py"}
        # depending on iteration order, `added` would come out empty against
        # a merge-base that also has neither/one of them, and this guard
        # would print "adds no new versions -- skipping" on a head that
        # fails to import at jobcannon/db/migrations/__init__.py.
        head_entries = fetch_dir_entries(repo, pr_head_sha, token, transport)
        duplicates = duplicate_versions(head_entries)
        if duplicates:
            out(format_duplicate_message(duplicates, pr_number))
            return 1
        head_versions = parse_versions(head_entries)

        base_versions = fetch_dir_versions(repo, merge_base_sha, token, transport)
        added = added_versions(head_versions, base_versions)

        if not added:
            out("check_migration_collisions: PR adds no new migration versions -- skipping")
            return 0

        others = {f"origin/{base_ref}": fetch_dir_versions(repo, base_ref, token, transport)}
        for pr in fetch_open_prs(repo, token, transport, exclude_number=pr_number):
            others[f"PR #{pr['number']}"] = fetch_dir_versions(
                repo, pr["head"]["sha"], token, transport
            )
    except (urllib.error.URLError, KeyError, ValueError, TypeError) as exc:
        out(f"check_migration_collisions: GitHub API error while checking -- {exc!r}")
        return 1

    collisions = find_collisions(added, others)
    if collisions:
        out(format_collision_message(collisions, pr_number))
        return 1

    out(
        f"check_migration_collisions: clean -- PR #{pr_number} adds version(s) "
        f"{sorted(added)} with no collision against origin/main or "
        f"{len(others) - 1} other open PR(s)"
    )
    return 0


def main() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name != "pull_request":
        print(
            f"check_migration_collisions: event={event_name!r}, not a pull_request event -- skipping"
        )
        return 0

    try:
        repo = os.environ["GITHUB_REPOSITORY"]
        token = os.environ["GITHUB_TOKEN"]
        pr_number = int(os.environ["PR_NUMBER"])
        pr_head_sha = os.environ["PR_HEAD_SHA"]
    except (KeyError, ValueError) as exc:
        print(
            f"check_migration_collisions: missing/invalid required env var -- {exc}",
            file=sys.stderr,
        )
        return 1
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")

    return run(
        event_name=event_name,
        repo=repo,
        token=token,
        pr_number=pr_number,
        pr_head_sha=pr_head_sha,
        base_ref=base_ref,
        transport=_default_transport,
    )


if __name__ == "__main__":
    sys.exit(main())

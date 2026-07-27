"""Scan all git-tracked files for owner-identifying terms before any push.

The term list itself is sensitive, so it lives OUTSIDE the repo: set
JOBCANNON_LEAK_TERMS_FILE to a UTF-8 file with one lowercase term per line.
Exit 0 = clean (or skipped with a loud warning), exit 1 = hits found.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys


def main() -> int:
    terms_file = os.environ.get("JOBCANNON_LEAK_TERMS_FILE")
    if not terms_file or not pathlib.Path(terms_file).exists():
        print(
            "leak-guard: JOBCANNON_LEAK_TERMS_FILE not set/found — SKIPPED. "
            "Set it before pushing anything."
        )
        return 0
    terms = [
        t.strip().lower()
        for t in pathlib.Path(terms_file).read_text(encoding="utf-8").splitlines()
        if t.strip()
    ]
    # Tracked + staged + untracked: freshly-copied files must be visible to the
    # scan BEFORE they are ever committed (git ls-files alone misses untracked).
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    tracked = sorted(set(tracked) | set(untracked))
    hits: list[str] = []
    for rel in tracked:
        # AGPL license attribution intentionally carries the repo owner's own
        # public GitHub handle (github.com/<owner>/jobcannon) — that is not a
        # leak, it is required copyright notice. Every other path stays scanned.
        if pathlib.PurePosixPath(rel).name == "LICENSE":
            continue
        try:
            text = pathlib.Path(rel).read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for t in terms:
            if t in text:
                hits.append(f"{rel}: contains {t!r}")
    for h in hits:
        print(h)
    if hits:
        print(f"leak-guard: {len(hits)} hit(s) — DO NOT PUSH")
        return 1
    print(f"leak-guard: clean ({len(tracked)} tracked files, {len(terms)} terms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

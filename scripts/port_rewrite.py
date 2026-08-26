"""Rewrite private-repo import prefixes to jobcannon.engine equivalents.

Usage: python scripts/port_rewrite.py <file-or-dir> [<file-or-dir> ...]
       python scripts/port_rewrite.py --include-committed <file-or-dir> [...]
Idempotent; safe to re-run. Order matters: longest/most-specific first.

Rewrites freshly-copied files only: in already-committed files, surviving
`job_finder` mentions are deliberate provenance prose that a blind rewrite
corrupts into false `jobcannon.engine.*` paths.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

REWRITES: list[tuple[str, str]] = [
    # moved-and-renamed modules / re-exported symbols
    (
        r"\bfrom job_finder\.db import derive_classification\b",
        "from jobcannon.engine.classification import derive_classification",
    ),
    (
        r"\bfrom job_finder\.db import JobAssessment\b",
        "from jobcannon.engine.classification import JobAssessment",
    ),
    # NOTE: deliberately NO blanket `from job_finder.db import` rule — ats_scanner's
    # `from job_finder.db import upsert_job` must SURVIVE as loud boundary-test
    # residue so Task 3 hand-routes it to svc.upsert_job (a blanket rule would
    # silently rewrite it to a nonexistent module).
    (
        r"\bfrom job_finder\.db\._classification import\b",
        "from jobcannon.engine.classification import",
    ),
    (
        r"\bfrom job_finder\.db\._jd_full import _is_jd_junk\b",
        "from jobcannon.engine.jd_content_contract import _is_jd_junk",
    ),
    (
        r"\bfrom job_finder\.db\._jd_content_contract import\b",
        "from jobcannon.engine.jd_content_contract import",
    ),
    # subpackages and web siblings: name preserved, prefix swapped
    (r"\bjob_finder\.web\.", "jobcannon.engine."),
    # top-level portable modules
    (r"\bjob_finder\.models\b", "jobcannon.engine.models"),
    (r"\bjob_finder\.parsed_job\b", "jobcannon.engine.parsed_job"),
    (r"\bjob_finder\.normalizers\b", "jobcannon.engine.normalizers"),
    (r"\bjob_finder\.salary_normalizer\b", "jobcannon.engine.salary_normalizer"),
    (r"\bjob_finder\.json_utils\b", "jobcannon.engine.json_utils"),
    (r"\bjob_finder\.constants\b", "jobcannon.engine.constants"),
    (r"\bjob_finder\.enrichment_states\b", "jobcannon.engine.enrichment_states"),
]
# Anything still matching \bjob_finder\b after rewriting is a seam-edit
# worklist item (config, secrets, db, non-ported web modules) — the boundary
# test will list them.


def _is_committed_and_unmodified(path: pathlib.Path) -> bool:
    """True if `path` is tracked by git with no uncommitted changes.

    Derived entirely from git state — no path allowlists. A tracked,
    unmodified file was rewritten (if it ever needed it) before its first
    commit; any `job_finder` substring still in it today is deliberate
    provenance prose, not missed mechanical residue.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", str(path)],
        capture_output=True,
        text=True,
    )
    if tracked.returncode != 0:
        return False  # untracked (freshly copied) — always eligible
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return status.stdout.strip() == ""


def rewrite_file(path: pathlib.Path) -> bool:
    text = path.read_text(encoding="utf-8")
    new = text
    for pat, repl in REWRITES:
        new = re.sub(pat, repl, new)
    if new != text:
        path.write_text(new, encoding="utf-8", newline="\n")
        return True
    return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--include-committed",
        action="store_true",
        help="Also rewrite already-committed, unmodified files (rare deliberate case).",
    )
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)

    changed = 0
    skipped = 0
    for arg in args.paths:
        p = pathlib.Path(arg)
        files = p.rglob("*.py") if p.is_dir() else [p]
        for f in files:
            if not args.include_committed and _is_committed_and_unmodified(f):
                skipped += 1
                continue
            if rewrite_file(f):
                changed += 1
                print(f"rewrote {f}")
    if skipped:
        print(
            f"skipped {skipped} already-committed file(s) — port_rewrite only operates "
            "on freshly-copied (untracked or modified) files; use --include-committed "
            "to override"
        )
    print(f"{changed} file(s) changed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

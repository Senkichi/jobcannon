"""Derive and validate ``ported-paths.json``: the manifest of every file
under ``jobcannon/{engine,db,host,web,worker}`` that carries a provenance
comment naming its upstream private-repo counterpart.

The manifest is DERIVED, never hand-maintained: it is a direct function of
the comments the ported files already carry. A previous version of this
manifest only walked ``jobcannon/engine``, so parity gaps in the other four
package roots were invisible to any tooling that consulted it (issue #65) —
this version walks all five, discovered from ``PACKAGE_ROOTS`` below rather
than assuming any one root is "the" ported surface.

Provenance-comment convention
    Reading the comments/docstrings already committed across all five roots,
    porters consistently mark a file's upstream lineage one of two ways:
      1. a literal reference to the private package's import path, e.g.
         ``job_finder.web.model_provider.call_model`` or
         ``job_finder/db/_jd_full.py``;
      2. one of a small set of recurring phrases that name the private
         checkout as the file's origin: "the private repo's X", "the
         private source's X", "the private original", "the private
         deployment's X".
    Bare "private" is deliberately NOT a signal by itself — in this
    codebase it is overwhelmingly Python's privacy convention ("kept
    private to the package", "a private helper") and matching on it alone
    pulls in unrelated files. PROVENANCE_RE below encodes exactly the
    patterns above; see the sabotage/positive-control tests in
    tests/test_ported_paths_manifest.py for the false-positive cases this
    was checked against.

Modes
    derive (default)   Rewrite ``ported-paths.json`` from a fresh scan.
    --check             Read-only. Exits non-zero if the checked-in
                         manifest and a fresh scan disagree in any way:
                           * a provenance-bearing file is absent from the
                             manifest (the completeness gap issue #65 is
                             about);
                           * a manifest entry names a file that no longer
                             exists or no longer carries a provenance
                             comment (stale entry);
                           * an entry's recorded comment line(s) no longer
                             match the file (out of date — rerun derive).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
PACKAGE_ROOTS = ("engine", "db", "host", "web", "worker")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = REPO_ROOT / "ported-paths.json"

PROVENANCE_RE = re.compile(
    r"job_finder|private repo|private source|private original|private deployment",
    re.IGNORECASE,
)


def find_provenance_files(repo_root: Path) -> dict[str, list[dict]]:
    """Scan every package root under *repo_root* for provenance comments.

    Returns ``{relative_posix_path: [{"line": N, "text": stripped line}]}``,
    one dict entry per file that has at least one matching line, ordered by
    line number within the file. Directory discovery is dynamic
    (``PACKAGE_ROOTS`` names the five architectural package roots, not a
    hand-picked file list) so a newly ported file is picked up the moment it
    carries a provenance comment, with no manifest edit required.
    """
    found: dict[str, list[dict]] = {}
    for root_name in PACKAGE_ROOTS:
        root_dir = repo_root / "jobcannon" / root_name
        if not root_dir.is_dir():
            continue
        for py_file in sorted(root_dir.rglob("*.py")):
            text = py_file.read_text(encoding="utf-8")
            markers = [
                {"line": lineno, "text": line.strip()}
                for lineno, line in enumerate(text.splitlines(), start=1)
                if PROVENANCE_RE.search(line)
            ]
            if markers:
                found[py_file.relative_to(repo_root).as_posix()] = markers
    return found


def build_manifest(repo_root: Path) -> dict:
    found = find_provenance_files(repo_root)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "roots": list(PACKAGE_ROOTS),
        "entries": [
            {"path": path, "root": path.split("/")[1], "markers": found[path]}
            for path in sorted(found)
        ],
    }


def write_manifest(manifest: dict, manifest_path: Path) -> None:
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def check(repo_root: Path, manifest_path: Path) -> tuple[bool, list[str]]:
    """Read-only completeness/freshness check. See module docstring."""
    if not manifest_path.exists():
        return False, [f"manifest missing at {manifest_path}"]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        failures.append(
            f"schema_version must be {SCHEMA_VERSION}, got {manifest.get('schema_version')!r}"
        )

    current = find_provenance_files(repo_root)
    recorded = {e["path"]: e for e in manifest.get("entries", [])}

    missing = sorted(set(current) - set(recorded))
    if missing:
        failures.append(
            f"{len(missing)} file(s) carry a provenance comment but are absent from "
            f"the manifest: {missing}"
        )

    stale = sorted(set(recorded) - set(current))
    if stale:
        failures.append(
            f"{len(stale)} manifest entry(ies) name a file with no provenance comment "
            f"today (deleted, edited, or never real): {stale}"
        )

    drifted = sorted(
        p for p in set(current) & set(recorded) if recorded[p].get("markers") != current[p]
    )
    if drifted:
        failures.append(
            f"{len(drifted)} manifest entry(ies) are out of date with the file's "
            f"current comments — rerun `python scripts/derive_ported_paths.py`: {drifted}"
        )

    return not failures, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate only; write nothing")
    parser.add_argument(
        "--manifest",
        default=None,
        help=f"manifest path (default: {DEFAULT_MANIFEST_PATH})",
    )
    args = parser.parse_args(argv)
    manifest_path = Path(args.manifest) if args.manifest else DEFAULT_MANIFEST_PATH

    if args.check:
        ok, failures = check(REPO_ROOT, manifest_path)
        if ok:
            n = len(json.loads(manifest_path.read_text(encoding="utf-8"))["entries"])
            print(
                f"ported-paths check: OK — {n} provenance-bearing file(s) across "
                f"{len(PACKAGE_ROOTS)} package roots"
            )
            return 0
        for failure in failures:
            print(f"ported-paths check: FAIL — {failure}")
        print(f"ported-paths check: {len(failures)} failure(s)")
        return 1

    manifest = build_manifest(REPO_ROOT)
    write_manifest(manifest, manifest_path)
    print(f"ported-paths derive: wrote {manifest_path} — {len(manifest['entries'])} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())

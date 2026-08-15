"""ported-paths.json completeness guard (issue #65).

The manifest at repo root maps every jobcannon/{engine,db,host,web,worker}
.py file that carries a provenance comment (see scripts/derive_ported_paths.py
for the exact convention and PROVENANCE_RE) to the comment line(s) that
earned it a place. test_manifest_matches_fresh_scan fails whenever the
checked-in manifest drifts from a fresh scan of the tree — including the
completeness gap this manifest exists to close: a provenance-bearing file
silently absent from the manifest. The remaining tests are a sabotage
self-test (proving the checker actually fires on a synthetic gap, matching
tests/engine/test_boundary.py's established pattern) and positive/negative
controls on PROVENANCE_RE itself, since the whole guard is only as good as
that regex's precision and recall.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "derive_ported_paths.py"
MANIFEST_PATH = REPO_ROOT / "ported-paths.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("derive_ported_paths", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dpp = _load_module()


def test_manifest_matches_fresh_scan():
    """The checked-in manifest must equal what a fresh scan derives right now.

    This is the completeness check issue #65 asks for: if any file under a
    package root gains, loses, or edits a provenance comment without
    `python scripts/derive_ported_paths.py` being rerun, this fails.
    """
    ok, failures = dpp.check(REPO_ROOT, MANIFEST_PATH)
    assert ok, "\n".join(failures)


def test_manifest_covers_multiple_roots():
    """Regression guard for the exact bug in #65: a manifest that only
    enumerates jobcannon/engine would still pass a naive existence check.
    Assert real coverage outside engine/, using the roots this repo actually
    has non-trivial ported surface in today (web/ and worker/ are net-new
    hosted-product code with no upstream counterpart yet, so 0 there is
    correct, not a gap — see the PR description for the positive count)."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    roots_present = {e["root"] for e in manifest["entries"]}
    assert {"engine", "db", "host"} <= roots_present
    assert manifest["roots"] == ["engine", "db", "host", "web", "worker"]


def test_checker_catches_provenance_file_missing_from_manifest(tmp_path):
    """Sabotage self-test for the gap this tool exists to close: a file
    carrying a provenance comment that the manifest doesn't know about must
    fail the check, not pass it silently."""
    (tmp_path / "jobcannon" / "engine").mkdir(parents=True)
    (tmp_path / "jobcannon" / "engine" / "ported_thing.py").write_text(
        '"""Ported from the private repo\'s ported_thing.py."""\n',
        encoding="utf-8",
    )
    empty_manifest = tmp_path / "ported-paths.json"
    empty_manifest.write_text(
        json.dumps({"schema_version": 1, "generated_at": "x", "roots": [], "entries": []}),
        encoding="utf-8",
    )

    ok, failures = dpp.check(tmp_path, empty_manifest)

    assert not ok
    assert any("jobcannon/engine/ported_thing.py" in f for f in failures)


def test_checker_accepts_complete_manifest(tmp_path):
    """Companion positive case: a manifest built by build_manifest() for a
    synthetic tree must itself pass check() against that same tree — proves
    the checker isn't just failing everything."""
    (tmp_path / "jobcannon" / "db").mkdir(parents=True)
    (tmp_path / "jobcannon" / "db" / "thing.py").write_text(
        '"""Postgres port of the private repo\'s thing.py."""\n',
        encoding="utf-8",
    )
    manifest_path = tmp_path / "ported-paths.json"
    dpp.write_manifest(dpp.build_manifest(tmp_path), manifest_path)

    ok, failures = dpp.check(tmp_path, manifest_path)

    assert ok, "\n".join(failures)


def test_checker_catches_stale_entry(tmp_path):
    """A manifest entry naming a file that no longer carries a provenance
    comment (or no longer exists) must fail, not linger silently forever."""
    (tmp_path / "jobcannon" / "web").mkdir(parents=True)
    manifest_path = tmp_path / "ported-paths.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "x",
                "roots": [],
                "entries": [
                    {
                        "path": "jobcannon/web/gone.py",
                        "root": "web",
                        "markers": [{"line": 1, "text": "ported from job_finder"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    ok, failures = dpp.check(tmp_path, manifest_path)

    assert not ok
    assert any("jobcannon/web/gone.py" in f for f in failures)


def test_provenance_regex_rejects_bare_python_privacy_mentions():
    """Negative control: the word 'private' alone, used in its ordinary
    Python-privacy sense, must NOT trip the detector — this codebase uses
    that sense far more often than the porting sense, and an early full
    scan against it produced exactly these false-positive shapes."""
    non_provenance_lines = [
        "is intentionally kept private to the package",
        "that helper is private and not importable",
        "instead of each carrying a private copy",
        "bypassing the private import",
    ]
    for line in non_provenance_lines:
        assert not dpp.PROVENANCE_RE.search(line), line


def test_provenance_regex_accepts_known_phrasings():
    """Positive control mirroring the phrasings actually found across the
    tree (engine/db/host) during manifest derivation."""
    provenance_lines = [
        "Postgres port of the private repo's single postings writer.",
        "In the private source, a dedicated ats_reconciler.py module",
        "Same 4-layer gate chain as the private original, with the two",
        "parity with the private repo's JD_STORAGE_MAX_CHARS",
        "was job_finder.web.autoheal.override_loader.",
    ]
    for line in provenance_lines:
        assert dpp.PROVENANCE_RE.search(line), line

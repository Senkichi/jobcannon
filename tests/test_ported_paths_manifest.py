"""ported-paths.json completeness guard (issue #65).

The manifest at repo root maps every jobcannon/{engine,db,host,web,worker}
.py file that carries a provenance comment (see scripts/derive_ported_paths.py
for the exact detector contract and PROVENANCE_RE) to the comment line(s)
that earned it a place. test_manifest_matches_fresh_scan fails whenever the
checked-in manifest drifts from a fresh scan of the tree — including the
completeness gap this manifest exists to close: a provenance-bearing file
silently absent from the manifest.

test_manifest_matches_fresh_scan is necessarily circular (it re-runs the
same detector the manifest was derived with, so it can prove drift but can
never prove the detector itself has adequate recall). The remaining tests
are the non-circular controls that guard the detector's actual recall
surface:
  * a sabotage self-test with a plain single-line phrase (matching
    tests/engine/test_boundary.py's established pattern);
  * a sabotage self-test with a phrase that WRAPS across a comment's line
    break — the exact shape of the jobcannon/engine/ats_prober.py miss;
  * a sabotage self-test with a novel code-artifact noun the original
    four-phrase regex never covered — the exact shape of the
    jobcannon/db/compat.py miss;
  * a ground-truth pin asserting those two known-provenance files are
    actually present in the checked-in manifest, so a future regression in
    the detector (not just a stale manifest) fails loudly;
  * positive/negative controls on PROVENANCE_RE itself, since the whole
    guard is only as good as that regex's precision and recall.
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


def test_checker_catches_wrapped_provenance_phrase(tmp_path):
    """Non-circular recall control for the exact shape of the
    jobcannon/engine/ats_prober.py miss: a provenance phrase that line-wraps
    inside a comment ("...the private" / "source's helper...") must still be
    detected. A per-line scan (the pre-fix implementation) cannot see this —
    each half of the phrase is unremarkable on its own line — so this test
    fails under the old per-line PROVENANCE_RE.search(line) approach and
    only passes once matching runs against joined comment/docstring blocks.
    """
    (tmp_path / "jobcannon" / "engine").mkdir(parents=True)
    (tmp_path / "jobcannon" / "engine" / "wrapped_thing.py").write_text(
        "# Host-injectable seam. Replaces the private\n"
        "# source's helper that does not port to the engine.\n"
        "_thing = None\n",
        encoding="utf-8",
    )
    empty_manifest = tmp_path / "ported-paths.json"
    empty_manifest.write_text(
        json.dumps({"schema_version": 1, "generated_at": "x", "roots": [], "entries": []}),
        encoding="utf-8",
    )

    ok, failures = dpp.check(tmp_path, empty_manifest)

    assert not ok
    assert any("jobcannon/engine/wrapped_thing.py" in f for f in failures)


def test_checker_catches_novel_noun_phrase(tmp_path):
    """Non-circular recall control for the exact shape of the
    jobcannon/db/compat.py miss: a provenance phrase using a code-artifact
    noun ("schema") outside the original hand-picked four-phrase set
    (repo/source/original/deployment) must still be detected. This fails
    under the pre-fix hardcoded-phrase PROVENANCE_RE and only passes once
    the detector generalizes to proximity-based noun matching.
    """
    (tmp_path / "jobcannon" / "db").mkdir(parents=True)
    (tmp_path / "jobcannon" / "db" / "novel_noun_thing.py").write_text(
        '"""Records a private-schema delta not on the port surface."""\n',
        encoding="utf-8",
    )
    empty_manifest = tmp_path / "ported-paths.json"
    empty_manifest.write_text(
        json.dumps({"schema_version": 1, "generated_at": "x", "roots": [], "entries": []}),
        encoding="utf-8",
    )

    ok, failures = dpp.check(tmp_path, empty_manifest)

    assert not ok
    assert any("jobcannon/db/novel_noun_thing.py" in f for f in failures)


def test_manifest_pins_known_provenance_files():
    """Ground-truth pin: these two files are unambiguously provenance-
    bearing (jobcannon/db/compat.py names a private migration id and a
    private-schema delta; jobcannon/engine/ats_prober.py's provenance phrase
    wraps across a comment line break). Both were misses of the original
    per-line, four-phrase detector.

    Deliberately calls find_provenance_files() directly — a fresh scan —
    rather than reading the checked-in ported-paths.json. Reading the JSON
    would only prove the file hasn't gone stale (test_manifest_matches_fresh_scan
    already covers that); it would keep passing even if PROVENANCE_RE
    regressed to stop matching these files, as long as nobody reran derive.
    Also pins the specific matched lines, not just file presence, so a
    detector that keeps these files in the manifest for an unrelated /
    coincidental reason still fails this check.
    """
    found = dpp.find_provenance_files(REPO_ROOT)
    assert "jobcannon/db/compat.py" in found
    assert "jobcannon/engine/ats_prober.py" in found

    compat_lines = {m["line"] for m in found["jobcannon/db/compat.py"]}
    assert {67, 68} & compat_lines, (
        "expected the private-schema/private-migration lines in compat.py's "
        f"markers, got lines {sorted(compat_lines)}"
    )

    prober_lines = {m["line"] for m in found["jobcannon/engine/ats_prober.py"]}
    assert 21 in prober_lines, (
        "expected line 21 (the wrapped 'the private' / 'source's lazy "
        f"imports' phrase) in ats_prober.py's markers, got lines {sorted(prober_lines)}"
    )


def test_tight_gap_excludes_unrelated_local_filename_near_private():
    """Regression guard for the noun-gap/tight-gap split in PROVENANCE_RE.

    jobcannon/engine/ats_scanner/__init__.py's module docstring says "...live
    in private sibling modules: `_upsert.py` ..." — ordinary Python
    underscore-privacy describing THIS repo's own sibling files, not a
    cross-repo reference — sitting a few words from an unrelated `.py`
    filename. An earlier version of this detector (single wide proximity
    window for every artifact type) matched it. These three real sentences
    from the tree, plus that one, must NOT match on their own; if a future
    change collapses the tight/noun gap split back into one budget, this is
    what would go red (the other 10 tests would stay green, since none of
    them exercises this specific ambiguity).
    """
    found = dpp.find_provenance_files(REPO_ROOT)
    for path in (
        "jobcannon/engine/ats_platforms/_title_match.py",
        "jobcannon/engine/stale_detector.py",
        "jobcannon/engine/ats_scanner/_run_html.py",
    ):
        assert path not in found, f"{path} should not be provenance-bearing"


def test_provenance_regex_rejects_local_underscore_privacy_near_filename():
    """Isolated regex-level version of the same tight-gap regression,
    reproducing jobcannon/engine/ats_scanner/__init__.py's exact phrasing on
    its own — not through find_provenance_files() — so this fails cleanly
    even in a hypothetical world where that file also carries a genuine,
    unrelated provenance line elsewhere (as it currently does, which is why
    the file-level test above targets three *other* real files instead)."""
    block = (
        "The package's first-party concerns live in private sibling modules:\n"
        "\n"
        "- `_upsert.py`   — `is_company_tracked`: company tracking check.\n"
    )
    assert not dpp.PROVENANCE_RE.search(block), block


def test_provenance_regex_rejects_dotted_attribute_named_private():
    """Regression for a real false positive found (and fixed) while working
    on issue #178/#182: jobcannon/web/legal.py's Werkzeug Cache-Control usage
    `response.cache_control.private = True` is ordinary Python attribute
    access whose last segment happens to be spelled "private" — it is ONE
    identifier chain, not a code-artifact filename/module-path followed by a
    separately-written, descriptive "private". The mirrored
    signal-then-private alternative used to match this via a bare "."
    tight-gap, which is indistinguishable from continuing the same dotted
    chain. Prose with an actual word-level separator (space, comma) between
    a filename and "private" must still match — only the bare-dot,
    same-token case is excluded."""
    assert not dpp.PROVENANCE_RE.search("response.cache_control.private = True"), (
        "dotted attribute access ending in .private must not be treated as provenance"
    )
    assert not dpp.PROVENANCE_RE.search("self.some_module.private"), (
        "same shape with a different attribute name must also be excluded"
    )
    # Positive control: prose still matches when a real word-gap separates
    # the filename from "private" (the shape _TIGHT_GAP was written for).
    assert dpp.PROVENANCE_RE.search("the private data_enricher.py"), (
        "forward direction (private, then filename) must be unaffected"
    )
    assert dpp.PROVENANCE_RE.search("reconciler.py private variant"), (
        "backward direction with a real word-gap must still match"
    )


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

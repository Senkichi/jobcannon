"""Derive and validate ``ported-paths.json``: the manifest of every file
under ``jobcannon/{engine,db,host,web,worker}`` that carries a provenance
comment naming its upstream private-repo counterpart.

The manifest is DERIVED, never hand-maintained: it is a direct function of
the comments the ported files already carry. A previous version of this
manifest only walked ``jobcannon/engine``, so parity gaps in the other four
package roots were invisible to any tooling that consulted it (issue #65) —
this version walks all five, discovered from ``PACKAGE_ROOTS`` below rather
than assuming any one root is "the" ported surface.

Detector contract
    A file is provenance-bearing if, anywhere in its comments/docstrings, it
    contains EITHER:
      1. a literal ``job_finder`` reference (the private package's import
         root), unconditionally — e.g. ``job_finder.web.model_provider`` or
         ``job_finder/db/_jd_full.py``; or
      2. the word "private" within a short distance of a code-artifact
         signal: a ``.py`` filename, a dotted module-path-shaped token, a
         migration id (``m<digits>_...``), or one of the nouns porters use
         for the private checkout itself (repo, source, original,
         deployment, schema, migration, chain, function/functions).
    Bare "private" alone is NOT a signal — in this codebase it is
    overwhelmingly Python's privacy convention ("kept private to the
    package", "a private helper") and matching on it alone pulls in
    unrelated files (see the negative-control tests). Rule 2 is
    proximity-based rather than a fixed set of "private <noun>" bigrams
    specifically so novel phrasings (e.g. "a private-schema delta", "the
    private migration `m207454240_...`") are caught by construction instead
    of requiring the noun list to be extended by hand every time a porter
    phrases it differently.

    Rule 2's proximity budget is NOT one uniform window — it is two, with
    different tolerances for the two artifact families, because they carry
    different false-positive risk:
      * the noun list (schema/migration/chain/function/...) is inherently
        provenance-flavored vocabulary, so up to 3 intervening words are
        allowed — needed for phrasings like "the private location-
        enrichment chain".
      * a bare ``.py`` filename / dotted path / migration id is required to
        sit IMMEDIATELY next to "private" (only punctuation/whitespace
        between, no intervening word). A wider budget here produced a false
        positive on jobcannon/engine/ats_scanner/__init__.py's "...live in
        private sibling modules: `_upsert.py` ..." — ordinary Python underscore-
        privacy describing this repo's own sibling files, a few words from
        an unrelated filename, with no cross-repo meaning at all. See
        test_tight_gap_excludes_unrelated_local_filename_near_private and
        test_provenance_regex_rejects_local_underscore_privacy_near_filename.

    Matching runs against COMMENT BLOCKS and DOCSTRINGS as joined,
    multi-line text (via ``tokenize``), not per source line — a provenance
    phrase that wraps across a comment's line break (e.g. "...the private" /
    "source's lazy imports...") is still one contiguous phrase to the
    detector. Lines outside any comment/docstring are still scanned
    individually, so nothing loses coverage relative to the old per-line
    scan; only comment/docstring content gets merged across line breaks.

    PROVENANCE_RE encodes rule 2's code-artifact alternation and the
    proximity window; rule 1 (bare ``job_finder``) is folded into the same
    pattern. See the sabotage/positive-control tests in
    tests/test_ported_paths_manifest.py — including a synthetic wrapped
    phrase and a synthetic novel-noun phrase that MUST be caught, and
    jobcannon/db/compat.py + jobcannon/engine/ats_prober.py pinned as
    known-provenance ground truth — for what this detector's recall claim
    actually rests on.

Marker form (issue #278)
    The header convention is a line-1 ``#`` comment — ``# PORTED from
    <item> @ <sha> (private job-cannon). Ledger <row>.`` — never a second,
    back-to-back triple-quoted PORTED docstring stacked before the
    module's original docstring. A bare string is still a statement: a
    following ``from __future__ import`` then lands after real code and
    raises SyntaxError ("from __future__ imports must occur at the
    beginning of the file"), and without one the marker silently becomes
    ``__doc__`` while the original docstring degrades to a dead string
    literal — the version of the bug nothing else catches. A marker fused
    into the single module docstring's first line (the form most
    already-landed ports carry) stays legal — the docstring still leads
    the file — but new ports use the comment form so the module docstring
    stays verbatim-identical to its private source for fidelity diffs.
    ``find_misplaced_provenance_headers`` and its test in
    tests/test_ported_paths_manifest.py enforce the boundary across the
    port surface (the five package roots plus tests/).

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
import ast
import io
import json
import re
import sys
import tokenize
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
PACKAGE_ROOTS = ("engine", "db", "host", "web", "worker")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = REPO_ROOT / "ported-paths.json"

# Nouns porters use, across engine/db/host today, for the private checkout
# itself when not naming a job_finder path directly. Deliberately excludes
# generic words ("module", "package", "copy", "import", ...) that show up
# constantly in ordinary Python-privacy sentences near "private" — see
# test_provenance_regex_rejects_bare_python_privacy_mentions, which pins
# real sentences from this tree (jobcannon/engine/ats_platforms/_title_match.py,
# jobcannon/engine/stale_detector.py) that must NOT match.
_ARTIFACT_NOUNS = r"repo|source|original|deployment|schema|migrations?|chain|functions?"
_PY_FILENAME = r"\b\w+\.py\b"
# Dotted-path-shaped token, e.g. job_finder.web.foo or data_enricher.py's
# sibling data_enricher.<attr>. Each segment requires 2+ chars so common
# non-code abbreviations ("e.g.", "i.e.") can't masquerade as a module path.
_MODULE_PATH = r"\b[A-Za-z_]\w+(?:\.[A-Za-z_]\w+)+\b"
_MIGRATION_ID = r"\bm\d+_\w+\b"
_FILE_LIKE_SIGNAL = rf"(?:{_PY_FILENAME}|{_MODULE_PATH}|{_MIGRATION_ID})"

# Two different proximity budgets for "private", not one, because the two
# artifact families carry different false-positive risk:
#   - the noun list (schema/migration/chain/function/...) is inherently
#     provenance-flavored vocabulary, so a few intervening words are safe —
#     needed for e.g. "the private location-enrichment chain" (2 words
#     between "private" and "chain").
#   - a bare .py filename / dotted path / migration id can show up next to
#     "private" in ordinary same-repo prose with NO cross-repo meaning at
#     all — e.g. jobcannon/engine/ats_scanner/__init__.py's "...live in
#     private sibling modules: `_upsert.py` ..." is Python's own
#     underscore-privacy convention describing THIS repo's sibling files,
#     not a reference to the private origin. Requiring these to sit
#     immediately next to "private" (only punctuation/whitespace between,
#     no intervening word) keeps that out while still catching a filename
#     directly glued to "private" (e.g. "the private data_enricher.py").
_NOUN_GAP = r"(?:\W+\w+){0,3}\W+"
_TIGHT_GAP = r"\W+"
# Same tight-adjacency idea, but for the mirrored "signal, then private"
# direction, the gap additionally excludes a bare "." — a dotted attribute
# chain like `response.cache_control.private` (Werkzeug's Cache-Control API;
# jobcannon/web/legal.py, issue #182 item 4) is a SINGLE Python identifier whose
# final segment happens to be the word "private", not a code-artifact
# signal followed by a separately-written, descriptive "private". _MODULE_PATH
# backtracks to match "response.cache_control" and would otherwise treat the
# following ".private" as the tight gap plus the word — indistinguishable,
# with a plain \W+ gap, from real prose like "the private helper is
# reconciler.py.private" (which does not occur; a "." here is always a
# same-token attribute-access continuation, never a sentence separator).
# Whitespace/comma-separated prose ("reconciler.py private", "reconciler.py,
# private") still matches. See
# test_provenance_regex_rejects_dotted_attribute_named_private.
_TIGHT_GAP_NO_DOT = r"[^\w.]+"

PROVENANCE_RE = re.compile(
    rf"\bjob_finder\b"
    rf"|\bprivate\b{_NOUN_GAP}\b(?:{_ARTIFACT_NOUNS})\b"
    rf"|\b(?:{_ARTIFACT_NOUNS})\b{_NOUN_GAP}\bprivate\b"
    rf"|\bprivate\b{_TIGHT_GAP}{_FILE_LIKE_SIGNAL}"
    rf"|{_FILE_LIKE_SIGNAL}{_TIGHT_GAP_NO_DOT}\bprivate\b",
    re.IGNORECASE,
)

# The convention word itself, for spotting a PORTED header that names neither
# job_finder nor a "private"-proximity artifact (e.g. "PORTED from
# tests/test_x.py @ <sha>. Ledger L-0001."). Only consulted on string literals
# that are already in a suspicious position (a dead module-level statement),
# not run over whole files — a bare "PORTED" in ordinary prose is not a
# provenance signal on its own.
_PORTED_WORD_RE = re.compile(r"\bPORTED\b")


def _match_spans_to_lines(text: str, matches: list[re.Match]) -> set[int]:
    """Map each regex match's character span in *text* to the 1-indexed
    line number(s) it covers.

    Used so the manifest's self-auditing "line"/"text" markers point at the
    text that actually satisfied PROVENANCE_RE, not just any line that
    happens to contain the word "private" inside an already-confirmed
    block — e.g. jobcannon/engine/ats_scanner/__init__.py's module
    docstring is provenance-bearing because of "the private source they
    were static top-level imports" elsewhere in the same docstring, but its
    unrelated "private sibling modules" line (ordinary underscore-privacy,
    talking about this repo's own `_upsert.py`/`_probe.py`) should not be
    reported as if it were the reason.
    """
    covered: set[int] = set()
    for m in matches:
        start_line = text.count("\n", 0, m.start()) + 1
        end_line = text.count("\n", 0, max(m.end() - 1, 0)) + 1
        covered.update(range(start_line, end_line + 1))
    return covered


def _comment_and_docstring_blocks(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start_line, end_line, block_text)`` for each contiguous
    comment run and each triple-quoted string literal in *text*.

    Uses the standard-library tokenizer (not a hand-rolled ``#``/quote
    scanner) so strings containing ``#`` or escaped quotes don't confuse
    block boundaries. Consecutive ``#`` lines with no blank/code line
    between them are joined into one block, which is what lets a provenance
    phrase that line-wraps inside a comment survive as one contiguous
    string for PROVENANCE_RE to match against.
    """
    blocks: list[tuple[int, int, str]] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, UnicodeDecodeError):
        tokens = []

    for tok in tokens:
        if tok.type == tokenize.STRING and tok.string.lstrip().startswith(('"""', "'''")):
            blocks.append((tok.start[0], tok.end[0], tok.string))

    comment_lines = sorted({tok.start[0] for tok in tokens if tok.type == tokenize.COMMENT})
    lines = text.splitlines()
    i = 0
    while i < len(comment_lines):
        start = end = comment_lines[i]
        i += 1
        while i < len(comment_lines) and comment_lines[i] == end + 1:
            end = comment_lines[i]
            i += 1
        blocks.append((start, end, "\n".join(lines[start - 1 : end])))

    return blocks


def _find_markers(text: str) -> list[dict]:
    """Return self-auditing ``{"line": N, "text": ...}`` markers for every
    provenance-bearing line in *text*.

    Matching happens at block granularity (comment runs and docstrings
    joined, per ``_comment_and_docstring_blocks``) so a phrase that wraps
    across a comment's line break is still caught; any line not covered by
    a block is still checked on its own, so ordinary code lines keep the
    same coverage the old per-line scan gave them.
    """
    lines = text.splitlines()
    covered = [False] * (len(lines) + 1)  # 1-indexed
    marker_lines: set[int] = set()

    for start, end, block_text in _comment_and_docstring_blocks(text):
        for lineno in range(start, min(end, len(lines)) + 1):
            covered[lineno] = True
        matches = list(PROVENANCE_RE.finditer(block_text))
        for rel_line in _match_spans_to_lines(block_text, matches):
            abs_line = start + rel_line - 1
            if abs_line <= min(end, len(lines)):
                marker_lines.add(abs_line)

    for lineno, line in enumerate(lines, start=1):
        if not covered[lineno] and PROVENANCE_RE.search(line):
            marker_lines.add(lineno)

    return [{"line": lineno, "text": lines[lineno - 1].strip()} for lineno in sorted(marker_lines)]


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
            markers = _find_markers(text)
            if markers:
                found[py_file.relative_to(repo_root).as_posix()] = markers
    return found


def _is_bare_string_statement(node: ast.stmt) -> bool:
    """True for a module-level statement that is just a string literal — the
    module docstring itself, or the dead-string shape a misplaced provenance
    header takes (issue #278)."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_provenance_marker(text: str) -> bool:
    return bool(PROVENANCE_RE.search(text) or _PORTED_WORD_RE.search(text))


def _is_future_import(node: ast.stmt) -> bool:
    return isinstance(node, ast.ImportFrom) and node.module == "__future__"


def find_misplaced_provenance_headers(repo_root: Path) -> list[str]:
    """Scan the port surface for provenance markers in dead-string position.

    A marker is legal in a ``#`` comment or inside the single module
    docstring. Written as a SECOND triple-quoted statement stacked before
    (or after) the original docstring it is a plain expression statement:
    any following ``from __future__ import`` then raises SyntaxError, and
    without one the stack still compiles — the marker steals ``__doc__``
    while the original docstring becomes dead text (issue #278). Both
    shapes are caught: ast.parse surfaces the future-import failure
    directly, and the statement walk flags a marker-bearing dead string or
    a marker-bearing docstring whose original docstring was displaced into
    a dead slot behind it — including the "move the ``from __future__``
    import up" variant, where the displaced string lands after the import
    but is still dead text (and a ``-``-only hunk to fidelity diffs).
    Scans ``jobcannon/{PACKAGE_ROOTS}`` plus ``tests/`` — ported test
    files carry the same headers and the same hazard.

    Returns sorted ``"path:line: reason"`` strings; empty means clean.
    """
    offenders: list[str] = []
    scan_dirs = [repo_root / "jobcannon" / root for root in PACKAGE_ROOTS]
    tests_dir = repo_root / "tests"
    if tests_dir.is_dir():
        scan_dirs.append(tests_dir)

    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for py_file in sorted(scan_dir.rglob("*.py")):
            rel = py_file.relative_to(repo_root).as_posix()
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                # A marker-stacked header that precedes `from __future__`
                # never reaches the statement walk — it fails here first.
                if "from __future__" in str(exc):
                    offenders.append(f"{rel}:{exc.lineno}: {exc.msg}")
                continue
            body = tree.body
            # The module docstring is a legal marker position — but a second
            # string statement while still in header position (only
            # `from __future__` imports between it and the docstring) is the
            # original docstring displaced into a dead slot: the issue-#278
            # stack, including its "move the future import up" variant. The
            # displaced string carries no marker of its own, so it is only
            # reachable through the docstring. A dead string AFTER any real
            # statement is a different matter (e.g. the variable-docstring
            # idiom) and is not this convention's concern.
            docstring_marked = (
                bool(body)
                and _is_bare_string_statement(body[0])
                and _is_provenance_marker(body[0].value.value)
            )
            for i, node in enumerate(body):
                if i == 0 or not _is_bare_string_statement(node):
                    continue
                if _is_provenance_marker(node.value.value):
                    reason = "provenance marker in a dead module-level string statement"
                elif docstring_marked and all(_is_future_import(n) for n in body[1:i]):
                    reason = "module docstring displaced by a provenance docstring above it"
                else:
                    continue
                offenders.append(
                    f"{rel}:{node.lineno}: {reason} — `from __future__` after it is a "
                    "SyntaxError; use a `# PORTED` line-1 comment instead (issue #278)"
                )
    return sorted(offenders)


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

"""scripts/import_legal_text.py — mechanically strip drafting/review matter
out of a ratified legal-text draft and write the published .md the app
serves (issue #94).

Usage:
    python scripts/import_legal_text.py --source <draft.md> --target privacy \
        --effective-date 2026-08-27 [--public-root .]

`--target` selects which committed file gets (over)written:
`jobcannon/web/legal/privacy.md` or `jobcannon/web/legal/terms.md`, under
`--public-root` (default: the current directory — run this from the repo
root, same as every other script here).

The strip is deliberately MECHANICAL, not judgment-based: it removes only
what the draft's own authoring convention marks as non-publication matter
(the leading DRAFT blockquote banner, every HTML comment, and — for the
privacy policy only — the Appendix A gap register), then fills in the one
open placeholder ([EFFECTIVE DATE], matched case-insensitively so a drafting
variant like [Effective Date] is filled too) and does whitespace cleanup. It makes no
decision about what the TEXT says; that is the ratification step, done
before this script ever runs on a given draft.

Before writing, the result is checked with
`jobcannon.web.legal_guard.check_published_text` — the SAME function the
committed .md files are checked against in tests/host/test_legal_pages.py.
A violation refuses the write rather than producing a file the test suite
would immediately fail on. If a legitimate piece of ratified prose ever
trips the guard, fix the guard's rule or this script's strip logic — never
hand-edit the committed .md, since it is a generated artifact and the next
import overwrites it.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

# Runs as `python scripts/import_legal_text.py` from the repo root, so the
# repo root must be importable even though this file lives under scripts/.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jobcannon.web.legal_guard import check_published_text  # noqa: E402

_TARGET_FILENAMES = {"privacy": "privacy.md", "terms": "terms.md"}

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_EFFECTIVE_DATE_ARG = re.compile(r"\d{4}-\d{2}-\d{2}")
_TRAILING_RULE_OR_BLANK = re.compile(r"^(-{3,})?\s*$")
# Case-insensitive: the draft's own placeholder is written [EFFECTIVE DATE],
# but legal_guard.check_published_text's bracket rule now also flags mixed
# case ([Effective Date], [effective date], ...) as an unfilled placeholder
# (issue #94 guard-hardening review) — a case-sensitive .replace() here would
# leave a mixed-case variant unfilled and the guard call below refuses the
# write, so this fills every case variant of the same placeholder.
_EFFECTIVE_DATE_PLACEHOLDER = re.compile(r"\[effective date\]", re.IGNORECASE)

# issue #178: a Markdown table delimiter row is only ever valid immediately
# after its header row, so a blank line sitting between the two always
# indicates upstream corruption (here: an HTML comment that occupied that
# line and left an empty line behind once _strip_html_comments removed its
# text), never deliberate spacing.
#
# The fix is scoped to exactly this table-boundary shape rather than made
# "a comment-only line never leaves a blank line" in general: the draft
# carries an audit-citation comment after nearly every paragraph and list
# item, each currently leaving one blank line that (combined with the
# paragraph's own separator) produces the double-blank-line spacing already
# baked into the committed .md files. A general fix was tried and measured
# against both committed files — it also collapsed list items from "loose"
# to "tight" Markdown (blank line between `- ` items removed), which changes
# the rendered HTML (loose-list items are wrapped in `<p>`, tight-list items
# are not), not just cosmetic .md whitespace. That is exactly the kind of
# unreviewed drift the regeneration diff check below exists to catch, so the
# fix stays narrow: only the shape that actually breaks something is
# corrected, everywhere else in the document is untouched byte-for-byte.
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DELIMITER_ROW = re.compile(r"^\s*\|(\s*:?-+:?\s*\|)+\s*$")

# issue #182 item 6: the first plain-text mention of the OTHER published
# legal document's name becomes a link to it. Keyed by --target because each
# document only cross-links to the other one.
_CROSS_LINK_TARGETS: dict[str, tuple[str, str]] = {
    "terms": ("Privacy Policy", "/privacy"),
    "privacy": ("Terms of Service", "/terms"),
}


def _strip_html_comments(text: str) -> str:
    return _HTML_COMMENT.sub("", text)


def _collapse_blank_before_table_delimiter(text: str) -> str:
    """Drop every blank line sitting between a table header row and its
    delimiter row (see _TABLE_ROW / _TABLE_DELIMITER_ROW above) — not just a
    single blank line. Two adjacent HTML comments each leave their own blank
    behind once _strip_html_comments removes their text, and
    _collapse_blank_lines (which runs earlier in build_published_text) only
    trims a run of 3+ blanks down to 2, never to 0, so a two-comment gap
    still has to be handled here.

    Coverage boundary: this only looks at the header-row -> delimiter-row
    junction, the one shape that is never valid Markdown regardless of
    cause. A whole-line comment left as a blank line between two BODY rows
    of an already-open table, or after a table's last row, is not touched
    here (mid-body/last-row blanks split or truncate a table without ever
    producing this specific header/delimiter adjacency) — #190 tracks the
    general HTML-comment-blank-line normalization that would also cover
    those shapes."""
    lines = text.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if _TABLE_ROW.match(lines[i]):
            j = i + 1
            while j < n and lines[j].strip() == "":
                j += 1
            if j > i + 1 and j < n and _TABLE_DELIMITER_ROW.match(lines[j]):
                out.append(lines[i])
                out.append(lines[j])
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _link_first_cross_reference(text: str, target: str) -> str:
    """Turn the first plain-text mention of the other legal document's name
    into a Markdown link to it (issue #182 item 6) — e.g. terms.md's first
    "Privacy Policy" becomes `[Privacy Policy](/privacy)`. First mention
    only: repeating the link on every occurrence is noise, not navigation.

    Truly idempotent: short-circuits as soon as ANY occurrence of the
    already-linked form is present anywhere in the text, not merely at the
    specific spot about to be linked. The real terms.md has 5 plain-text
    "Privacy Policy" mentions; a narrower per-occurrence guard (skip only a
    mention already wrapped right where the match sits) still finds and
    links the next plain mention on a second pass, so f(f(x)) != f(x). The
    whole-text check makes it safe to re-run this against a target already
    published — once the first mention is linked, every later call is a
    no-op. A target with no configured cross-link (or a draft that never
    mentions the other document) passes text through unchanged."""
    target_info = _CROSS_LINK_TARGETS.get(target)
    if target_info is None:
        return text
    phrase, href = target_info
    linked_form = f"[{phrase}]({href})"
    if linked_form in text:
        return text
    pattern = re.compile(r"(?<!\[)" + re.escape(phrase) + r"(?!\]\()")
    return pattern.sub(linked_form, text, count=1)


def _strip_draft_banner(text: str) -> str:
    """Drop the consecutive '>'-prefixed DRAFT banner lines immediately
    preceding the first '---' rule, and that '---' rule itself. The banner
    always starts after the document's own H1 (and a blank line), so the H1
    is untouched.

    A blank line sits between the banner's last '>' line and the '---' rule
    (the blockquote has to end before a thematic break can start), so the
    backward scan first steps over blank lines to find where a blockquote
    block WOULD end, then checks whether '>' lines actually precede that
    point. Only removes the banner block when it finds at least one such
    '>' line — otherwise a lone '---' rule with no banner above it is left
    untouched, and only that rule line itself is dropped."""
    lines = text.split("\n")
    first_rule_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            first_rule_idx = i
            break
    if first_rule_idx is None:
        return text
    banner_end = first_rule_idx
    while banner_end > 0 and lines[banner_end - 1].strip() == "":
        banner_end -= 1
    start = banner_end
    while start > 0 and lines[start - 1].lstrip().startswith(">"):
        start -= 1
    if start == banner_end:
        start = first_rule_idx  # no blockquote lines found — drop only the rule
    del lines[start : first_rule_idx + 1]
    return "\n".join(lines)


def _cut_from_heading_to_eof(text: str, heading_prefix: str) -> str:
    marker = f"\n{heading_prefix}"
    idx = text.find(marker)
    if idx == -1:
        return text
    return text[: idx + 1]


def _strip_trailing_rule_and_blank_lines(text: str) -> str:
    """Drop trailing '---' thematic-break lines and blank lines left behind
    once a comment or an appendix section between them and the document's
    real tail content has been removed. Tolerant of however many blank
    lines a removed comment happened to leave, rather than assuming an
    exact count."""
    lines = text.split("\n")
    while lines and _TRAILING_RULE_OR_BLANK.match(lines[-1]):
        lines.pop()
    return "\n".join(lines)


def _collapse_blank_lines(text: str) -> str:
    """Collapse any run of 3+ consecutive blank lines down to exactly 2."""
    lines = text.split("\n")
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 2:
                out.append("")
        else:
            blank_run = 0
            out.append(line)
    return "\n".join(out)


def build_published_text(raw: str, target: str, effective_date: str) -> str:
    """Apply the full mechanical strip, in order, and return the published
    text (guard-checking is the caller's job, not this function's — kept
    separate so tests can exercise the transform without also asserting the
    guard passes in the same call)."""
    text = _strip_html_comments(raw)
    text = _strip_draft_banner(text)
    if target == "privacy":
        text = _cut_from_heading_to_eof(text, "# Appendix A")
    text = _strip_trailing_rule_and_blank_lines(text)
    text = _EFFECTIVE_DATE_PLACEHOLDER.sub(effective_date, text)
    text = _collapse_blank_lines(text)
    text = _collapse_blank_before_table_delimiter(text)
    text = _link_first_cross_reference(text, target)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source", required=True, type=pathlib.Path, help="path to the ratified draft .md"
    )
    parser.add_argument(
        "--target",
        required=True,
        choices=sorted(_TARGET_FILENAMES),
        help="which published file to write",
    )
    parser.add_argument("--effective-date", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--public-root",
        default=pathlib.Path("."),
        type=pathlib.Path,
        help="repo root containing jobcannon/ (default: cwd)",
    )
    args = parser.parse_args(argv)

    if not _EFFECTIVE_DATE_ARG.fullmatch(args.effective_date):
        print(
            f"import_legal_text: --effective-date must be YYYY-MM-DD, got {args.effective_date!r}",
            file=sys.stderr,
        )
        return 2

    if not args.source.is_file():
        print(f"import_legal_text: --source not found: {args.source}", file=sys.stderr)
        return 2

    raw = args.source.read_text(encoding="utf-8")
    published = build_published_text(raw, args.target, args.effective_date)

    violations = check_published_text(published)
    if violations:
        print(
            f"import_legal_text: refusing to write {args.target}.md — "
            f"{len(violations)} guard violation(s):",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    out_dir = args.public_root / "jobcannon" / "web" / "legal"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _TARGET_FILENAMES[args.target]
    out_path.write_text(published, encoding="utf-8")
    print(
        f"import_legal_text: wrote {out_path} ({len(published)} bytes, effective {args.effective_date})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

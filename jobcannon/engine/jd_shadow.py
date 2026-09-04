# PORTED from job_finder/db/_jd_shadow.py @ 1b9f0940120f8fd469e298b5fb4dbe14cccc60cf (private job-cannon). Ledger L-0493.
"""Shadow content-hash instrumentation for JD invalidation churn (T3.1 PR-A, D6).

MEASURE-ONLY. Nothing in this module changes invalidation behavior. It exists to
prove or refute — on live traffic, before any flip — the SUSPECTED root cause of
defect D6: that ``set_jd_full`` re-scores already-classified jobs because volatile
page chrome ``normalize_jd`` does not strip (view/applicant counts, relative
"posted X days ago" stamps, whitespace) churns the ``text != existing_jd``
comparison on re-fetches.

The comparison point is deliberately the SAME normalized text the live code
compares (``normalize_jd(text)`` — NOT raw fetch bytes); this module strips the
*additional* volatile chrome on top and hashes the result.

PR-B CONTRACT
-------------
``content_shadow_hash`` is the exact function the later flip (PR-B, a separate
gated task) must reuse to decide whether an invalidation is chrome-only. PR-A's
counter is only a valid gate for PR-B if PR-B's comparison is byte-identical, so
PR-B imports this function rather than reimplementing the stripper. The stripper
is intentionally CONSERVATIVE: every pattern it adds makes the hash more stable,
which inflates the suppressible-re-score counter and biases the operator toward
approving PR-B. Case is preserved (a case change in a JD body is a real edit, not
chrome); only unambiguous volatile chrome and whitespace are removed.

The shadow hash and the DB/event side effects are separated so the pure functions
(``strip_volatile_chrome`` / ``content_shadow_hash``) are unit-testable with no
DB or ledger, and the recorder (``record_content_observation``) never raises into
``set_jd_full`` (mirroring run_events' "emission never raises into the caller").
"""
# PORT-SEAM: only the pure S1 layer below is ported (Ledger L-0493, design note
# ports/design-tests-blocked.md Q-1). The DB/event recorder half described in
# the docstring above (record_content_observation, _emit_shadow_event,
# _utc_iso, and the set_jd_full seam that wired it in) is NOT ported here --
# it needs a per-job observation sink, and the sibling hooks-scoring design
# note's Q-D recommends against building one for this port. See this PR's
# "Modularity note" for the deferred-instrumentation tracking.

from __future__ import annotations

import hashlib

# PORT-SEAM: `logging` dropped -- backed only the S3 instrumentation logger
# (record_content_observation's INFO line), which is not ported here.
import re
# PORT-SEAM: `sqlite3` and `from datetime import UTC, datetime` (plus the
# module-level `logger = logging.getLogger(__name__)` they backed) are
# dropped along with the functions at the end of this module that used them
# (`_utc_iso`, `_emit_shadow_event`, `record_content_observation`) -- the S3
# shadow-instrumentation sink, not ported here (see module docstring).

# Volatile page-chrome patterns that churn across re-fetches of the SAME posting.
# Applied case-insensitively. These are NEVER substituted into the kept body —
# they are used ONLY to decide whether a whole LINE is pure chrome (see
# ``_line_is_pure_chrome`` / ``strip_volatile_chrome``). Board chrome appears on
# its own short line ("Posted 3 days ago · Over 200 applicants · 88 views"), so a
# line is dropped only when NOTHING survives removing these fragments; a prose
# sentence that merely contains "...3 years ago..." keeps a large residue and is
# therefore preserved verbatim. This line-anchored design makes mid-sentence
# over-stripping (which would inflate the D6 counter, the confirmation-bias
# direction that gates PR-B) structurally impossible.
_VOLATILE_CHROME_RE: tuple[re.Pattern[str], ...] = (
    # Relative recency stamps: "Posted 3 days ago", "Reposted 2 weeks ago",
    # "Updated 5 hours ago", and the bare "N <unit> ago" form.
    re.compile(
        r"\b(?:posted|reposted|updated)\b[^\n]{0,24}?\bago\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b\d+\+?\s+(?:second|minute|hour|day|week|month|year)s?\s+ago\b",
        re.IGNORECASE,
    ),
    # Applicant counters: "Over 100 applicants", "37 applicants",
    # "Be among the first 25 applicants".
    re.compile(
        r"\bbe among the first\s+\d[\d,]*\+?\s+applicants?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:over\s+)?\d[\d,]*\+?\s+applicants?\b",
        re.IGNORECASE,
    ),
    # View counters: "1,234 views", "Viewed 88 times".
    re.compile(r"\bviewed\s+\d[\d,]*\+?\s+times?\b", re.IGNORECASE),
    re.compile(r"\b\d[\d,]*\+?\s+views?\b", re.IGNORECASE),
)

# Layout separator glyphs (interpunct/bullet/pipe family) used to join chrome
# fragments on a metadata line. Removed ONLY inside the ``_line_is_pure_chrome``
# residue test — never from the kept body — so a prose line like "React | Node"
# is preserved verbatim (its residue is non-empty, so the whole line is kept).
_SEPARATOR_GLYPHS_RE = re.compile(r"[·•|‧∙]+")


def _line_is_pure_chrome(line: str) -> bool:
    """Return True if ``line`` is entirely volatile board chrome.

    A line qualifies only when NOTHING of substance survives removing the
    ``_VOLATILE_CHROME_RE`` fragments, the separator glyphs, and whitespace — i.e.
    it was a pure metadata line ("Posted 3 days ago · Over 200 applicants · 88
    views"). A prose sentence that merely mentions "...3 years ago..." retains a
    large residue and is NOT pure chrome, so it is kept verbatim. This is the sole
    consumer of the chrome patterns; they never mutate kept text.
    """
    residue = line
    for pattern in _VOLATILE_CHROME_RE:
        residue = pattern.sub(" ", residue)
    residue = _SEPARATOR_GLYPHS_RE.sub(" ", residue)
    return not residue.strip()


def strip_volatile_chrome(text: str) -> str:
    """Drop pure-chrome lines, keep prose lines verbatim, collapse whitespace.

    Operates LINE BY LINE. A line is dropped only when ``_line_is_pure_chrome``
    holds (it was entirely a board metadata line — recency stamp, applicant/view
    counters, separators); every other line is kept EXACTLY as written. The kept
    lines are joined with a single space and whitespace-collapsed so re-fetch
    whitespace churn does not change the hash. Case is preserved.

    This line-anchored design is deliberate: matching chrome patterns anywhere in
    the body would excise fragments mid-sentence ("We updated our architecture 3
    years ago to microservices" → "...architecture to microservices"), which would
    make ``shadow_stable`` fire on a real prose diff and inflate the suppressible-
    re-score numerator PR-B gates on. Keeping whole prose lines untouched makes
    that false positive impossible; the worst case is under-counting (a chrome
    fragment glued onto a prose line survives), which is the safe direction for a
    measurement that gates a behavior change.

    Idempotent: ``strip_volatile_chrome(strip_volatile_chrome(x))`` equals
    ``strip_volatile_chrome(x)`` (a kept prose line has non-empty residue on the
    second pass too).
    """
    if not text:
        return ""
    kept = [line for line in text.splitlines() if not _line_is_pure_chrome(line)]
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_shadow_hash(text: str) -> str:
    """Return the SHA-256 hex digest of ``strip_volatile_chrome(text)``.

    This is the canonical shadow-comparison primitive; PR-B reuses it verbatim so
    its chrome-only decision matches the number PR-A measured (see module
    docstring). Computed on TOP of the caller's already-``normalize_jd``-d text.
    """
    return _sha256_hex(strip_volatile_chrome(text))


# PORT-SEAM: `sqlite3`, `from datetime import UTC, datetime`, and the
# module-level `logger` are dropped along with the functions below that used
# them (`_utc_iso`, `_emit_shadow_event`, `record_content_observation`) — the
# S3 shadow-instrumentation sink, not ported here (see module docstring).

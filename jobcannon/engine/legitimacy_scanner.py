# PORTED from job_finder/web/legitimacy_scanner.py @ e95b3e27a020f4dc60eedae4735e073ca0ddd6e4 (private job-cannon). Ledger L-0191.
"""Legitimacy scanner — scam / MLM pattern detection for jd_full text.

Scans the raw JD text for known scam, MLM, and get-rich-quick patterns.
Returns a brief note string on the FIRST match, or ``None`` if clean.

False-positive policy (R-09 from spec §15):
    The scanner flags; ``derive_classification`` rejects; admins clear
    false positives by NULLing ``legitimacy_note`` via ``/admin/review``
    or a direct SQL UPDATE (``UPDATE jobs SET legitimacy_note = NULL
    WHERE dedup_key = '<key>'``).

Conservative pattern set — false negatives preferred over false positives.
Phrase patterns use lowercased substring match; dollar-amount pattern is regex.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

# (needle_lowercased, note_tag) pairs.  Checked in order; FIRST match wins.
_PHRASE_PATTERNS: list[tuple[str, str]] = [
    ("unlimited income potential", "mlm_phrase: unlimited income potential"),
    ("be your own boss", "mlm_phrase: be your own boss"),
    ("work from home opportunity", "mlm_phrase: work from home opportunity"),
    ("join our team of leaders", "mlm_phrase: join our team of leaders"),
    ("financial freedom", "mlm_phrase: financial freedom"),
    ("residual income", "mlm_phrase: residual income"),
    ("recruit your downline", "mlm_phrase: recruit your downline"),
    ("earnings depend on", "mlm_phrase: earnings depend on"),
    ("crypto trading opportunity", "scam_phrase: crypto trading opportunity"),
]

# Regex patterns applied to the original-case string.
# Each is (compiled_pattern, note_tag).
_REGEX_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"earn\s+\$\d{3,}/day", re.IGNORECASE),
        "scam_phrase: high-daily-earnings claim",
    ),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_legitimacy(jd_full: str) -> str | None:
    """Scan *jd_full* for scam / MLM patterns.

    Returns a brief note string (e.g. ``"mlm_phrase: financial freedom"``) on
    the first matched pattern, or ``None`` if the text is clean.

    This is a flag function, not a classifier — it returns the FIRST match
    only.  Down-stream callers (``derive_classification``) treat any truthy
    return value as a rejection signal.

    Args:
        jd_full: The full job-description text.  Empty string or ``None``
            input always returns ``None``.

    Returns:
        A short note string if a scam / MLM pattern was detected, else ``None``.
    """
    if not jd_full:
        return None

    lower = jd_full.lower()

    for phrase, note in _PHRASE_PATTERNS:
        if phrase in lower:
            return note

    for pattern, note in _REGEX_PATTERNS:
        if pattern.search(jd_full):
            return note

    return None

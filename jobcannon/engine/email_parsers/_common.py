# PORTED from job_finder/parsers/_common.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Shared utilities for job alert email parsers.

Centralises the meta-email detection logic that was previously duplicated
across linkedin_parser, glassdoor_parser, and ziprecruiter_parser.

Also provides shared salary parsing (parse_salary_range) used by all four
parsers, eliminating near-identical regex + K-notation conversion code.

Note: indeed_parser uses is_meta_email with _INDEED_META_PATTERNS as extra_patterns
(must NOT filter on "N new jobs" lines, which are real alerts for Indeed).
"""

import re

# ---------------------------------------------------------------------------
# Placeholder detection
# ---------------------------------------------------------------------------

# Known HTML template artifact strings that appear when email rendering engines
# substitute placeholder text instead of real job data.  These values must never
# become job titles or company names.  Lifted here from ziprecruiter_parser so
# both that parser and _positional_fallback share a single canonical copy.
_PLACEHOLDER_STRINGS: frozenset[str] = frozenset(
    {
        "title",
        "body",
        "unknown",
        "name",
        "company",
        "location",
        "n/a",
        "none",
    }
)

# ---------------------------------------------------------------------------
# Salary parsing
# ---------------------------------------------------------------------------

# Salary range: "$120K - $150K", "$120,000 - $150,000", "$168K-$255K"
# Handles K-notation, comma-separated full-dollar amounts, and en-dash.
SALARY_RANGE_RE = re.compile(r"\$(\d[\d,]*)\s*[Kk]?\s*[-\u2013]+\s*\$(\d[\d,]*)\s*[Kk]?")


def parse_salary_range(text: str) -> tuple[int | None, int | None]:
    """Extract a salary range from free-form email text.

    P1.4 (D-2): delegates to ``salary_normalizer.parse_salary_text`` (single
    parser) + ``normalize_observation`` (single normalizer) with provenance
    ``email_snippet`` instead of the bespoke regex + K-notation math this
    replaces (plan §1.2 item 5). The normalizer now applies the plausibility
    floor/ceiling and the salvage ladder (D-3), so sub-floor / cross-unit junk
    that the old unbounded parser would have stapled into the row now returns
    ``(None, None)``; period cues ("an hour") are honored and annualized.

    Handles formats like:
        $168K-$255K / year salary
        $150,000 - $200,000
        $120K - $150K (Employer est.)

    Returns:
        (salary_min, salary_max) as annualized-USD ints, or (None, None) when no
        range is found or the value is implausible.
    """
    from jobcannon.engine.salary_normalizer import (
        RESOLVED_RESOLUTIONS,
        normalize_observation,
        parse_salary_text,
    )

    obs = parse_salary_text(text, provenance="email_snippet")
    if obs is None:
        return None, None
    normalized = normalize_observation(obs)
    if normalized.resolution not in RESOLVED_RESOLUTIONS:
        return None, None
    return normalized.salary_min, normalized.salary_max


def looks_like_salary_range(text: str) -> bool:
    """Return True if *text* contains a salary range pattern ($X - $Y)."""
    return bool(SALARY_RANGE_RE.search(text))


def looks_like_salary_text(text: str) -> bool:
    """Return True if *text* contains any dollar amount (e.g. '$120K')."""
    return bool(re.search(r"\$\d+", text))


# ---------------------------------------------------------------------------
# Meta-email detection
# ---------------------------------------------------------------------------

# Base meta-email patterns checked against the first 200 characters of the
# email body.  Checking only the preamble avoids false positives where job
# titles contain phrases like "30+ new" (per Research Pitfall 4).
BASE_META_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\d+\+?\s+new\s+jobs?\s+match", re.IGNORECASE | re.MULTILINE),
    re.compile(r"job alert digest|weekly digest", re.IGNORECASE),
    re.compile(r"you have \d+ new jobs?", re.IGNORECASE),
    re.compile(r"^\d+ jobs? found", re.IGNORECASE | re.MULTILINE),
]


def is_meta_email(body: str, extra_patterns: list[re.Pattern[str]] | None = None) -> bool:
    """Return True if the email preamble matches known meta-email patterns.

    Only inspects the first 200 characters of the body to avoid false positives
    from job titles or descriptions that contain pattern-matching words.

    Args:
        body: Email body text.
        extra_patterns: Additional compiled regex patterns to check beyond the
            base set.  Used by parsers that need source-specific patterns
            (e.g. LinkedIn's "you'll receive notifications" check).

    Returns:
        True if the body looks like a digest/count summary, not a job alert.
    """
    preamble = body[:200]
    # fmt: off
    # PORT-SEAM: fmt:off preserves the private wrap (line-length 99); public's
    # line-length 100 would otherwise collapse this to one line, and the
    # collapse itself (not the rewrite) is what the fidelity diff can't
    # classify. Content is unchanged.
    patterns = (
        BASE_META_PATTERNS if extra_patterns is None else BASE_META_PATTERNS + extra_patterns
    )
    # fmt: on  # PORT-SEAM: end fmt:off block
    return any(pattern.search(preamble) for pattern in patterns)

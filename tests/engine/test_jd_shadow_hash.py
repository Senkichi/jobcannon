# PORTED from tests/test_jd_shadow_hash.py @ 1b9f0940120f8fd469e298b5fb4dbe14cccc60cf (private job-cannon). Ledger L-0493.
# PORT-SEAM: only the private module's FIRST test section (pure stripper/hash)
# is ported below. Its SECOND section -- the instrumentation wired into
# set_jd_full (test_chrome_only_refetch_flags_shadow_stable_and_still_invalidates,
# test_genuine_change_invalidates_without_shadow_stable,
# test_hash_recorded_and_idempotent_rewrite_does_not_increment,
# test_record_observation_never_raises_on_missing_table, plus their
# _insert_scored_job/_latest_history_row/_classification helpers and the `db`
# sqlite3 fixture) is NOT ported (Ledger L-0493, design note
# ports/design-tests-blocked.md Q-1). jobcannon.engine.jd_shadow does not
# carry the S3 recorder (record_content_observation/_emit_shadow_event) those
# tests exercise -- see that module's trailing PORT-SEAM comment. Deferred,
# tracked in this PR's "Modularity note", tied to the sibling hooks-scoring
# note's Q-D (per-job event sink) ruling.
"""Tests for the pure shadow-hash layer (T3.1 PR-A, measure-only, S1) --
the private module's instrumentation-layer section is not ported here.

# PORT-SEAM: the private module's "Two layers" framing and its section-2
# description (the set_jd_full instrumentation, points (a)/(b)/(c)) are
# dropped from this docstring; only the pure-layer paragraph below survives.
Exercises the pure stripper/hash (``strip_volatile_chrome`` /
``content_shadow_hash``): volatile page chrome and whitespace get stripped,
case is preserved, the stripper is idempotent, and the hash stays stable
across a chrome-only diff while changing on a genuine content edit.
"""
# PORT-SEAM: `os`/`sqlite3`/`collections.abc.Iterator` imports are dropped --
# they backed only the S3 instrumentation-layer tests (sqlite3 `db` fixture,
# `_insert_scored_job`), which are not ported here.

import pytest

# PORT-SEAM: `set_jd_full` and `record_content_observation` have no import
# here -- both belong to the S3 instrumentation layer this file does not
# port (see module docstring above). The private `os.environ.setdefault(
# "GSD_BACKUP_CONFIRMED", "1")` CI/backup-safety gate is also dropped as
# not applicable to the public repo.
from jobcannon.engine.jd_shadow import content_shadow_hash, strip_volatile_chrome

# A clean, >=200-char, non-junk, HTML-signal-free JD body.
_BASE_JD = (
    "We are seeking a Senior Backend Engineer to design and operate distributed "
    "systems at scale. You will own services end to end, mentor engineers, and "
    "partner with product to ship high-impact features. Requirements: 6+ years of "
    "Python, strong system design, and a track record of operational excellence."
)

# Same posting on a re-fetch: identical body PLUS volatile board chrome and
# extra whitespace that normalize_jd does NOT strip. No HTML signal, so
# normalize_jd passes it through unchanged and the raw invalidation must fire.
_BASE_JD_WITH_CHROME = (
    _BASE_JD + "\n\n   Posted 3 days ago   ·   Over 200 applicants   ·   1,234 views\n\n"
)

# A genuinely different posting (different role/requirements).
_OTHER_JD = (
    "Join our platform team as a Staff Software Engineer. You will lead the "
    "architecture for our data ingestion pipeline, drive reliability initiatives, "
    "and coach senior engineers. Requirements: 8+ years building back-end systems, "
    "deep SQL knowledge, and hands-on experience with event streaming platforms."
)


# --------------------------------------------------------------------------- #
# Pure stripper / hash
# --------------------------------------------------------------------------- #


def test_strip_removes_chrome_and_collapses_whitespace():
    """Chrome fragments vanish and whitespace collapses; base content survives."""
    stripped = strip_volatile_chrome(_BASE_JD_WITH_CHROME)
    assert stripped == strip_volatile_chrome(_BASE_JD)
    assert "applicants" not in stripped.lower()
    assert "views" not in stripped.lower()
    assert "ago" not in stripped.lower()
    assert "  " not in stripped  # no double spaces after collapse


def test_strip_is_idempotent():
    once = strip_volatile_chrome(_BASE_JD_WITH_CHROME)
    assert strip_volatile_chrome(once) == once


def test_strip_preserves_case():
    """A case change is a real edit, not chrome — the stripper must not fold it."""
    assert strip_volatile_chrome("Senior Engineer") != strip_volatile_chrome("senior engineer")


def test_hash_stable_across_chrome_only_diff():
    assert content_shadow_hash(_BASE_JD) == content_shadow_hash(_BASE_JD_WITH_CHROME)


def test_hash_changes_on_genuine_content_edit():
    assert content_shadow_hash(_BASE_JD) != content_shadow_hash(_OTHER_JD)


def test_hash_is_sha256_hex():
    h = content_shadow_hash(_BASE_JD)
    assert len(h) == 64
    int(h, 16)  # raises if not hex


# Legitimate JD prose that contains chrome-shaped substrings mid-sentence. The
# line-anchored stripper MUST pass these through unchanged — matching the chrome
# patterns mid-sentence (the original defect) would excise real content and make
# shadow_stable fire on a genuine prose diff, inflating the D6 numerator PR-B
# gates on. Negative test cases from the adversarial review of PR #1793.
_PROSE_WITH_CHROME_SHAPED_SUBSTRINGS = [
    "We updated our architecture 3 years ago to microservices.",
    "Our team posted record revenue a year ago.",
    "The product reached 10,000 views on launch day.",
    "We serve over 500 applicants through our platform monthly.",
]


@pytest.mark.parametrize("sentence", _PROSE_WITH_CHROME_SHAPED_SUBSTRINGS)
def test_prose_with_chrome_shaped_substrings_survives_unchanged(sentence):
    """A single prose line is never mutilated mid-sentence — kept verbatim."""
    assert strip_volatile_chrome(sentence) == sentence


def test_standalone_chrome_line_is_dropped_but_prose_line_kept():
    """A pure metadata line is dropped; an adjacent prose line survives intact."""
    doc = (
        "Senior Backend Engineer at TestCo. You will own services end to end.\n"
        "Posted 3 days ago · Over 200 applicants · 1,234 views"
    )
    stripped = strip_volatile_chrome(doc)
    assert stripped == "Senior Backend Engineer at TestCo. You will own services end to end."


# PORT-SEAM: the private module's "Instrumentation wired into set_jd_full"
# section ends here -- its 4 tests (chrome-only-refetch-flags-shadow-stable,
# genuine-change-invalidates-without-shadow-stable,
# hash-recorded-and-idempotent-rewrite, record-observation-never-raises) plus
# the `_insert_scored_job`/`_latest_history_row`/`_classification` helpers and
# the sqlite3 `db` fixture are NOT ported (Ledger L-0493 Q-1, S3 deferred).

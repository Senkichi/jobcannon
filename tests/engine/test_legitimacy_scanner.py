"""Tests for jobcannon.engine.legitimacy_scanner (Phase 49.07).

Covers:
1. Unit: scan_legitimacy returns a non-None note for each scam/MLM pattern.
2. Unit: scan_legitimacy returns None for a clean JD.
3. Unit: scan_legitimacy returns None for empty/None input.
4. Unit: regex pattern fires for high-daily-earnings claim.

PORT NOTE (Ledger L-0191): the private suite's E2E class (run_scoring on a
flagged/clean JD verifying legitimacy_note + classification end to end) is
dropped here — it drives jobcannon.engine.scoring_runner, which is not yet
ported (Ledger L-0263, deferred: needs a host DB-write seam). Re-add once
scoring_runner lands.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.legitimacy_scanner import scan_legitimacy

# ---------------------------------------------------------------------------
# Unit tests — scan_legitimacy
# ---------------------------------------------------------------------------


class TestScanLegitimacyPhrases:
    """Each phrase pattern fires correctly."""

    @pytest.mark.parametrize(
        "jd_text,expected_prefix",
        [
            ("This role offers unlimited income potential for driven people.", "mlm_phrase"),
            ("Be your own boss and set your own schedule.", "mlm_phrase"),
            ("This is a work from home opportunity with flexible hours.", "mlm_phrase"),
            ("Join our team of leaders in the fastest-growing network.", "mlm_phrase"),
            ("Achieve financial freedom through our proven system.", "mlm_phrase"),
            ("Earn residual income that keeps paying month after month.", "mlm_phrase"),
            ("You will recruit your downline and coach them to success.", "mlm_phrase"),
            ("Your earnings depend on the effort you put in.", "mlm_phrase"),
            ("This is a crypto trading opportunity with daily returns.", "scam_phrase"),
        ],
    )
    def test_phrase_returns_note(self, jd_text: str, expected_prefix: str) -> None:
        note = scan_legitimacy(jd_text)
        assert note is not None, f"Expected a note for: {jd_text!r}"
        assert note.startswith(expected_prefix), (
            f"Expected note starting with {expected_prefix!r}, got {note!r}"
        )

    def test_regex_high_daily_earnings(self) -> None:
        """earn $500/day regex fires."""
        note = scan_legitimacy("You can earn $500/day working from home.")
        assert note is not None
        assert note.startswith("scam_phrase")

    def test_regex_high_daily_earnings_no_match_below_threshold(self) -> None:
        """earn $99/day does NOT fire (threshold is 3+ digits)."""
        note = scan_legitimacy("Earn $99/day as a side hustle.")
        assert note is None

    def test_case_insensitive_phrase(self) -> None:
        """Phrase match is case-insensitive."""
        note = scan_legitimacy("UNLIMITED INCOME POTENTIAL awaits you!")
        assert note is not None
        assert "unlimited income potential" in note

    def test_first_match_wins(self) -> None:
        """When multiple patterns match, only the first is returned."""
        jd = "unlimited income potential and financial freedom await"
        note = scan_legitimacy(jd)
        # First phrase in the table is 'unlimited income potential'
        assert note is not None
        assert "unlimited income potential" in note


class TestScanLegitimacyClean:
    """Clean JD → None."""

    def test_clean_jd_returns_none(self) -> None:
        jd = (
            "We are looking for a Senior Data Engineer to join our platform team. "
            "You will design and build scalable data pipelines, work closely with "
            "product and analytics teams, and mentor junior engineers. "
            "Requirements: 5+ years Python, strong SQL, cloud (AWS/GCP), "
            "experience with dbt or similar. Competitive salary + equity."
        )
        assert scan_legitimacy(jd) is None

    def test_empty_string_returns_none(self) -> None:
        assert scan_legitimacy("") is None

    def test_none_like_empty_returns_none(self) -> None:
        # The function signature accepts str; test with empty to cover the guard.
        assert scan_legitimacy("   ") is None

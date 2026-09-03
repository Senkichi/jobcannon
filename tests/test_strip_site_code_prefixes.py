"""Precision tests for strip_site_code_prefix (Issue #1046).

strip_site_code_prefix is NOT wired into normalize_company (see
job_finder/normalizers.py) — it's a standalone helper used only by
scripts/strip_site_code_prefixes.py. These tests pin the regex's precision:
it must strip the 4 confirmed site-code rows and the 2 borderline rows must
NOT be auto-stripped (they lack the leading-zero / letter+3-digit signal and
are reserved for owner review), while a broad set of legitimate numbered
brands — including ones sharing the "digit(s) + space + word" shape — must
never be touched.

Loosening _SITE_CODE_PREFIX_RE to catch the borderline cases would also catch
some of the legitimate-brand cases below; this file is the tripwire for that
tradeoff, so a regex change that breaks any of these assertions must be a
deliberate, reviewed decision — not an accidental regression.
"""

from jobcannon.engine.normalizers import strip_site_code_prefix


class TestStripSiteCodePrefixConfirmedCases:
    """The 4 confirmed site-code rows from the issue must strip correctly."""

    def test_leading_zero_four_digit(self):
        assert (
            strip_site_code_prefix("0006 MA01-CAMBRIDGE-CROSSING-US4E")
            == "MA01-CAMBRIDGE-CROSSING-US4E"
        )

    def test_leading_zero_bank_name(self):
        assert (
            strip_site_code_prefix("0101 The Huntington National Bank")
            == "The Huntington National Bank"
        )

    def test_letter_plus_four_digits(self):
        assert strip_site_code_prefix("C4000 Stewart Title Company") == "Stewart Title Company"

    def test_leading_zero_five_digit(self):
        assert (
            strip_site_code_prefix(
                "09516 Banco Nacional de Mexico, S.A., integrante del Grupo Financiero Banamex"
            )
            == "Banco Nacional de Mexico, S.A., integrante del Grupo Financiero Banamex"
        )


class TestStripSiteCodePrefixBorderlineCasesNotAutoStripped:
    """The 2 borderline rows lack the leading-zero / letter+3-digit signal.

    Per the issue, these require owner review rather than automatic
    stripping. The derived regex correctly declines to touch them: "3010"
    and "410" are plain digit tokens with no leading zero, structurally
    indistinguishable from a legitimate numbered brand like "2020 Companies".
    """

    def test_no_leading_zero_not_stripped(self):
        assert (
            strip_site_code_prefix("3010 HYDRIL USA DISTRIBUTION") == "3010 HYDRIL USA DISTRIBUTION"
        )

    def test_short_digit_token_not_stripped(self):
        assert strip_site_code_prefix("410 ICR United States USA") == "410 ICR United States USA"


class TestStripSiteCodePrefixLegitimateNumberedBrandsPreserved:
    """Legitimate numbered brands must never be stripped — no allowlist needed.

    Precision comes from the regex shape itself: these names either don't
    start with a leading-zero digit, or (for the single-letter case) don't
    have 3+ trailing digits.
    """

    def test_issue_named_brands(self):
        assert strip_site_code_prefix("2020 Companies") == "2020 Companies"
        assert strip_site_code_prefix("1872 Consulting") == "1872 Consulting"
        assert strip_site_code_prefix("21 Tech") == "21 Tech"
        assert strip_site_code_prefix("A10 Networks") == "A10 Networks"

    def test_additional_numbered_brands(self):
        """Brands sharing the "digit(s) + space + word(s)" shape as the
        confirmed site-code rows, but without the leading-zero/letter+3-digit
        signal — the exact overbroad-regex failure mode this rework fixes."""
        assert strip_site_code_prefix("3M Company") == "3M Company"
        assert strip_site_code_prefix("84 Lumber") == "84 Lumber"
        assert strip_site_code_prefix("1st Financial Bank USA") == "1st Financial Bank USA"
        assert strip_site_code_prefix("99 Cents Only Stores") == "99 Cents Only Stores"


class TestStripSiteCodePrefixMisc:
    def test_non_matching_names_unchanged(self):
        assert strip_site_code_prefix("Google") == "Google"
        assert strip_site_code_prefix("Acme Corporation") == "Acme Corporation"

    def test_whitespace_handling(self):
        assert strip_site_code_prefix("0006  Company Name") == "Company Name"
        assert strip_site_code_prefix("C4000  Company Name") == "Company Name"

    def test_empty_input(self):
        assert strip_site_code_prefix("") == ""
        assert strip_site_code_prefix(None) is None

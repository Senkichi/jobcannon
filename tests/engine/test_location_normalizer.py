"""Unit tests for jobcannon.engine.location_normalizer.

Pure-function module — no fixtures needed.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.location_normalizer import (
    normalize_for_display,
    normalize_location,
    split_multi_locations,
)


class TestNormalizeLocation:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Remote", "Remote"),
            ("  Remote  ", "Remote"),
            ("Remote\t\n", "Remote"),
            ("San  Francisco,  CA", "San Francisco, CA"),  # collapse runs
            ("- Remote -", "Remote"),  # strip surrounding dashes
            (".San Francisco, CA;", "San Francisco, CA"),  # strip surrounding punct
            ("New York, NY", "New York, NY"),  # state code preserved
        ],
    )
    def test_canonicalizes_whitespace_and_punct(self, raw, expected):
        assert normalize_location(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", None, "  ", "\t\n  \t", "-", "--"],
    )
    def test_empty_and_pure_punct_returns_none(self, raw):
        assert normalize_location(raw) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "Unknown",
            "unknown",
            "UNKNOWN",
            "N/A",
            "n/a",
            "TBD",
            "TBA",
            "Various",
            "varies",
            "Multiple Locations",
            "multiple locations",
            "See Job Description",
            "see jd",
            "Not Specified",
            "None",
        ],
    )
    def test_placeholder_values_returned_as_none(self, raw):
        assert normalize_location(raw) is None

    @pytest.mark.parametrize(
        "raw",
        # Conservative — these LOOK placeholder-ish but ARE meaningful filter
        # targets when a user is hunting for fully-remote jobs.
        ["Anywhere", "Worldwide", "Global", "US", "USA", "Remote - US"],
    )
    def test_meaningful_vague_values_are_preserved(self, raw):
        assert normalize_location(raw) is not None

    def test_does_not_aggressively_title_case(self):
        """Aggressive title-casing mangles state codes ("CA" -> "Ca"). The
        normalizer must preserve parser-emitted casing as-is."""
        assert normalize_location("san francisco, CA") == "san francisco, CA"
        assert normalize_location("REMOTE") == "REMOTE"


class TestSplitMultiLocations:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Remote | NYC | SF", ["Remote", "NYC", "SF"]),
            ("Remote;NYC;SF", ["Remote", "NYC", "SF"]),
            ("Remote / NYC / SF", ["Remote", "NYC", "SF"]),
            ("Remote & NYC & SF", ["Remote", "NYC", "SF"]),
            ("Remote or NYC", ["Remote", "NYC"]),
        ],
    )
    def test_splits_on_unambiguous_separators(self, raw, expected):
        assert split_multi_locations(raw) == expected

    def test_does_not_split_on_plain_comma(self):
        """City/State pairs use ',' — splitting would mangle them.
        'San Francisco, CA' must stay one entry."""
        assert split_multi_locations("San Francisco, CA") == ["San Francisco, CA"]

    def test_deduplicates_case_insensitively(self):
        assert split_multi_locations("Remote | REMOTE | remote") == ["Remote"]

    def test_drops_placeholders_inside_split(self):
        assert split_multi_locations("Remote | Unknown | NYC") == ["Remote", "NYC"]

    def test_pure_placeholder_returns_empty(self):
        assert split_multi_locations("Unknown") == []
        assert split_multi_locations("") == []
        assert split_multi_locations(None) == []

    def test_single_location_passes_through(self):
        assert split_multi_locations("Remote") == ["Remote"]
        assert split_multi_locations("San Francisco, CA") == ["San Francisco, CA"]

    def test_idempotent(self):
        """Applying twice produces the same result — required for m060 to be
        safe to re-run."""
        once = split_multi_locations("Remote | UNKNOWN | nyc | REMOTE")
        twice = []
        for entry in once:
            twice.extend(split_multi_locations(entry))
        assert once == twice


class TestNormalizeForDisplay:
    """Display-side normalizer: collapses the many San Jose variants the
    user reported (annotations / ZIP / country / ALLCAPS / state name)
    into a single canonical dropdown entry."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("San Jose, CA", "San Jose, CA"),
            # Strip "(+N other)" / "(+N others)" annotation
            ("San Jose, CA (+1 other)", "San Jose, CA"),
            ("San Jose, CA (+2 others)", "San Jose, CA"),
            # Strip ZIP codes
            ("San Jose, CA 95131", "San Jose, CA"),
            ("San Jose, CA 95131-1234", "San Jose, CA"),
            # Strip trailing US country variants
            ("San Jose, CA, United States", "San Jose, CA"),
            ("San Jose, CA, USA", "San Jose, CA"),
            ("San Jose, CA, US", "San Jose, CA"),
            ("San Jose, CA, us", "San Jose, CA"),
            # Multi-issue stack
            ("San Jose, CA, United States (+1 other)", "San Jose, CA"),
            # ALLCAPS fold + state name -> code
            ("SAN JOSE, CALIFORNIA", "San Jose, CA"),
            # State name -> code (preserves mixed case)
            ("San Jose, California", "San Jose, CA"),
            ("San Jose, California, United States", "San Jose, CA"),
            ("San Francisco, California, USA", "San Francisco, CA"),
            # Multi-word state names
            ("Albany, New York", "Albany, NY"),
            ("Raleigh, North Carolina", "Raleigh, NC"),
        ],
    )
    def test_collapses_variants(self, raw, expected):
        assert normalize_for_display(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "Remote",
            "Anywhere",
            "Worldwide",
            "London, UK",
            "Costa Rica",
            "Toronto, Ontario",
            "Berlin",
            # Non-US country segments at the end stay (only US is stripped).
            "San Jose, Costa Rica",
        ],
    )
    def test_non_us_strings_unchanged(self, raw):
        assert normalize_for_display(raw) == raw

    def test_state_code_preserved_no_mangle(self):
        """The notorious .title() bug — 'San Francisco, CA' must NOT
        become 'San Francisco, Ca'. The function only folds case when the
        whole input is ALLCAPS."""
        assert normalize_for_display("San Francisco, CA") == "San Francisco, CA"

    @pytest.mark.parametrize(
        "raw",
        [
            "NYC",
            "SF",
            "LA",
            "USA",
            "UK",
        ],
    )
    def test_single_token_allcaps_abbreviations_preserved(self, raw):
        """ALLCAPS abbreviations without commas (NYC, SF, USA, UK)
        must NOT be title-cased — they should appear in the dropdown
        exactly as the parser captured them. Only multi-segment ALLCAPS
        (which always contain a comma) get folded."""
        assert normalize_for_display(raw) == raw

    def test_empty_returns_none(self):
        assert normalize_for_display("") is None
        assert normalize_for_display(None) is None

    def test_zip_only_collapses_to_none(self):
        """Bare ZIPs with nothing else are useless as a location filter."""
        assert normalize_for_display("95131") is None

    def test_idempotent(self):
        """Applying twice produces the same result."""
        once = normalize_for_display("SAN JOSE, CALIFORNIA, USA (+1 other) 95131")
        twice = normalize_for_display(once)
        assert once == twice == "San Jose, CA"

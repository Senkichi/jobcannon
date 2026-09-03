"""Unit tests for the pre-Haiku exclusion filter module.

Tests should_exclude() for title keyword exclusions, company exclusions,
salary floor checks with 15% tolerance, case-insensitivity, and empty configs.
"""

import pytest

from jobcannon.engine.exclusion_filter import should_exclude


class TestExclusionFilter:
    """Tests for should_exclude() pure string-matching function."""

    # --- Title keyword exclusions ---

    def test_excludes_title_with_matching_keyword(self):
        """Title containing an excluded keyword returns (True, reason)."""
        job = {"title": "Data Science Intern", "company": "Acme", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": ["intern"], "companies": []}
        )
        assert excluded is True
        assert "intern" in detailed_text.lower()

    def test_excludes_junior_title(self):
        """Title containing 'junior' keyword is excluded."""
        job = {"title": "Junior Data Analyst", "company": "Acme", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": ["junior", "intern"], "companies": []}
        )
        assert excluded is True
        assert "junior" in detailed_text.lower()

    def test_no_exclusion_when_title_clean(self):
        """Title not matching any exclusion keyword returns (False, '')."""
        job = {"title": "Senior Data Scientist", "company": "Acme", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": ["intern", "junior"], "companies": []}
        )
        assert excluded is False
        assert detailed_text == ""

    def test_title_keyword_matching_is_case_insensitive(self):
        """INTERN in title matches 'intern' in keywords (case-insensitive)."""
        job = {"title": "DATA SCIENCE INTERN", "company": "Acme", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": ["intern"], "companies": []}
        )
        assert excluded is True

    def test_title_keyword_partial_match(self):
        """Keyword match is substring-based: 'intern' matches 'internship'."""
        job = {"title": "Data Science Internship", "company": "Acme", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": ["intern"], "companies": []}
        )
        assert excluded is True

    # --- Company exclusions ---

    def test_excludes_matching_company(self):
        """Company exactly matching an excluded company returns (True, reason)."""
        job = {"title": "Data Scientist", "company": "Mercor", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": ["Mercor"]}
        )
        assert excluded is True
        assert "Mercor" in detailed_text

    def test_company_matching_is_case_insensitive(self):
        """'mercor' in job matches 'Mercor' in exclusion list."""
        job = {"title": "Data Scientist", "company": "mercor", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": ["Mercor"]}
        )
        assert excluded is True

    def test_no_exclusion_when_company_clean(self):
        """Company not in exclusion list returns (False, '')."""
        job = {"title": "Data Scientist", "company": "Google", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": ["Mercor", "Staffmark"]}
        )
        assert excluded is False
        assert detailed_text == ""

    # --- Salary floor exclusions ---

    @pytest.mark.parametrize(
        "salary_max,min_salary,should_be_excluded",
        [
            # Well below floor (85k < 150k * 0.85 = 127.5k) -> excluded
            (85_000, 150_000, True),
            # Exactly at 85% floor (127_500 == 150k * 0.85) -> NOT excluded (equal is ok)
            (127_500, 150_000, False),
            # Slightly above 85% floor -> NOT excluded
            (130_000, 150_000, False),
            # At the full min_salary -> NOT excluded
            (150_000, 150_000, False),
            # Above min_salary -> NOT excluded
            (200_000, 150_000, False),
        ],
    )
    def test_salary_floor_tolerance(self, salary_max, min_salary, should_be_excluded):
        """Salary max below min_salary * 0.85 triggers exclusion; at/above floor does not."""
        job = {"title": "Data Scientist", "company": "Acme", "salary_max": salary_max}
        excluded, _, _ = should_exclude(
            job, {"title_keywords": [], "companies": []}, min_salary=min_salary
        )
        assert excluded is should_be_excluded

    def test_undisclosed_salary_not_excluded(self):
        """salary_max=None means salary is not disclosed — must NOT be excluded."""
        job = {"title": "Data Scientist", "company": "Acme", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}, min_salary=150_000
        )
        assert excluded is False
        assert detailed_text == ""

    def test_salary_exclusion_reason_mentions_amounts(self):
        """Salary exclusion reason must mention the actual salary_max value."""
        job = {"title": "Data Scientist", "company": "Acme", "salary_max": 85_000}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}, min_salary=150_000
        )
        assert excluded is True
        assert "85,000" in detailed_text or "85000" in detailed_text

    # --- Empty / missing exclusions ---

    def test_empty_exclusions_no_salary_returns_false(self):
        """Empty exclusions dict with no min_salary returns (False, '') for non-denylist job."""
        job = {"title": "Junior Intern at Acme", "company": "Acme Corp", "salary_max": 50_000}
        excluded, rule_tag, detailed_text = should_exclude(job, {})
        assert excluded is False
        assert detailed_text == ""

    def test_empty_exclusions_with_good_salary_returns_false(self):
        """Empty exclusions dict with salary above floor returns (False, '')."""
        job = {"title": "Junior Intern at Acme", "company": "Acme Corp", "salary_max": 160_000}
        excluded, rule_tag, detailed_text = should_exclude(job, {}, min_salary=150_000)
        assert excluded is False
        assert detailed_text == ""

    def test_no_min_salary_skips_salary_check(self):
        """min_salary=None means salary check is skipped entirely."""
        job = {"title": "Data Scientist", "company": "Acme", "salary_max": 50_000}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}, min_salary=None
        )
        assert excluded is False
        assert detailed_text == ""

    def test_company_with_leading_trailing_whitespace(self):
        """Company matching strips whitespace for comparison."""
        job = {"title": "Data Scientist", "company": "  Mercor  ", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": ["Mercor"]}
        )
        assert excluded is True

    def test_first_match_wins_title_before_company(self):
        """Title keyword match is checked before company — first match wins."""
        job = {"title": "Junior Developer", "company": "Mercor", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": ["junior"], "companies": ["Mercor"]}
        )
        assert excluded is True
        # Reason should mention the keyword, not the company (first match)
        assert "junior" in detailed_text.lower()

    # --- COMPANY_DENYLIST integration (hardcoded denylist, zero config required) ---

    def test_denylist_company_excluded_without_config(self):
        """Job with company='Mercor' is excluded even when exclusions.companies is empty."""
        job = {"title": "Data Scientist", "company": "Mercor", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}
        )
        assert excluded is True
        assert "Mercor" in detailed_text

    def test_denylist_company_case_insensitive(self):
        """'JOBGETHER' matches denylist entry 'jobgether' case-insensitively."""
        job = {"title": "Data Scientist", "company": "JOBGETHER", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}
        )
        assert excluded is True

    def test_config_exclusion_still_works_alongside_denylist(self):
        """Company in exclusions.companies (but not denylist) is still excluded."""
        job = {"title": "Data Scientist", "company": "Staffmark", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": ["Staffmark"]}
        )
        assert excluded is True
        assert "Staffmark" in detailed_text

    # --- Config-driven denylist (filters.company_denylist in config.yaml) ---

    def test_config_denylist_extends_hardcoded(self):
        """Company in config.yaml filters.company_denylist is excluded."""
        config = {"filters": {"company_denylist": ["custom-spam-corp"]}}
        job = {"title": "Data Scientist", "company": "Custom-Spam-Corp", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}, config=config
        )
        assert excluded is True
        assert "Custom-Spam-Corp" in detailed_text

    def test_hardcoded_denylist_always_active_with_config(self):
        """Hardcoded denylist entries still excluded when config denylist is also provided."""
        config = {"filters": {"company_denylist": ["some-other-corp"]}}
        job = {"title": "Data Scientist", "company": "Mercor", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}, config=config
        )
        assert excluded is True

    # --- #213: aggregator/re-poster seeds + legal-entity-suffix parity ---

    @pytest.mark.parametrize(
        "company",
        [
            "Virtual Vocations",
            "Virtual Vocations Inc",  # the suffixed form 102/103 rows actually store
            "VIRTUAL VOCATIONS INC.",
            "ProSidian Consulting, LLC",
            "SynergisticIT",
            "Synergistic it",
            "Role, Inc.",  # role.com aggregator, discovered 2026-07-11
        ],
    )
    def test_seeded_aggregator_excluded(self, company):
        """#213: seeded aggregators/re-posters are excluded regardless of legal-entity
        suffix or case — the bug was the exact-lowercase compare missing 'Inc' variants."""
        job = {"title": "Senior Data Scientist", "company": company, "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}
        )
        assert excluded is True
        assert "Excluded company" in detailed_text

    def test_suffix_variant_config_entry_matches_stored_suffix(self):
        """A config denylist entry without a suffix matches a stored brand WITH one.

        This is the load-bearing #213 fix: previously 'virtual vocations' in the
        denylist would NOT fire on a stored 'Virtual Vocations Inc' row.
        """
        config = {"filters": {"company_denylist": ["Globex"]}}
        job = {"title": "Data Scientist", "company": "Globex, Inc.", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}, config=config
        )
        assert excluded is True

    def test_user_exclusion_company_suffix_variant_matches(self):
        """exclusions.companies is also normalized: 'Initech' excludes 'Initech LLC'."""
        job = {"title": "Data Scientist", "company": "Initech LLC", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": ["Initech"]}
        )
        assert excluded is True

    def test_legit_company_sharing_a_token_not_excluded(self):
        """A real employer that merely shares a word with a seed is NOT excluded."""
        job = {"title": "Data Scientist", "company": "Vocations Academy", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}
        )
        assert excluded is False
        assert detailed_text == ""

    def test_empty_company_not_excluded_by_normalization(self):
        """A blank/whitespace company must not match an empty normalized denylist entry."""
        job = {"title": "Data Scientist", "company": "   ", "salary_max": None}
        excluded, rule_tag, detailed_text = should_exclude(
            job, {"title_keywords": [], "companies": []}
        )
        assert excluded is False


class TestDenylistConfigRoundTrip:
    """get_company_denylist normalizes both the seed and config entries (#213)."""

    def test_seed_contains_normalized_aggregators(self):
        from jobcannon.engine.exclusion_filter import COMPANY_DENYLIST

        for name in (
            "virtual vocations",
            "prosidian consulting",
            "synergisticit",
            "synergistic it",
            "role",
        ):
            assert name in COMPANY_DENYLIST

    def test_seed_entries_are_normalized_form(self):
        """Every seed entry equals its own normalize_company output (no raw suffixes)."""
        from jobcannon.engine.exclusion_filter import COMPANY_DENYLIST
        from jobcannon.engine.normalizers import normalize_company

        for entry in COMPANY_DENYLIST:
            assert entry == normalize_company(entry), f"{entry!r} is not in normalized form"

    def test_config_entry_normalized_on_merge(self):
        from jobcannon.engine.exclusion_filter import get_company_denylist

        merged = get_company_denylist({"filters": {"company_denylist": ["Umbrella Corp."]}})
        assert "umbrella" in merged  # 'Corp.' suffix stripped + lowercased
        assert "umbrella corp." not in merged

    def test_config_is_additive_to_seed(self):
        from jobcannon.engine.exclusion_filter import COMPANY_DENYLIST, get_company_denylist

        merged = get_company_denylist({"filters": {"company_denylist": ["Custom Spam"]}})
        assert merged >= COMPANY_DENYLIST

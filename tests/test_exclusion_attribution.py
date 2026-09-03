# PORTED from tests/test_exclusion_attribution.py @ 694ac4d08d0f98c322f050b2804894917cdeb64a (private job-cannon). Ledger L-0489.
"""Tests for exclusion attribution and filter-visibility ledger (issue #849).

# PORT-SEAM: only the pure should_exclude() unit tests are carried here --
# everything else in this file asserts on private-only DB/route surfaces
# (see the drop-site comments below and the PR body for the full list and
# reasons). should_exclude() already has extensive pre-existing coverage in
# tests/engine/test_exclusion_filter.py (title/company/salary-floor/
# denylist/config-merge paths); the 3 tests kept here are near-fully
# subsumed by that suite but are ported per the L-0509 precedent (port
# despite overlap, note it, no re-adjudication authority).
"""

import pytest

# PORT-SEAM: private's job_finder.db exclusion-ledger imports
# (get_excluded_jobs, get_excluded_jobs_counts, get_filtered_jobs,
# update_pipeline_status) dropped -- only the pure should_exclude() unit
# tests are carried (see module docstring).
from jobcannon.engine.exclusion_filter import should_exclude

# PORT-SEAM: test_excluded_reason_column_exists,
# test_update_pipeline_status_sets_excluded_reason,
# test_update_pipeline_status_does_not_set_excluded_reason_for_manual,
# test_get_excluded_jobs_counts, test_get_excluded_jobs_counts_empty,
# test_get_filtered_jobs_with_excluded_reason_filter,
# test_get_filtered_jobs_without_excluded_reason_filter dropped here --
# each asserts on the private jobs.excluded_reason column and/or
# update_pipeline_status/get_excluded_jobs[_counts]/get_filtered_jobs,
# none of which exist publicly (see this row's ledger evidence: the
# defining module job_finder/db/_queries.py is ADAPT, not carried by this
# row). migrated_db_mem (private's sqlite fixture) is dropped for the same
# reason -- no test below touches a database.


def test_should_exclude_returns_reason():
    """Test that should_exclude returns coarse rule tag and detailed text."""
    job = {
        "title": "Software Engineer Intern",
        "company": "Test Company",
        "salary_max": 100000,
    }
    exclusions = {"title_keywords": ["intern"]}
    min_salary = None

    excluded, rule_tag, detailed_text = should_exclude(job, exclusions, min_salary)

    assert excluded is True
    assert rule_tag == "title_kw"
    assert "intern" in detailed_text.lower()


@pytest.mark.parametrize(
    "salary_currency,salary_max,expected_excluded",
    [
        ("USD", 80_000, True),
        ("GBP", 80_000, False),
    ],
)
def test_should_exclude_salary_floor(salary_currency, salary_max, expected_excluded):
    """Salary floor excludes USD salaries below the floor; non-USD is not compared."""
    job = {
        "title": "Software Engineer",
        "company": "Test Company",
        "salary_max": salary_max,
        "salary_currency": salary_currency,
    }
    exclusions = {}
    min_salary = 100000

    excluded, rule_tag, detailed_text = should_exclude(job, exclusions, min_salary)

    assert excluded is expected_excluded
    if expected_excluded:
        assert rule_tag == "salary_floor"
        assert "salary" in detailed_text.lower()
    else:
        assert rule_tag == ""
        assert detailed_text == ""


def test_should_exclude_company():
    """Test that should_exclude returns rule tag and detailed text for company exclusion."""
    job = {
        "title": "Software Engineer",
        "company": "Excluded Company",
        "salary_max": 100000,
    }
    exclusions = {"companies": ["Excluded Company"]}
    min_salary = None

    excluded, rule_tag, detailed_text = should_exclude(job, exclusions, min_salary)

    assert excluded is True
    assert rule_tag == "company"
    assert "excluded company" in detailed_text.lower()


# PORT-SEAM: test_get_excluded_jobs_parity_with_counts,
# test_excluded_reason_hidden_input_round_trips (app/client Flask
# fixtures), and test_table_without_excluded_reason_does_not_conflate
# (app/client Flask fixtures) dropped here for the same private-only-surface
# reasons as the block dropped above (see module docstring / PR body).

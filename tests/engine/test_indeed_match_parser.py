# PORTED from tests/test_indeed_match_parser.py @ e6ff9bf3c0f5b7505b127e6b6a0ca945382831be (private job-cannon). Ledger L-0554.
"""Tests for Indeed match email parser (donotreply@match.indeed.com)."""

from datetime import datetime

from jobcannon.engine.email_parsers.indeed_parser import parse_indeed_match_alert

# ---------------------------------------------------------------------------
# Fixtures: realistic email bodies from actual match.indeed.com emails
# ---------------------------------------------------------------------------

SAMPLE_SINGLE_JOB_WITH_SALARY = (
    "We thought this job for a AI Automation Engineer at Confidential "
    "in Fairfield, CA 94533 paying $100,000 - $150,000 a year would be a "
    "good fit. Check out the job at "
    "https://cts.indeed.com/v3/H4sIAAAAAAAA_32T2abc/6sPXQ-MBRqZd\n"
    "Unsubscribe: https://cts.indeed.com/v3/H4sIAAAAAAAA_12RXW-abc"
)

SAMPLE_SINGLE_JOB_NO_SALARY = (
    "We thought this job for a Senior Data Analyst at Acme Corp "
    "in San Francisco, CA would be a good fit. Check out the job at "
    "https://cts.indeed.com/v3/H4sIBBBBBBBB_32T2xyz/abcdef123\n"
    "Unsubscribe: https://cts.indeed.com/v3/H4sICCCCCCCC_12RXWyz"
)

SAMPLE_MULTI_JOB = """\
Hi ALEX, Your background in product analytics and experience with LIMS \
could be a good match for this Sr. Technical Product Analyst role at Atlas. \
If you're interested in applying your skills in the pharmaceutical industry, \
you can apply now or explore more jobs below.
Jobs are based on your preferences, profile, and activity on Indeed \u00b9

Sr. Technical Product Analyst
Atlas - Remote
$70 - $80 an hour
Easily apply
Education: Bachelor's degree in Life Sciences, Data Science, Business Administration...

https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=5-cmh1-1-1jk7u1udhjvtc805-e7f5e28509e7dd2d&mo=r&ad=abc123&jsa=6355

Data Scientist-II
HackerEarth - Sunnyvale, CA
$140,000 - $170,000 a year
Responsive employer
Easily apply
The ideal candidate should be comfortable working with large datasets...

https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=5-cmh1-1-1jk7u1udhjvtc805-1d436b6214eaf74a&mo=r&ad=def456&jsa=6356

Brand Manager \u2013 CPG packaging
SGS Consulting - Remote
$95 an hour
Easily apply
Proficiency with databases, data gathering tools...

https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=5-cmh1-1-1jk7u1udhjvtc805-2ecfa4e01dc075db&mo=r&ad=ghi789&jsa=6357

Senior Innovation Program Manager
Turning Stone Enterprises - Remote
$121,000 - $154,000 a year
Responsive employer
Easily apply
By aligning cross-functional teams, shaping governance frameworks...

https://www.indeed.com/pagead/clk/dl?from=jobi2a_multijob-en-US_email&jrtk=5-cmh1-1-1jk7u1udhjvtc805-52127687c2b7932a&mo=r&ad=jkl012&jsa=6357

Salaries estimated if unavailable. When a job posting doesn't include a salary, we estimate it.

\u00a9 2026 Indeed, Inc.
Indeed Tower 200 West 6th Street, Floor 36, Austin, TX 78701
"""

# ---------------------------------------------------------------------------
# Single-job format
# ---------------------------------------------------------------------------


class TestIndeedMatchSingleJob:
    def test_parses_single_job(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert len(jobs) == 1

    def test_single_job_title(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].title == "AI Automation Engineer"

    def test_single_job_company(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].company == "Confidential"

    def test_single_job_location(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].location == "Fairfield, CA 94533"

    def test_single_job_salary(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].salary_min == 100000
        assert jobs[0].salary_max == 150000

    def test_single_job_source(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].source == "indeed"

    def test_single_job_source_url(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert "cts.indeed.com" in jobs[0].source_url

    def test_single_job_source_id(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY)
        assert jobs[0].source_id  # non-empty

    def test_single_job_no_salary(self):
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_NO_SALARY)
        assert len(jobs) == 1
        assert jobs[0].title == "Senior Data Analyst"
        assert jobs[0].company == "Acme Corp"
        assert jobs[0].salary_min is None
        assert jobs[0].salary_max is None

    def test_single_job_email_date(self):
        date = datetime(2026, 3, 24)
        jobs = parse_indeed_match_alert(SAMPLE_SINGLE_JOB_WITH_SALARY, email_date=date)
        assert jobs[0].posted_date == date


# ---------------------------------------------------------------------------
# Multi-job format
# ---------------------------------------------------------------------------


class TestIndeedMatchMultiJob:
    def test_parses_multiple_jobs(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert len(jobs) == 4

    def test_first_job_title(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].title == "Sr. Technical Product Analyst"

    def test_first_job_company(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].company == "Atlas"

    def test_first_job_location(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].location == "Remote"

    def test_hourly_salary_annualized(self):
        """$70 - $80 an hour should be annualized via 2080 multiplier."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].salary_min == (70 * 2080)
        assert jobs[0].salary_max == (80 * 2080)

    def test_annual_salary_parsed(self):
        """$140,000 - $170,000 a year should be parsed directly."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[1].salary_min == 140000
        assert jobs[1].salary_max == 170000

    def test_single_hourly_annualized(self):
        """$95 an hour should be annualized."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[2].salary_min == (95 * 2080)
        assert jobs[2].salary_max == (95 * 2080)

    def test_noise_lines_filtered(self):
        """'Easily apply' and 'Responsive employer' should not appear as titles."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        for job in jobs:
            assert job.title.lower() not in ("easily apply", "responsive employer")

    def test_description_not_in_title(self):
        """Description snippets should not be parsed as job titles."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        for job in jobs:
            assert len(job.title) < 150

    def test_stops_at_footer(self):
        """Footer content ('Salaries estimated', copyright) should not be parsed."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        for job in jobs:
            assert "salaries estimated" not in job.title.lower()
            assert "\u00a9" not in job.title

    def test_source_is_indeed(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert all(j.source == "indeed" for j in jobs)

    def test_source_url_is_indeed_pagead(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert all("indeed.com/pagead/clk/dl" in j.source_url for j in jobs)

    def test_source_id_is_jrtk(self):
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].source_id == "5-cmh1-1-1jk7u1udhjvtc805-e7f5e28509e7dd2d"

    def test_email_date_propagated(self):
        date = datetime(2026, 3, 21)
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB, email_date=date)
        assert all(j.posted_date == date for j in jobs)

    def test_intro_paragraph_not_parsed_as_job(self):
        """The personalized 'Hi ALEX...' intro should not create a job."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        for job in jobs:
            assert "ALEX" not in job.title
            assert "background" not in job.title.lower()

    def test_company_with_dash_in_name(self):
        """'SGS Consulting - Remote' should parse company as SGS Consulting."""
        jobs = parse_indeed_match_alert(SAMPLE_MULTI_JOB)
        brand_mgr = [j for j in jobs if "Brand Manager" in j.title]
        assert len(brand_mgr) == 1
        assert brand_mgr[0].company == "SGS Consulting"
        assert brand_mgr[0].location == "Remote"


# ---------------------------------------------------------------------------
# New-format single-job (2026-04+): structured block + "View job:" anchor
# ---------------------------------------------------------------------------

# Derived from data/parse_failures/match_indeed_com_2026-05-18T20-15-01.html
# (real GoodRx Lead Data Analyst match email). URLs shortened for fixture
# readability; structural layout (blank-line paragraph breaks, indented
# Benefits items, field labels) preserved verbatim.
SAMPLE_NEW_FORMAT_FULL = """\
Hi ALEX,

Your background in analytics leadership and experience with SQL and Python \
could be a great fit for this Lead Data Analyst role at GoodRx. If you're \
interested in applying your skills to healthcare analytics, feel free to \
apply now or view the job description for more information.

Lead Data Analyst
GoodRx
Remote
Salary: $131,000 - $229,000 a year
Job type: Full-time

Benefits:
  - Health insurance
  - Dental insurance
  - 401(k) matching

View job: https://cts.indeed.com/v3/H4sIAAAAAAAA_GOODRX_VIEW_JOB_TOKEN/AAAAA
Apply now: https://cts.indeed.com/v3/H4sIAAAAAAAA_GOODRX_APPLY_TOKEN/BBBBB

Do you want to get more jobs like this?
No: https://cts.indeed.com/v3/H4sIAAAAAAAA_NO_TOKEN/CCCCC
"""

# Derived from data/parse_failures/match_indeed_com_2026-04-29T00-00-07.html.
# Single-bounded salary ("From $160,000 a year") falls outside the existing
# range parser, so salary_min/max stays None — a no-regression case.
SAMPLE_NEW_FORMAT_SINGLE_BOUND_SALARY = """\
Hi ALEX,

Your background in product analytics and proficiency with Python and SQL \
could be a great match for this Lead Systems Architect role. CASS NV, LLC \
is looking for someone to build their data pipeline.

Lead Systems Architect
CASS NV,LLC
Oakland, CA 94607
Salary: From $160,000 a year
Job type: Full-time
Work setting: Factory, In-person

Benefits:
  - Paid time off

View job: https://cts.indeed.com/v3/H4sIAAAAAAAA_CASS_VIEW_TOKEN/DDDDD
"""

# Variant: no Salary/Job type lines at all (3-line structured block).
# Intro mirrors real Indeed multi-sentence wording so the intro detector trips.
SAMPLE_NEW_FORMAT_NO_FIELDS = """\
Hi ALEX,

Your skills in distributed systems could be a great match for this Data \
Engineer role at Acme Corp. If you're interested in modern data tooling, \
feel free to apply now or learn more in the job description.

Data Engineer
Acme Corp
San Francisco, CA

View job: https://cts.indeed.com/v3/H4sIAAAAAAAA_ACME_VIEW_TOKEN/EEEEE
"""


class TestIndeedMatchNewSingleFormat:
    """2026-04+ structured-block format from donotreply@match.indeed.com."""

    def test_parses_single_job(self):
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert len(jobs) == 1

    def test_title(self):
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert jobs[0].title == "Lead Data Analyst"

    def test_company(self):
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert jobs[0].company == "GoodRx"

    def test_location(self):
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert jobs[0].location == "Remote"

    def test_salary_range(self):
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert jobs[0].salary_min == 131000
        assert jobs[0].salary_max == 229000

    def test_source(self):
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert jobs[0].source == "indeed"

    def test_source_url_is_view_job_url(self):
        """Authoritative URL is the 'View job:' link, not 'Apply now:'."""
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert "GOODRX_VIEW_JOB_TOKEN" in jobs[0].source_url
        assert "GOODRX_APPLY_TOKEN" not in jobs[0].source_url

    def test_source_id_extracted(self):
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert jobs[0].source_id  # non-empty

    def test_email_date_propagated(self):
        date = datetime(2026, 5, 18)
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL, email_date=date)
        assert jobs[0].posted_date == date

    def test_intro_paragraph_not_in_title(self):
        """The personalized 'Your background...' intro should not be parsed."""
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert "background" not in jobs[0].title.lower()
        assert "alex" not in jobs[0].title.lower()

    def test_benefits_not_in_title(self):
        """The Benefits paragraph should not be picked up as the job block."""
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_FULL)
        assert "health insurance" not in jobs[0].title.lower()
        assert "benefits" not in jobs[0].title.lower()

    def test_single_bound_salary_no_range(self):
        """'From $160,000 a year' has no range — salary should be None."""
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_SINGLE_BOUND_SALARY)
        assert len(jobs) == 1
        assert jobs[0].title == "Lead Systems Architect"
        assert jobs[0].company == "CASS NV,LLC"
        assert jobs[0].location == "Oakland, CA 94607"
        assert jobs[0].salary_min is None
        assert jobs[0].salary_max is None

    def test_no_field_lines(self):
        """Block with only title/company/location (no Salary/Job type) parses."""
        jobs = parse_indeed_match_alert(SAMPLE_NEW_FORMAT_NO_FIELDS)
        assert len(jobs) == 1
        assert jobs[0].title == "Data Engineer"
        assert jobs[0].company == "Acme Corp"
        assert jobs[0].location == "San Francisco, CA"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestIndeedMatchEdgeCases:
    def test_empty_body(self):
        assert parse_indeed_match_alert("") == []

    def test_none_body(self):
        assert parse_indeed_match_alert(None) == []

    def test_whitespace_only(self):
        assert parse_indeed_match_alert("   \n\n  ") == []

    def test_no_indeed_urls_returns_empty(self):
        assert parse_indeed_match_alert("Just some random text.") == []

    def test_unrelated_url_returns_empty(self):
        assert parse_indeed_match_alert("Visit https://example.com") == []


# ---------------------------------------------------------------------------
# Salary fix regression: existing _extract_salary_from_text improvements
# ---------------------------------------------------------------------------


class TestHourlySalaryFix:
    """Verify the hourly range fix doesn't break existing salary parsing."""

    def test_hourly_range_annualized(self):
        from jobcannon.engine.email_parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$70 - $80 an hour")
        assert result == ((70 * 2080), (80 * 2080))

    def test_hourly_single_annualized(self):
        from jobcannon.engine.email_parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$95 an hour")
        assert result == ((95 * 2080), (95 * 2080))

    def test_hourly_per_hour(self):
        from jobcannon.engine.email_parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$50.50 - $60 per hour")
        assert result == (int(50.50 * 2080), (60 * 2080))

    def test_annual_range_unchanged(self):
        from jobcannon.engine.email_parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$140,000 - $170,000 a year")
        assert result == (140000, 170000)

    def test_k_notation_unchanged(self):
        from jobcannon.engine.email_parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$120K - $150K")
        assert result == (120000, 150000)

    def test_slash_hr_still_works(self):
        from jobcannon.engine.email_parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("$25/hr")
        assert result == ((25 * 2080), (25 * 2080))

    def test_no_salary(self):
        from jobcannon.engine.email_parsers.indeed_parser import _extract_salary_from_text

        result = _extract_salary_from_text("No salary listed here")
        assert result == (None, None)

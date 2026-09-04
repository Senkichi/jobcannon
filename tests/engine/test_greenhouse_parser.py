# PORTED from tests/test_greenhouse_parser.py @ ebb9f865b13b6304615b0ba11e5e39750151de1b (private job-cannon). Ledger L-0553.
"""Tests for Greenhouse job alert parser (no-reply@us.greenhouse-jobs.com)."""

from datetime import datetime

from jobcannon.engine.email_parsers.greenhouse_parser import parse_greenhouse_alert

SAMPLE_SINGLE_JOB = """\
Greenhouse\r\r
\r\r
Here's your weekly update about new roles. If you see a job\r\r
that's a good fit, apply early to help your application stand\r\r
out.\r\r
\r\r
ParetoHealth logo\r\r
\r\r
Lead Data Analyst \r\r
( https://job-boards.greenhouse.io/paretocaptiveservicesllc/jobs/4647776006?gh_src=myjobs.greenhouse )\r\r
\r\r
ParetoHealth\r\r
Tech\r\r
\r\r
We'll send your next job alert soon - but why wait? Get a head\r\r
start by searching MyGreenhouse Jobs and find your next role\r\r
faster.\r\r
\r\r
Sincerely,\r\r
Greenhouse\r\r
\r\r
Search MyGreenhouse Jobs ( https://my.greenhouse.io/jobs )\r\r
\r\r
Unsubscribe \r\r
( https://my.greenhouse.io/unsubscribe?token=abc )\r\r
\r\r
\r\r
\u00a9 2026 Greenhouse\r\r
\r\r
250 W 34th Street, Suite 329, New York, NY 10119, USA"""

SAMPLE_MULTI_JOB = """\
Here's your weekly update about new roles.

Senior Engineer
( https://job-boards.greenhouse.io/acmecorp/jobs/1111111?gh_src=myjobs.greenhouse )

Acme Corp
Engineering

Product Manager
( https://job-boards.greenhouse.io/widgetsinc/jobs/2222222?gh_src=myjobs.greenhouse )

Widgets Inc
Product

We'll send your next job alert soon."""


class TestGreenhouseParser:
    def test_parses_single_job(self):
        jobs = parse_greenhouse_alert(SAMPLE_SINGLE_JOB)
        assert len(jobs) == 1

    def test_job_title(self):
        jobs = parse_greenhouse_alert(SAMPLE_SINGLE_JOB)
        assert jobs[0].title == "Lead Data Analyst"

    def test_job_company(self):
        jobs = parse_greenhouse_alert(SAMPLE_SINGLE_JOB)
        assert jobs[0].company == "ParetoHealth"

    def test_source_id_from_url(self):
        jobs = parse_greenhouse_alert(SAMPLE_SINGLE_JOB)
        assert jobs[0].source_id == "4647776006"

    def test_clean_source_url(self):
        """source_url should not include ?gh_src= tracking param."""
        jobs = parse_greenhouse_alert(SAMPLE_SINGLE_JOB)
        assert "gh_src" not in jobs[0].source_url
        assert "job-boards.greenhouse.io" in jobs[0].source_url
        assert "/jobs/4647776006" in jobs[0].source_url

    def test_source_is_greenhouse(self):
        jobs = parse_greenhouse_alert(SAMPLE_SINGLE_JOB)
        assert jobs[0].source == "greenhouse"

    def test_email_date_propagated(self):
        date = datetime(2026, 1, 29)
        jobs = parse_greenhouse_alert(SAMPLE_SINGLE_JOB, email_date=date)
        assert jobs[0].posted_date == date


class TestGreenhouseMultiJob:
    def test_parses_multiple_jobs(self):
        jobs = parse_greenhouse_alert(SAMPLE_MULTI_JOB)
        assert len(jobs) == 2

    def test_first_job_fields(self):
        jobs = parse_greenhouse_alert(SAMPLE_MULTI_JOB)
        assert jobs[0].title == "Senior Engineer"
        assert jobs[0].company == "Acme Corp"
        assert jobs[0].source_id == "1111111"

    def test_second_job_fields(self):
        jobs = parse_greenhouse_alert(SAMPLE_MULTI_JOB)
        assert jobs[1].title == "Product Manager"
        assert jobs[1].company == "Widgets Inc"
        assert jobs[1].source_id == "2222222"

    def test_stops_at_footer(self):
        jobs = parse_greenhouse_alert(SAMPLE_MULTI_JOB)
        for job in jobs:
            assert "we'll send" not in job.title.lower()


class TestGreenhouseEdgeCases:
    def test_empty_body(self):
        assert parse_greenhouse_alert("") == []

    def test_none_body(self):
        assert parse_greenhouse_alert(None) == []

    def test_no_greenhouse_urls(self):
        assert parse_greenhouse_alert("Just some text") == []

    def test_dedup_same_job_id(self):
        body = """\
Job A
( https://job-boards.greenhouse.io/co/jobs/999?gh_src=a )

Company

Job B
( https://job-boards.greenhouse.io/co/jobs/999?gh_src=b )

Company

Sincerely,"""
        jobs = parse_greenhouse_alert(body)
        assert len(jobs) == 1

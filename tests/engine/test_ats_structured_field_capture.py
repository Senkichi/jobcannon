"""ATS structured-field CAPTURE unit tests.

Each of the five ATS scanners' ``_posting_to_job`` must extend the canonical
job dict with three raw-as-provided capture keys — ``is_remote`` /
``employment_type`` / ``department`` — reading whichever field the provider's
public JSON exposes, and returning ``None`` (never synthesizing) when absent.

These are pure mapping tests: provider HTTP detail fetches (SmartRecruiters /
Workday) are patched out so the assertions exercise only the field-capture
logic against fixture payloads.
"""

from __future__ import annotations

from unittest.mock import patch

from jobcannon.engine.ats_platforms._platforms_ashby import (
    _posting_to_job as ashby_posting_to_job,
)
from jobcannon.engine.ats_platforms._platforms_greenhouse import (
    _posting_to_job as greenhouse_posting_to_job,
)
from jobcannon.engine.ats_platforms._platforms_lever import (
    _posting_to_job as lever_posting_to_job,
)
from jobcannon.engine.ats_platforms._platforms_smartrecruiters import (
    _posting_to_job as smartrecruiters_posting_to_job,
)
from jobcannon.engine.ats_platforms._platforms_workday import (
    _posting_to_job as workday_posting_to_job,
)

# ---------------------------------------------------------------------------
# Ashby — isRemote (bool), employmentType (string), department / team
# ---------------------------------------------------------------------------


class TestAshbyCapture:
    def test_fields_present(self):
        posting = {
            "title": "Staff Engineer",
            "isRemote": True,
            "employmentType": "FullTime",
            "department": "Engineering",
            "descriptionPlain": "Build things.",
            "jobUrl": "https://jobs.ashbyhq.com/Co/abc",
            "id": "abc",
        }
        job = ashby_posting_to_job(posting, "Co")
        assert job["is_remote"] is True
        assert job["employment_type"] == "FullTime"
        assert job["department"] == "Engineering"

    def test_non_remote(self):
        posting = {
            "title": "Onsite Engineer",
            "isRemote": False,
            "descriptionPlain": "Build things.",
        }
        job = ashby_posting_to_job(posting, "Co")
        assert job["is_remote"] in (False, None)
        # Per the explicit payload contents, isRemote=False → False (not None).
        assert job["is_remote"] is False

    def test_department_falls_back_to_team(self):
        posting = {
            "title": "Engineer",
            "team": "Platform",
            "descriptionPlain": "Build things.",
        }
        job = ashby_posting_to_job(posting, "Co")
        assert job["department"] == "Platform"

    def test_fields_absent(self):
        posting = {"title": "Engineer", "descriptionPlain": "Build things."}
        job = ashby_posting_to_job(posting, "Co")
        assert job["is_remote"] is None
        assert job["employment_type"] is None
        assert job["department"] is None


# ---------------------------------------------------------------------------
# SmartRecruiters — location.remote (bool), typeOfEmployment / department objs
# ---------------------------------------------------------------------------


class TestSmartRecruitersCapture:
    @patch(
        "jobcannon.engine.ats_platforms._fetch_smartrecruiters_description",
        return_value="",
    )
    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.time.sleep")
    def test_fields_present(self, _sleep, _desc):
        posting = {
            "name": "Staff Engineer",
            "id": "1",
            "location": {"city": "New York", "country": "US", "remote": True},
            "typeOfEmployment": {"id": "1", "label": "Permanent"},
            "department": {"id": "2", "label": "Engineering"},
        }
        job = smartrecruiters_posting_to_job(posting, "Co")
        assert job["is_remote"] is True
        assert job["employment_type"] == "Permanent"
        assert job["department"] == "Engineering"

    @patch(
        "jobcannon.engine.ats_platforms._fetch_smartrecruiters_description",
        return_value="",
    )
    @patch("jobcannon.engine.ats_platforms._platforms_smartrecruiters.time.sleep")
    def test_fields_absent(self, _sleep, _desc):
        posting = {
            "name": "Engineer",
            "id": "1",
            "location": {"city": "New York", "country": "US"},
        }
        job = smartrecruiters_posting_to_job(posting, "Co")
        assert job["is_remote"] is None
        assert job["employment_type"] is None
        assert job["department"] is None


# ---------------------------------------------------------------------------
# Lever — workplaceType enum (→ is_remote), categories.commitment / department
# ---------------------------------------------------------------------------


class TestLeverCapture:
    def test_fields_present_remote(self):
        posting = {
            "id": "1",
            "text": "Staff Engineer",
            "workplaceType": "remote",
            "categories": {
                "location": "New York",
                "commitment": "Full-time",
                "department": "Engineering",
                "team": "Core",
            },
            "descriptionPlain": "Build things.",
            "hostedUrl": "https://jobs.lever.co/Co/abc",
        }
        job = lever_posting_to_job(posting, "Co")
        assert job["is_remote"] is True
        assert job["employment_type"] == "Full-time"
        assert job["department"] == "Engineering"

    def test_non_remote(self):
        posting = {
            "id": "1",
            "text": "Onsite Engineer",
            "workplaceType": "on-site",
            "categories": {"location": "New York"},
            "descriptionPlain": "Build things.",
        }
        job = lever_posting_to_job(posting, "Co")
        assert job["is_remote"] is False

    def test_department_falls_back_to_team(self):
        posting = {
            "id": "1",
            "text": "Engineer",
            "categories": {"team": "Platform"},
            "descriptionPlain": "Build things.",
        }
        job = lever_posting_to_job(posting, "Co")
        assert job["department"] == "Platform"

    def test_fields_absent(self):
        posting = {
            "id": "1",
            "text": "Engineer",
            "categories": {},
            "descriptionPlain": "Build things.",
        }
        job = lever_posting_to_job(posting, "Co")
        assert job["is_remote"] is None
        assert job["employment_type"] is None
        assert job["department"] is None


# ---------------------------------------------------------------------------
# Greenhouse — Board API exposes only departments[]; no remote/employment field
# ---------------------------------------------------------------------------


class TestGreenhouseCapture:
    def test_department_present(self):
        posting = {
            "id": 1,
            "title": "Staff Engineer",
            "content": "<p>Build things.</p>",
            "location": {"name": "New York"},
            "departments": [{"id": 1, "name": "Engineering"}],
        }
        job = greenhouse_posting_to_job(posting, "Co")
        assert job["department"] == "Engineering"
        # Greenhouse's Board API carries neither field — both stay None.
        assert job["is_remote"] is None
        assert job["employment_type"] is None

    def test_fields_absent(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "content": "<p>Build things.</p>",
            "location": {"name": "New York"},
        }
        job = greenhouse_posting_to_job(posting, "Co")
        assert job["is_remote"] is None
        assert job["employment_type"] is None
        assert job["department"] is None

    def test_ats_refreshed_at_present(self):
        posting = {
            "id": 1,
            "title": "Staff Engineer",
            "content": "<p>Build things.</p>",
            "location": {"name": "New York"},
            "updated_at": "2026-06-26T17:05:44-04:00",
        }
        job = greenhouse_posting_to_job(posting, "Co")
        # Should normalize to naive UTC ISO
        assert job["ats_refreshed_at"] == "2026-06-26T21:05:44"

    def test_ats_refreshed_at_absent(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "content": "<p>Build things.</p>",
            "location": {"name": "New York"},
        }
        job = greenhouse_posting_to_job(posting, "Co")
        assert job["ats_refreshed_at"] is None

    def test_ats_refreshed_at_malformed(self):
        posting = {
            "id": 1,
            "title": "Engineer",
            "content": "<p>Build things.</p>",
            "location": {"name": "New York"},
            "updated_at": "not-a-date",
        }
        job = greenhouse_posting_to_job(posting, "Co")
        # Malformed timestamps should be NULL, not garbage
        assert job["ats_refreshed_at"] is None


# ---------------------------------------------------------------------------
# Workday — CXS list payload rarely carries these; defensive reads → None
# ---------------------------------------------------------------------------


class TestWorkdayCapture:
    @patch("jobcannon.engine.ats_platforms._fetch_workday_description", return_value="")
    @patch("jobcannon.engine.ats_platforms._platforms_workday.time.sleep")
    def test_fields_present(self, _sleep, _desc):
        posting = {
            "title": "Staff Engineer",
            "externalPath": "/job/Staff-Engineer_R-1",
            "locationsText": "New York",
            "isRemote": True,
            "employmentType": "Full time",
            "department": "Engineering",
            "__workday_subdomain": "co.wd5",
            "__workday_tenant": "co",
            "__workday_board": "External",
        }
        job = workday_posting_to_job(posting, "co.wd5/External")
        assert job["is_remote"] is True
        assert job["employment_type"] == "Full time"
        assert job["department"] == "Engineering"

    @patch("jobcannon.engine.ats_platforms._fetch_workday_description", return_value="")
    @patch("jobcannon.engine.ats_platforms._platforms_workday.time.sleep")
    def test_fields_absent(self, _sleep, _desc):
        posting = {
            "title": "Engineer",
            "externalPath": "/job/Engineer_R-2",
            "locationsText": "New York",
            "__workday_subdomain": "co.wd5",
            "__workday_tenant": "co",
            "__workday_board": "External",
        }
        job = workday_posting_to_job(posting, "co.wd5/External")
        assert job["is_remote"] is None
        assert job["employment_type"] is None
        assert job["department"] is None

import pytest

from jobcannon.engine import parsed_job
from jobcannon.engine.models import Job


def _make_job(company: str) -> Job:
    """Minimal valid Job — same builder pattern as the private repo's
    tests/test_parsed_job_validators.py::_make_job (defaults chosen so no
    validator other than I-10 company-denylist can fire)."""
    return Job(
        title="Software Engineer",
        company=company,
        location="New York, NY",
        source="linkedin",
        source_url="https://linkedin.com/jobs/1",
        source_id="",
        description=None,
    )


def test_no_provider_means_empty_denylist():
    parsed_job.set_denylist_provider(None)
    job = _make_job(company="Virtual Vocations Inc")
    result = parsed_job.ParsedJob.from_job(job)
    assert isinstance(result, parsed_job.ParsedJob)


def test_denylisted_company_raises():
    parsed_job.set_denylist_provider(lambda: frozenset({"virtual vocations"}))
    try:
        job = _make_job(company="Virtual Vocations Inc")
        with pytest.raises(parsed_job.DenylistedCompanyError):
            parsed_job.ParsedJob.from_job(job)
    finally:
        parsed_job.set_denylist_provider(None)

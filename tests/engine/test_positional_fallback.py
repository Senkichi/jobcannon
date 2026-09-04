# PORTED from tests/test_positional_fallback.py @ 1b3b47b304f8af1791c12ed5d9462899ee98f321 (private job-cannon). Ledger L-0560.
"""Tests for the generic URL-anchored positional fallback
(job_finder/parsers/_positional_fallback.py) and the gated
extract_with_fallback wrapper (job_finder/parsers/__init__.py).
"""

from jobcannon.engine.email_parsers._positional_fallback import has_job_urls, positional_fallback

# ---------------------------------------------------------------------------
# has_job_urls
# ---------------------------------------------------------------------------


def test_has_job_urls_empty_body() -> None:
    assert not has_job_urls("")


def test_has_job_urls_no_recognized_url() -> None:
    assert not has_job_urls("Check out this link: https://example.com/careers")


def test_has_job_urls_greenhouse() -> None:
    body = "https://job-boards.greenhouse.io/acme/jobs/123"
    assert has_job_urls(body)


def test_has_job_urls_lever() -> None:
    assert has_job_urls("https://jobs.lever.co/stripe/abc-def-123")


def test_has_job_urls_ashby() -> None:
    assert has_job_urls("https://jobs.ashbyhq.com/acme/12345")


def test_has_job_urls_indeed() -> None:
    assert has_job_urls("https://www.indeed.com/viewjob?jk=abc123")


def test_has_job_urls_linkedin() -> None:
    assert has_job_urls("https://www.linkedin.com/jobs/view/1234567890")


def test_has_job_urls_workday() -> None:
    assert has_job_urls("https://acme.myworkdayjobs.com/en-US/careers/job/123")


def test_has_job_urls_ziprecruiter() -> None:
    assert has_job_urls("https://www.ziprecruiter.com/jobs/acme-12345")


# ---------------------------------------------------------------------------
# positional_fallback — basic extraction
# ---------------------------------------------------------------------------


def test_no_recognized_urls_returns_empty() -> None:
    assert positional_fallback("just some text, no jobs") == []


def test_empty_body_returns_empty() -> None:
    assert positional_fallback("") == []


def test_extracts_from_greenhouse_url_block() -> None:
    # Greenhouse plain-text structure: title line, then URL, then company name.
    body = (
        "Some preamble header line\n"
        "Senior Data Scientist\n"
        "https://job-boards.greenhouse.io/acme/jobs/123\n"
        "Acme Corp\n"
    )
    jobs = positional_fallback(body)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Senior Data Scientist"
    assert job.company == "Acme Corp"
    assert "greenhouse.io/acme/jobs/123" in job.source_url
    assert job.source == "email_fallback"
    assert job.location == ""


def test_extracts_from_lever_url_block() -> None:
    body = "Backend Engineer\nhttps://jobs.lever.co/stripe/abc-def-123\nStripe\n"
    jobs = positional_fallback(body)
    assert len(jobs) == 1
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].company == "Stripe"


def test_extracts_from_ashby_url_block() -> None:
    body = "Product Manager\nhttps://jobs.ashbyhq.com/openai/99999\nOpenAI\n"
    jobs = positional_fallback(body)
    assert len(jobs) == 1
    assert jobs[0].title == "Product Manager"
    assert jobs[0].company == "OpenAI"


def test_extracts_multiple_jobs() -> None:
    body = (
        "Software Engineer\n"
        "https://job-boards.greenhouse.io/co1/jobs/1\n"
        "Company One\n"
        "\n"
        "Data Analyst\n"
        "https://jobs.lever.co/co2/xyz\n"
        "Company Two\n"
    )
    jobs = positional_fallback(body)
    assert len(jobs) == 2
    titles = {j.title for j in jobs}
    assert "Software Engineer" in titles
    assert "Data Analyst" in titles


# ---------------------------------------------------------------------------
# positional_fallback — placeholder rejection
# ---------------------------------------------------------------------------


def test_rejects_placeholder_title() -> None:
    # "unknown" is in _PLACEHOLDER_STRINGS — should be rejected as title
    body = "unknown\nhttps://job-boards.greenhouse.io/acme/jobs/9\nAcme Corp\n"
    assert positional_fallback(body) == []


def test_rejects_placeholder_company() -> None:
    # "company" is in _PLACEHOLDER_STRINGS — should be rejected as company
    body = "Real Job Title\nhttps://job-boards.greenhouse.io/acme/jobs/9\ncompany\n"
    assert positional_fallback(body) == []


def test_rejects_none_placeholder() -> None:
    body = "none\nhttps://jobs.lever.co/co/abc\nnone\n"
    assert positional_fallback(body) == []


# ---------------------------------------------------------------------------
# positional_fallback — URL deduplication
# ---------------------------------------------------------------------------


def test_deduplicates_same_url() -> None:
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    body = f"Senior Engineer\n{url}\nAcme Corp\n\nSenior Engineer\n{url}\nAcme Corp\n"
    jobs = positional_fallback(body)
    assert len(jobs) == 1


# ---------------------------------------------------------------------------
# positional_fallback — posted_date passthrough
# ---------------------------------------------------------------------------


def test_posted_date_passed_through() -> None:
    from datetime import datetime

    date = datetime(2024, 6, 1, 12, 0, 0)
    body = "ML Engineer\nhttps://jobs.lever.co/anthropic/abc\nAnthropic\n"
    jobs = positional_fallback(body, email_date=date)
    assert len(jobs) == 1
    assert jobs[0].posted_date == date


# ---------------------------------------------------------------------------
# extract_with_fallback (parsers/__init__.py) — after Task 3 wires it up
# ---------------------------------------------------------------------------


def test_fallback_not_used_when_primary_succeeds() -> None:
    from jobcannon.engine.email_parsers import extract_with_fallback

    primary = lambda body, date: ["primary-job"]  # noqa: E731
    body = "https://job-boards.greenhouse.io/a/jobs/1\nCompany\n"
    result = extract_with_fallback(primary, body, None)
    assert result == ["primary-job"]


def test_fallback_skipped_on_empty_body_without_urls() -> None:
    from jobcannon.engine.email_parsers import extract_with_fallback

    result = extract_with_fallback(lambda b, d: [], "no jobs here", None)
    assert result == []


def test_fallback_fires_when_primary_empty_and_url_present() -> None:
    from jobcannon.engine.email_parsers import extract_with_fallback

    body = "Data Engineer\nhttps://jobs.lever.co/acme/xyz\nAcme\n"
    result = extract_with_fallback(lambda b, d: [], body, None)
    # Should get at least one job from fallback
    assert len(result) >= 1
    assert result[0].source == "email_fallback"


def test_fallback_returns_empty_for_body_without_urls() -> None:
    from jobcannon.engine.email_parsers import extract_with_fallback

    result = extract_with_fallback(lambda b, d: [], "random text no urls", None)
    assert result == []

"""PORTED from tests/test_salary_tagging.py @ abedc58ab57db586c74bf22c8a80cb40399deb8b (private job-cannon). Ledger L-0014.

Tests for Phase 49.02 — salary_currency + salary_period emission and plumbing.

Two sections of the private source are NOT carried:
- The m081 migration tests (`job_finder.web.migrations.*`): migrations are
  host-owned, not part of the engine port (see CLAUDE.md "Don't add an ORM"
  / engine-vs-host boundary) — no engine equivalent exists.
- `test_upsert_writes_currency_period` (DB-level, via `job_finder.db.upsert_job`
  against a migrated sqlite DB): ported instead to
  `tests/host/test_upsert_job.py`, which already exercises the real
  Postgres `jobcannon.db._jobs.upsert_job` writer via the `db_conn` fixture
  — the natural home for a persistence assertion, not this pure-function
  engine suite.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._platforms_greenhouse import _posting_to_job
from jobcannon.engine.models import Job
from jobcannon.engine.parsed_job import ParsedJob

# ---------------------------------------------------------------------------
# Greenhouse per-source emission
# ---------------------------------------------------------------------------


def test_greenhouse_emits_hourly_period_and_currency():
    posting = {
        "title": "Data Scientist",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "pay_input_ranges": [
            {"unit": "hour", "min_cents": 64, "max_cents": 90, "currency_type": "USD"}
        ],
    }
    out = _posting_to_job(posting, "acme")
    assert out["salary_period"] == "hourly"
    assert out["salary_currency"] == "USD"
    # P1.3: hourly $64 now annualizes ×2080 → 133,120 (was stored raw as 64).
    assert out["salary_min"] == 133_120


def test_greenhouse_emits_annual_eur():
    posting = {
        "title": "Engineer",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
        "pay_input_ranges": [
            {
                "interval": "year",
                "min_cents": 12_000_000,
                "max_cents": 18_000_000,
                "currency": "EUR",
            }
        ],
    }
    out = _posting_to_job(posting, "acme")
    assert out["salary_period"] == "annual"
    assert out["salary_currency"] == "EUR"
    assert out["salary_min"] == 120_000  # cents → dollars for year


def test_greenhouse_defaults_when_no_pay_ranges():
    posting = {"title": "Analyst", "absolute_url": "https://x/y"}
    out = _posting_to_job(posting, "acme")
    assert out["salary_period"] == "unknown"
    assert out["salary_currency"] == "USD"


def test_greenhouse_unknown_currency_falls_back_to_usd():
    posting = {
        "title": "X",
        "absolute_url": "https://x/y",
        "pay_input_ranges": [{"unit": "hour", "min_cents": 50, "currency": "XYZ"}],
    }
    out = _posting_to_job(posting, "acme")
    assert out["salary_currency"] == "USD"


# ---------------------------------------------------------------------------
# Job -> ParsedJob plumbing
# ---------------------------------------------------------------------------


def test_parsed_job_carries_currency_period_from_job():
    job = Job(
        title="Data Scientist",
        company="Acme",
        location="Remote",
        source="greenhouse",
        source_url="https://acme.com/1",
        salary_min=64,
        salary_max=90,
        salary_currency="EUR",
        salary_period="hourly",
    )
    parsed = ParsedJob.from_job(job)
    assert isinstance(parsed, ParsedJob)
    assert parsed.salary_currency == "EUR"
    assert parsed.salary_period == "hourly"

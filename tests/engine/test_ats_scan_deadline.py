"""Behavior tests for the shared whole-scan deadline threaded through the ATS
scan phases (Phase A ATS-API, Phase C HTML fallback, Phase D scoring).

Each per-company/per-key loop is expected to check
``time.monotonic() >= deadline_monotonic`` at the TOP of its loop and, on
trip, set ``summary["truncated"] = True`` and stop before doing the next
unit of work. ``run_ats_scan`` sources that absolute deadline once: from
``config["ats"]["runtime_limit_s"]`` when set, otherwise falling back to the
harder scoring wall (``services.scan_deadline_s``) so unbounded config still
bounds every phase, not only scoring.

None of these tests sleep. ``time.monotonic`` is replaced with a small
deterministic ``_Ticker`` stand-in via ``monkeypatch`` (auto-restored on
teardown), so a "past deadline" or "clock jumped past the wall" scenario is
expressed as fixed return values rather than a real wall-clock wait.

These tests are written against the target behavior described for the
concurrent scan-deadline port and will fail at CALL time (``TypeError:
unexpected keyword argument``) until that port lands — new
``deadline_monotonic`` parameters on the phase helpers and a new
``scan_deadline_s`` field on ``ScanServices`` — that is expected; the
module itself imports and collects cleanly today.
"""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.ats_scanner._run import (
    _run_ats_api_scan,
    _score_new_ats_jobs,
    run_ats_scan,
)
from jobcannon.engine.ats_scanner._run_html import _run_html_fallback_scan
from tests.engine.helpers.ats_scan_services import create_scan_schema, make_scan_services


class _Ticker:
    """Deterministic ``time.monotonic()`` stand-in.

    Returns values from ``values`` in order; once exhausted, repeats the
    final value forever. Never sleeps, never raises ``StopIteration`` — safe
    to install regardless of how many times the code under test calls
    ``time.monotonic()`` before or after the check being exercised.

    ``self.calls`` logs every value returned, in order — useful for
    diagnosing a red test caused by a wrong assumption about how many
    ``time.monotonic()`` reads precede the check under test, rather than a
    genuine behavior gap in the implementation.
    """

    def __init__(self, values: list[float]) -> None:
        assert values, "_Ticker needs at least one value"
        self._values = list(values)
        self.calls: list[float] = []

    def __call__(self) -> float:
        idx = min(len(self.calls), len(self._values) - 1)
        value = self._values[idx]
        self.calls.append(value)
        return value


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "scan.db")
    conn = sqlite3.connect(path)
    create_scan_schema(conn)
    conn.close()
    return path


def _insert_hit_company(
    conn: sqlite3.Connection, name: str, slug: str, platform: str = "greenhouse"
) -> None:
    conn.execute(
        """INSERT INTO companies
           (name, name_raw, ats_platform, ats_slug, ats_probe_status,
            scan_enabled, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'hit', 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        (name.lower(), name, platform, slug),
    )
    conn.commit()


def _seed_html_eligible_company(conn: sqlite3.Connection, name: str = "Acme") -> None:
    conn.execute(
        """INSERT INTO companies
           (name, name_raw, ats_probe_status, homepage_url, scan_enabled,
            careers_crawl_last_at, created_at, updated_at)
           VALUES (?, ?, 'miss', 'https://acme.example', 1,
                   NULL, '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        (name.lower(), name),
    )
    conn.commit()


def _seed_job(conn: sqlite3.Connection, dedup_key: str, **cols) -> None:
    fields = {"dedup_key": dedup_key, "title": "Engineer", "company": "Acme", **cols}
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO jobs ({', '.join(fields)}) VALUES ({placeholders})",
        list(fields.values()),
    )
    conn.commit()


def _base_summary() -> dict:
    return {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}


# ---------------------------------------------------------------------------
# 1. _run_ats_api_scan: deadline already past -> zero companies scanned.
# ---------------------------------------------------------------------------


def test_run_ats_api_scan_past_deadline_scans_zero_companies(db_path, monkeypatch):
    """A deadline already in the past trips the check at the top of the
    serial per-company loop, before the first eligible company is ever
    dispatched to the platform scanner."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_hit_company(conn, "AcmeCo", "acme")

    monkeypatch.setattr(time, "monotonic", _Ticker([1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    summary = _base_summary()
    all_new_job_keys: list = []

    services.set_services(make_scan_services(db_path))
    try:
        with patch("jobcannon.engine.ats_scanner._run.run_platform_scan") as mock_scan:
            _run_ats_api_scan(
                conn,
                db_path,
                ["Engineer"],
                [],
                summary,
                all_new_job_keys,
                high_score_threshold=999,
                dormancy_threshold=10,
                dormancy_interval_days=3,
                tracker=None,
                company_names=None,
                workday_max_pages=None,
                scan_concurrency=1,
                # Positive and well in the past — trips both an `is not
                # None` guard and a bare truthiness guard (0.0 would pass
                # `deadline_monotonic and ...` as falsy and defeat the test
                # for a reason unrelated to the behavior under test; see the
                # `runtime_limit_s` <= 0 "previously truthy" bug this file's
                # test 5 guards below).
                deadline_monotonic=100.0,
            )
    finally:
        services.clear_services()
    conn.close()

    assert mock_scan.call_count == 0, (
        "no company should have been dispatched to the platform scanner past the deadline"
    )
    assert summary["companies_scanned"] == 0
    assert summary.get("truncated") is True


# ---------------------------------------------------------------------------
# 2. _score_new_ats_jobs: deadline already past -> scored < len(keys).
# ---------------------------------------------------------------------------


def test_score_new_ats_jobs_past_deadline_scores_fewer_than_keys(db_path, monkeypatch):
    """A past deadline truncates Phase D before all queued keys are scored —
    mirrors the private regression's assertion shape: with 3 keys queued,
    scored must land strictly below 3, and keys after the trip are never
    handed to score_and_persist_job."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    keys = ["dk1", "dk2", "dk3"]
    for dk in keys:
        _seed_job(conn, dk)

    scored_calls: list = []

    def fake_score_and_persist_job(job_row, conn, config):
        scored_calls.append(job_row["dedup_key"])
        return None

    monkeypatch.setattr(time, "monotonic", _Ticker([1_000_000.0]))

    services.set_services(
        make_scan_services(db_path, score_and_persist_job=fake_score_and_persist_job)
    )
    try:
        summary: dict = {"classified_apply": 0, "classified_consider": 0, "errors": []}
        # Positive past value — see test 1's comment on why 0.0 is unsafe.
        _score_new_ats_jobs(conn, {}, keys, summary, deadline_monotonic=100.0)
    finally:
        services.clear_services()
    conn.close()

    assert summary.get("truncated") is True
    assert summary.get("scored", 0) < len(keys), (
        f"expected scored < {len(keys)}, got {summary.get('scored')}"
    )
    assert len(scored_calls) < len(keys), (
        "score_and_persist_job must not be called for every key past the deadline"
    )


# ---------------------------------------------------------------------------
# 3. _run_html_fallback_scan: deadline already past -> zero companies scraped.
# ---------------------------------------------------------------------------


def test_run_html_fallback_scan_past_deadline_scans_zero_companies(db_path, monkeypatch):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_html_eligible_company(conn)

    find_calls: list = []
    scrape_calls: list = []

    def _fake_find_careers_url(homepage_url, *, conn=None, config=None):
        find_calls.append(homepage_url)
        return "https://acme.example/careers"

    def _fake_scrape_careers_page(
        careers_url, target_titles, title_exclusions, *, conn=None, config=None
    ):
        scrape_calls.append(careers_url)
        return [], 0

    monkeypatch.setattr(time, "monotonic", _Ticker([1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    services.set_services(
        make_scan_services(
            db_path,
            find_careers_url=_fake_find_careers_url,
            scrape_careers_page=_fake_scrape_careers_page,
        )
    )
    try:
        summary: dict = {"jobs_new": 0, "errors": [], "html_scraped": 0}
        _run_html_fallback_scan(
            conn,
            db_path,
            {},
            ["Engineer"],
            [],
            summary,
            [],
            high_score_threshold=999,
            # Positive past value — see test 1's comment on why 0.0 is unsafe.
            deadline_monotonic=100.0,
        )
    finally:
        services.clear_services()
    conn.close()

    assert find_calls == [], "find_careers_url must not run once the deadline has passed"
    assert scrape_calls == [], "scrape_careers_page must not run once the deadline has passed"
    assert summary.get("truncated") is True
    assert summary["html_scraped"] == 0


# ---------------------------------------------------------------------------
# 4. run_ats_scan sourcing: config.ats.runtime_limit_s unset falls back to
#    services.scan_deadline_s, and that wall bounds sourcing phases too.
# ---------------------------------------------------------------------------


def test_run_ats_scan_falls_back_to_scan_deadline_s_for_sourcing(db_path, monkeypatch):
    """With no ``ats.runtime_limit_s`` in config, the harder scoring wall
    (``services.scan_deadline_s``) must still bound Phase A once it fires —
    not just Phase D scoring. The clock's first read establishes the
    deadline baseline; every subsequent read lands far past it, regardless
    of exactly how many baseline reads happen before Phase A's own check."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_hit_company(conn, "AcmeCo", "acme")
    conn.close()

    monkeypatch.setattr(time, "monotonic", _Ticker([0.0, 1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    services.set_services(make_scan_services(db_path, scan_deadline_s=5.0))
    try:
        with patch("jobcannon.engine.ats_scanner._run.run_platform_scan") as mock_scan:
            config = {
                "profile": {
                    "target_titles": ["Engineer"],
                    "exclusions": {"title_keywords": []},
                },
            }
            summary = run_ats_scan(db_path, config)
    finally:
        services.clear_services()

    assert summary.get("truncated") is True
    assert mock_scan.call_count == 0, (
        "Phase A must not scan any company once the shared wall has fired"
    )
    assert summary["companies_scanned"] == 0


# ---------------------------------------------------------------------------
# 5. Normalization: scan_deadline_s <= 0 on the seam means NO bound.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("non_positive_deadline_s", [0, -5.0])
def test_scan_deadline_s_non_positive_means_no_bound(db_path, monkeypatch, non_positive_deadline_s):
    """A ``scan_deadline_s`` of 0 OR negative must normalize to "no
    deadline" end-to-end — the scan completes and no truncated flag is set,
    even though the clock jumps by an amount that would trip any real
    deadline. This guards the same off-by-truthiness class of bug this
    codebase has already hit once for ``ats.runtime_limit_s`` (a <= 0 value
    must not be treated as truthy/enabled) — the negative case is the one
    the historical bug actually was; 0 alone would only cover half of it."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_hit_company(conn, "AcmeCo", "acme")
    conn.close()

    monkeypatch.setattr(time, "monotonic", _Ticker([0.0, 999_999.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    services.set_services(make_scan_services(db_path, scan_deadline_s=non_positive_deadline_s))
    try:
        with patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=([], 0, []),
        ) as mock_scan:
            config = {
                "profile": {
                    "target_titles": ["Engineer"],
                    "exclusions": {"title_keywords": []},
                },
            }
            summary = run_ats_scan(db_path, config)
    finally:
        services.clear_services()

    assert not summary.get("truncated"), "scan_deadline_s <= 0 must not bound the scan"
    assert mock_scan.call_count == 1
    assert summary["companies_scanned"] == 1

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

These tests were written test-first against the scan-deadline port, which
has since landed: the phase helpers take ``deadline_monotonic`` and
``ScanServices`` carries ``scan_deadline_s``. They now pin that behavior
against regression.
"""

from __future__ import annotations

import sqlite3
import sys
import time
import types
from unittest.mock import patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.ats_scanner._run import (
    _CompanyScanResult,
    _run_ats_api_scan,
    _score_new_ats_jobs,
    run_ats_scan,
)
from jobcannon.engine.ats_scanner._run_html import _run_html_fallback_scan
from jobcannon.engine.ats_scanner._run_playwright import _run_playwright_scan
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
    """A past deadline truncates Phase D before all queued keys are scored:
    with 3 keys queued, keys after the trip are never handed to
    score_and_persist_job. The ``summary["scored"]`` counter is pinned by
    test_score_new_ats_jobs_scores_exactly_the_keys_before_the_deadline
    below, whose fake returns a real result — this test's fake returns None,
    under which ``scored`` never increments, so asserting on it here would be
    dead weight."""
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
# 4b. run_ats_scan sourcing: BOTH the config knob and the services wall set —
#     the knob keeps authority over the sourcing phases (no fallback taken).
# ---------------------------------------------------------------------------


def test_run_ats_scan_knob_keeps_authority_when_wall_also_set(db_path, monkeypatch):
    """With ``ats.runtime_limit_s`` AND ``services.scan_deadline_s`` both set,
    the sourcing deadline must come from the knob, not the wall. The knob is
    short (5s) and the wall enormous (1_000_000s); the clock's post-baseline
    reads land past the knob but far short of any wall-derived deadline, so
    Phase A trips iff the knob kept authority. An implementation that let the
    fallback overwrite the knob (or sourced phases A/A2/C from the wall when
    both are set) sees no trip and goes red here."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_hit_company(conn, "AcmeCo", "acme")
    conn.close()

    # First read 0.0 (deadline baseline), every later read 1_000.0 — past the
    # 5s knob, nowhere near the 1_000_000s wall, regardless of how many
    # baseline reads the sourcing block performs.
    monkeypatch.setattr(time, "monotonic", _Ticker([0.0, 1_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    services.set_services(make_scan_services(db_path, scan_deadline_s=1_000_000.0))
    try:
        with patch("jobcannon.engine.ats_scanner._run.run_platform_scan") as mock_scan:
            config = {
                "ats": {"runtime_limit_s": 5},
                "profile": {
                    "target_titles": ["Engineer"],
                    "exclusions": {"title_keywords": []},
                },
            }
            summary = run_ats_scan(db_path, config)
    finally:
        services.clear_services()

    assert summary.get("truncated") is True, (
        "the 5s knob must bound Phase A even though the wall is far away"
    )
    assert mock_scan.call_count == 0
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


# ---------------------------------------------------------------------------
# 6. Phase A CONCURRENT branch: deadline already past -> nothing submitted,
#    plus a positive control proving the absence assertion can fail.
#
#    The seam is _scan_one_company_worker — the callable the concurrent branch
#    actually submits. (run_platform_scan is the SERIAL branch's seam; patching
#    it here would silently never fire, making any call-count assertion
#    vacuously true.)
#
#    Deliberately NOT covered here: the deadline tripping while futures are
#    already queued. That path currently deadlocks — shutdown(wait=False,
#    cancel_futures=True) leaves drained futures in CANCELLED, which
#    as_completed() never counts as done — so no passing test can pin it until
#    the defect is fixed. Reproduction tests live in the tracking issue.
# ---------------------------------------------------------------------------


def _insert_hit_companies(conn: sqlite3.Connection, count: int) -> None:
    platforms = ["greenhouse", "lever", "ashby"]
    for i in range(count):
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'hit', 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
            (f"co{i}", f"Co{i}", platforms[i % 3], f"co{i}"),
        )
    conn.commit()


def _run_concurrent(db_path, summary, *, deadline, worker, concurrency=3):
    """Drive the Phase A CONCURRENT branch with the worker task stubbed out."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    services.set_services(make_scan_services(db_path))
    try:
        with patch(
            "jobcannon.engine.ats_scanner._run._scan_one_company_worker",
            side_effect=worker,
        ):
            _run_ats_api_scan(
                conn,
                db_path,
                ["Engineer"],
                [],
                summary,
                [],
                high_score_threshold=999,
                dormancy_threshold=10,
                dormancy_interval_days=3,
                tracker=None,
                company_names=None,
                workday_max_pages=None,
                scan_concurrency=concurrency,
                deadline_monotonic=deadline,
            )
    finally:
        services.clear_services()
        conn.close()


def _ok_result(company, *_a, **_k):
    return _CompanyScanResult(
        company_name=company["name_raw"],
        jobs_discovered=0,
        jobs_new=[],
        skipped_title_filter=0,
    )


def test_concurrent_path_past_deadline_submits_no_company(db_path, monkeypatch):
    conn = sqlite3.connect(db_path)
    _insert_hit_companies(conn, 4)
    conn.close()

    monkeypatch.setattr(time, "monotonic", _Ticker([1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    calls: list = []

    def worker(company, *a, **kw):
        calls.append(company["name_raw"])
        return _ok_result(company)

    summary = _base_summary()
    _run_concurrent(db_path, summary, deadline=100.0, worker=worker)

    assert calls == []
    assert summary["companies_scanned"] == 0
    assert summary.get("truncated") is True


def test_concurrent_path_positive_control_scans_every_company(db_path, monkeypatch):
    """Positive control for the test above: the SAME assertions must be able
    to fail. With the deadline far away every company is scanned, so
    ``calls == []`` is a real constraint rather than a vacuous one.
    """
    conn = sqlite3.connect(db_path)
    _insert_hit_companies(conn, 4)
    conn.close()

    monkeypatch.setattr(time, "monotonic", _Ticker([0.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    calls: list = []

    def worker(company, *a, **kw):
        calls.append(company["name_raw"])
        return _ok_result(company)

    summary = _base_summary()
    _run_concurrent(db_path, summary, deadline=1_000_000.0, worker=worker)

    assert len(calls) == 4
    assert summary["companies_scanned"] == 4
    assert not summary.get("truncated")


# ---------------------------------------------------------------------------
# 7. Phase A2 (Playwright): deadline already past -> zero companies scanned.
#    Playwright is an optional extra, so sync_playwright is injected as a
#    fake; the browser.closed assertion also pins the "browser still closes
#    in finally on truncation" property.
# ---------------------------------------------------------------------------


def _seed_icims_companies(conn: sqlite3.Connection, count: int) -> None:
    for i in range(count):
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                scan_enabled, created_at, updated_at)
               VALUES (?, ?, 'icims', ?, 'hit', 1,
                       '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
            (f"icims{i}", f"Icims{i}", f"icims{i}"),
        )
    conn.commit()


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakePw:
    def __init__(self, browser):
        self.chromium = types.SimpleNamespace(launch=lambda **kw: browser)


class _FakeSyncPlaywright:
    def __init__(self, browser):
        self._browser = browser

    def __call__(self):
        return self

    def __enter__(self):
        return _FakePw(self._browser)

    def __exit__(self, *exc):
        return False


def test_run_playwright_scan_past_deadline_scans_zero_companies(db_path, monkeypatch):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_icims_companies(conn, 2)

    browser = _FakeBrowser()
    fake_mod = types.ModuleType("playwright.sync_api")
    fake_mod.sync_playwright = _FakeSyncPlaywright(browser)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_mod)

    scanned_calls: list = []
    monkeypatch.setattr(
        "jobcannon.engine.ats_scanner._run_playwright._scan_one_company_via_playwright",
        lambda *a, **kw: scanned_calls.append(a[2]),
    )
    monkeypatch.setattr(time, "monotonic", _Ticker([1_000_000.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    summary = _base_summary()
    services.set_services(make_scan_services(db_path))
    try:
        _run_playwright_scan(
            conn,
            db_path,
            {},
            ["Engineer"],
            [],
            summary,
            [],
            high_score_threshold=999,
            deadline_monotonic=100.0,
        )
    finally:
        services.clear_services()
    conn.close()

    assert scanned_calls == []
    assert summary.get("truncated") is True
    assert browser.closed is True


# ---------------------------------------------------------------------------
# 8. Sourcing vs scoring: the two deadlines are DISTINCT when the soft knob
#    is set. Asserted at the wiring level (which value each phase receives)
#    rather than through observable truncation, because producing real
#    Phase-D-eligible jobs would couple this test to the whole upsert path
#    for no extra signal.
# ---------------------------------------------------------------------------


def test_sourcing_and_scoring_receive_distinct_deadlines(db_path, monkeypatch):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_hit_companies(conn, 1)
    conn.close()

    # Read 1 -> scan_deadline baseline (0.0 + 5 = 5.0)
    # Read 2 -> score_deadline baseline (0.0 + 100_000 = 100_000.0)
    monkeypatch.setattr(time, "monotonic", _Ticker([0.0, 0.0, 1.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    phase_a_deadline: list = []
    score_deadline_seen: list = []

    def _capture_phase_a(*args, **kwargs):
        phase_a_deadline.append(kwargs.get("deadline_monotonic"))

    def _capture_score(conn, config, keys, summary, deadline_monotonic=None):
        score_deadline_seen.append(deadline_monotonic)

    services.set_services(make_scan_services(db_path, scan_deadline_s=100_000.0))
    try:
        with (
            patch("jobcannon.engine.ats_scanner._run._run_ats_api_scan", _capture_phase_a),
            patch("jobcannon.engine.ats_scanner._run._score_new_ats_jobs", _capture_score),
        ):
            run_ats_scan(
                db_path,
                {
                    "ats": {"runtime_limit_s": 5},
                    "profile": {
                        "target_titles": ["Engineer"],
                        "exclusions": {"title_keywords": []},
                    },
                },
            )
    finally:
        services.clear_services()

    assert phase_a_deadline == [5.0], (
        "sourcing phases must be bounded by ats.runtime_limit_s when it is set"
    )
    assert score_deadline_seen == [100_000.0], (
        "scoring must be bounded by the services wall, NOT by the softer sourcing "
        "knob — the knob exists precisely to reserve scoring headroom"
    )


# ---------------------------------------------------------------------------
# 9. Normalization of the CONFIG knob: ats.runtime_limit_s <= 0 means "no
#    limit" and falls back to the wall — the config-knob twin of test 5's
#    services-field case, and the field the historical truthiness bug was
#    actually on. The wall is set far away so the branches are
#    distinguishable: normalized -> unbounded -> the company scans;
#    unnormalized -> a negative is truthy -> the deadline is instantly past.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("non_positive", [0, -5])
def test_non_positive_runtime_limit_s_falls_back_to_the_wall(db_path, monkeypatch, non_positive):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_hit_companies(conn, 1)
    conn.close()

    monkeypatch.setattr(time, "monotonic", _Ticker([0.0, 0.0, 10.0]))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)

    services.set_services(make_scan_services(db_path, scan_deadline_s=100_000.0))
    try:
        with patch(
            "jobcannon.engine.ats_scanner._run.run_platform_scan",
            return_value=([], 0, []),
        ) as mock_scan:
            summary = run_ats_scan(
                db_path,
                {
                    "ats": {"runtime_limit_s": non_positive},
                    "profile": {
                        "target_titles": ["Engineer"],
                        "exclusions": {"title_keywords": []},
                    },
                },
            )
    finally:
        services.clear_services()

    assert not summary.get("truncated"), (
        "a <= 0 ats.runtime_limit_s must normalize to 'no limit' and fall back to the "
        "wall — treating a negative as truthy makes the deadline instantly past and "
        "silently truncates every phase at the first company"
    )
    assert mock_scan.call_count == 1


# ---------------------------------------------------------------------------
# 10. Phase D scored count is REAL: the fake returns an actual result so
#     scored_count increments, making `scored` a live value pinned at exact
#     counts instead of an inequality that holds at zero.
# ---------------------------------------------------------------------------


def test_score_new_ats_jobs_scores_exactly_the_keys_before_the_deadline(db_path, monkeypatch):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    keys = ["dk1", "dk2", "dk3"]
    for dk in keys:
        _seed_job(conn, dk)

    scored_calls: list = []

    def fake_score_and_persist_job(job_row, conn, config):
        scored_calls.append(job_row["dedup_key"])
        return {"classification": "consider"}

    # First key is scored (clock still inside), then the deadline passes.
    monkeypatch.setattr(time, "monotonic", _Ticker([0.0, 1_000_000.0]))

    services.set_services(
        make_scan_services(db_path, score_and_persist_job=fake_score_and_persist_job)
    )
    try:
        summary: dict = {"classified_apply": 0, "classified_consider": 0, "errors": []}
        _score_new_ats_jobs(conn, {}, keys, summary, deadline_monotonic=100.0)
    finally:
        services.clear_services()
    conn.close()

    assert summary.get("truncated") is True
    assert scored_calls == ["dk1"], "exactly the keys reached before the deadline are scored"
    assert summary.get("scored") == 1

"""Ported tests for ATS board-level scan concurrency.

Mechanical port of the private repo's tests/test_ats_scan_concurrency.py
(6 test cases) onto the jobcannon.engine ScanServices DI seam. Substitutions
from the private source:

- job_finder.web.db_migrate.run_migrations does not port (the engine has no
  migrations system — host-owned, not ported) -> replaced by
  tests/engine/helpers/ats_scan_services.py's create_scan_schema(), the
  minimal companies/jobs/company_scan_log subset _run.py's Phase A SQL
  actually references (same helper test_ats_scanner_run.py already uses —
  see that helper module's docstring, not a full migrated DB).
- job_finder.web.db_helpers.standalone_connection (a private-repo-only
  module-level connection opener that _run.py imported directly) has no
  engine equivalent; the ported _run.py opens connections exclusively via
  the injected ScanServices.connection_factory seam instead
  (helpers.ats_scan_services.make_connection_factory /  open_connection).
  Tests that need to observe connection-opening behavior (thread IDs,
  cross-thread violations) wrap that factory rather than patching a symbol
  that no longer exists.
- job_finder.db._jobs.upsert_job / job_finder.db._jd_full.set_jd_full are
  host-owned (see ScanServices docstrings) and don't port; the equivalence
  test below uses helpers.ats_scan_services.make_scan_services()'s real
  (not no-op) dedup_key-keyed fakes, which are faithful enough to exercise
  the real comp_data_json / is_remote / employment_type / department /
  jd_full UPDATE paths in _upsert_one_ats_api_job — those run unconditionally
  in _run.py itself, not inside the injected hooks.
- All DBs here are file-backed temp files (via tmp_path), never
  ``:memory:`` — each worker thread opens its OWN connection via
  connection_factory, and separate ``sqlite3.connect(":memory:")`` calls
  never share state across connections/threads.

Tests:
- Default (scan_concurrency=1) preserves the exact serial behavior,
  including the 0.5s inter-company sleep.
- scan_concurrency > 1 dispatches company scans across a bounded thread
  pool (recorded-concurrency-with-overlap: max simultaneous workers == the
  bound, not 1 and not more).
- get_scan_concurrency clamps out-of-range config values to [1, 6].
- Worker threads never touch the orchestrator's own sqlite3 connection
  (each opens its own via connection_factory) — thread-identity capture on
  a spied orchestrator conn.
- Serial vs. concurrent equivalence: both dispatch paths must land
  identical summary totals and DB state for the same input.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.ats_platforms._concurrency import get_scan_concurrency
from jobcannon.engine.ats_platforms._registry import PlatformScanner
from jobcannon.engine.ats_scanner._run import _run_ats_api_scan
from tests.engine.helpers.ats_scan_services import (
    create_scan_schema,
    make_scan_services,
    open_connection,
)


@pytest.fixture
def scan_db_path(tmp_path) -> str:
    """Stand-in for the private repo's ``migrated_db_path`` fixture (real
    ``db_migrate.run_migrations()`` is host-owned and does not port) — a real
    on-disk sqlite3 DB with the shared minimal ats_scanner schema."""
    path = str(tmp_path / "scan.db")
    conn = sqlite3.connect(path)
    create_scan_schema(conn)
    conn.close()
    return path


def _insert_test_companies(path: str, count: int = 3) -> list[dict]:
    """Insert test companies with different platforms for concurrency testing."""
    companies = []
    platforms = ["greenhouse", "lever", "ashby"]
    with open_connection(path) as conn:
        for i in range(count):
            platform = platforms[i % len(platforms)]
            cur = conn.execute(
                """INSERT INTO companies
                   (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                    scan_enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'hit', 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
                (f"Company{i}", f"Company{i}", platform, f"company{i}"),
            )
            companies.append({"id": cur.lastrowid, "name": f"Company{i}", "platform": platform})
        conn.commit()
    return companies


def _base_summary() -> dict:
    return {
        "companies_scanned": 0,
        "jobs_discovered": 0,
        "jobs_new": 0,
        "errors": [],
    }


def _tracking_connection_factory(db_path: str, thread_ids: list[int]):
    """Wraps the shared helper's open_connection to also record
    threading.get_ident() on every open — stands in for the private test's
    patch on db_helpers.standalone_connection (no engine equivalent; the
    ported _run.py calls svc.connection_factory directly instead)."""

    @contextlib.contextmanager
    def factory(*, synchronous: str = "FULL"):
        thread_ids.append(threading.get_ident())
        with open_connection(db_path, synchronous=synchronous) as conn:
            yield conn

    return factory


def test_scan_concurrency_clamps_to_range():
    """get_scan_concurrency floors at 1 and caps at 6 (matches sibling knobs)."""
    cases = [
        ({"ats": {"scan_concurrency": 0}}, 1),  # below floor
        ({"ats": {"scan_concurrency": -5}}, 1),  # negative
        ({"ats": {"scan_concurrency": 1}}, 1),  # minimum valid
        ({}, 1),  # ats section missing entirely -> default
        ({"ats": {"scan_concurrency": 4}}, 4),  # recommended default
        ({"ats": {"scan_concurrency": 6}}, 6),  # maximum valid
        ({"ats": {"scan_concurrency": 10}}, 6),  # above ceiling
        ({"ats": {"scan_concurrency": 1000}}, 6),  # way above ceiling
        ({"ats": {"scan_concurrency": "bogus"}}, 1),  # invalid type -> default
    ]
    for config, expected in cases:
        assert get_scan_concurrency(config) == expected, (
            f"config={config}, expected={expected}, got={get_scan_concurrency(config)}"
        )


def test_default_scan_concurrency_preserves_serial_behavior(scan_db_path):
    """scan_concurrency=1 (the default) takes the serial path with the 0.5s sleep."""
    _insert_test_companies(scan_db_path, count=2)

    summary = _base_summary()
    all_new_job_keys: list[str] = []

    with open_connection(scan_db_path) as conn:
        with patch("jobcannon.engine.ats_scanner._run._scan_one_company_via_ats_api") as mock_scan:
            mock_scan.return_value = None

            start = time.monotonic()
            _run_ats_api_scan(
                conn,
                scan_db_path,
                ["Engineer"],
                [],
                summary,
                all_new_job_keys,
                high_score_threshold=999,  # skip history gate
                dormancy_threshold=10,
                dormancy_interval_days=3,
                tracker=None,
                company_names=None,
                workday_max_pages=None,
                scan_concurrency=1,
            )
            elapsed = time.monotonic() - start

    assert elapsed >= 1.0, f"serial path must sleep 0.5s per company, took {elapsed}s"
    assert mock_scan.call_count == 2


def test_concurrent_path_uses_multiple_threads(scan_db_path):
    """scan_concurrency > 1 dispatches company scans across worker threads."""
    _insert_test_companies(scan_db_path, count=4)

    summary = _base_summary()
    all_new_job_keys: list[str] = []
    thread_ids: list[int] = []

    services.set_services(
        make_scan_services(
            scan_db_path,
            connection_factory=_tracking_connection_factory(scan_db_path, thread_ids),
        )
    )
    try:
        with patch("jobcannon.engine.ats_scanner._run.run_platform_scan") as mock_scan:
            mock_scan.return_value = ([], 0, [])

            with open_connection(scan_db_path) as conn:
                _run_ats_api_scan(
                    conn,
                    scan_db_path,
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
                    scan_concurrency=2,
                )
    finally:
        services.clear_services()

    assert len(set(thread_ids)) > 1, (
        f"expected multiple worker threads, got {len(set(thread_ids))} unique IDs "
        f"from {len(thread_ids)} connection_factory() calls"
    )
    assert summary["companies_scanned"] == 4


def test_scan_concurrency_bound_respected_with_overlap(scan_db_path):
    """Recorded-concurrency-with-overlap: max simultaneous company workers == the bound.

    8 companies, scan_concurrency=3: max_concurrent must land at exactly 3 —
    not 1 (the pool never actually overlapped, i.e. accidentally serial) and
    not >3 (the bound was ignored).
    """
    _insert_test_companies(scan_db_path, count=8)

    summary = _base_summary()
    all_new_job_keys: list[str] = []
    tracker = {"active": 0, "max_concurrent": 0, "lock": threading.Lock()}

    def _scan(*_args, **_kwargs):
        with tracker["lock"]:
            tracker["active"] += 1
            tracker["max_concurrent"] = max(tracker["max_concurrent"], tracker["active"])
        time.sleep(0.05)
        with tracker["lock"]:
            tracker["active"] -= 1
        return ([], 0, [])

    services.set_services(make_scan_services(scan_db_path))
    try:
        with open_connection(scan_db_path) as conn:
            with patch("jobcannon.engine.ats_scanner._run.run_platform_scan", side_effect=_scan):
                _run_ats_api_scan(
                    conn,
                    scan_db_path,
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
                    scan_concurrency=3,
                )
    finally:
        services.clear_services()

    assert tracker["max_concurrent"] == 3, (
        f"expected exactly 3 concurrent company workers (scan_concurrency bound), "
        f"got {tracker['max_concurrent']}"
    )
    assert summary["companies_scanned"] == 8


class _ThreadGuardConn:
    """Proxy around a real sqlite3.Connection that flags any .execute() call
    made from a thread other than the one that constructed it.

    sqlite3.Connection is an immutable extension type in this interpreter
    build (neither per-instance nor per-class attribute assignment is
    possible), so the guard has to live at the call-site boundary instead —
    this wrapper is passed to _run_ats_api_scan in place of the real conn,
    and forwards everything else straight through via __getattr__.
    """

    def __init__(self, real_conn: sqlite3.Connection, violations: list[int]) -> None:
        object.__setattr__(self, "_real", real_conn)
        object.__setattr__(self, "_owner_thread_id", threading.get_ident())
        object.__setattr__(self, "_violations", violations)

    def execute(self, *args, **kwargs):
        if threading.get_ident() != self._owner_thread_id:
            self._violations.append(threading.get_ident())
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_concurrent_workers_never_touch_orchestrator_connection(scan_db_path):
    """Thread-identity capture: nothing may call .execute() on the orchestrator's
    own conn from a worker thread — each worker must open its own connection
    via svc.connection_factory (a real cross-thread conn share would raise
    sqlite3.ProgrammingError; this test would surface that as a captured
    company error even if the guard below didn't fire first).
    """
    _insert_test_companies(scan_db_path, count=6)

    summary = _base_summary()
    all_new_job_keys: list[str] = []
    violations: list[int] = []

    services.set_services(make_scan_services(scan_db_path))
    try:
        with open_connection(scan_db_path) as real_conn:
            guarded_conn = _ThreadGuardConn(real_conn, violations)

            with patch("jobcannon.engine.ats_scanner._run.run_platform_scan") as mock_scan:
                mock_scan.return_value = ([], 0, [])
                _run_ats_api_scan(
                    guarded_conn,  # type: ignore[arg-type]
                    scan_db_path,
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
                    scan_concurrency=3,
                )
    finally:
        services.clear_services()

    assert violations == [], (
        f"orchestrator conn.execute() was called from worker thread(s): {violations}"
    )
    assert summary["errors"] == []
    assert summary["companies_scanned"] == 6


# ---------------------------------------------------------------------------
# Serial vs. concurrent equivalence (Tests requirement, review
# finding M4)
# ---------------------------------------------------------------------------
# The serial path (_scan_one_company_via_ats_api) uses the orchestrator's own
# `conn` for the jd_full/comp-data UPDATEs and a short-lived `scan_conn`
# (opened via svc.connection_factory) for upsert_job, while the worker path
# (_scan_one_company_worker) uses a single `worker_conn` for both. Nothing
# above proves those two connection-usage patterns land the same bytes on
# disk — this test drives BOTH real functions (via the real _run_ats_api_scan
# dispatcher, no mocking of either scan-one-company function) against two
# freshly seeded, identically shaped DBs and diffs the resulting state.

_EQUIVALENCE_DESCRIPTION = (
    "<p>We are looking for an experienced engineer to join our platform "
    "team. You will design, build, and operate distributed systems that "
    "power our core product, partner closely with product and design, and "
    "mentor other engineers on the team. Strong communication skills and a "
    "track record of shipping production software are required.</p>"
)


def _equivalence_postings() -> list[dict]:
    """Deterministic single-posting fixture, identical on every call.

    Shaped as the final job-dict (not a raw platform payload) since the
    fixture scanner's posting_to_job is the identity function — carries
    description/comp/structured fields so the jd_full promotion UPDATE
    (conn) and the comp_data_json / is_remote / employment_type / department
    UPDATEs (conn, first-seen-wins) in _upsert_one_ats_api_job are exercised,
    not just the INSERT branch of upsert_job.
    """
    return [
        {
            "title": "Senior Engineer",
            "company_source": "Fixture",
            "location": "Remote",
            "description": _EQUIVALENCE_DESCRIPTION,
            "source_url": "https://example.test/jobs/1",
            "source_id": "1",
            "salary_min": 150000,
            "salary_max": 200000,
            "salary_currency": "USD",
            "salary_period": "annual",
            "comp_json": '{"equity": "0.05%-0.10%", "bonus_target_pct": 10}',
            "is_remote": True,
            "employment_type": "full_time",
            "department": "Engineering",
            "posted_date": "2026-01-01",
        }
    ]


def _make_fixture_scanner(
    name: str,
    company_source: str,
    *,
    postings: list[dict] | None = None,
    raises: Exception | None = None,
) -> PlatformScanner:
    """Real PlatformScanner whose fetch_postings is a deterministic fixture
    (or raises). This is the "platform scanner network layer" mock point —
    run_platform_scan, _scan_one_company_via_ats_api, and
    _scan_one_company_worker all run for real against it.
    """

    def _fetch(_slug, max_pages=None):
        if raises is not None:
            raise raises
        return list(postings or [])

    def _title_of(posting: dict) -> str:
        return posting.get("title", "")

    def _posting_to_job(posting: dict, _slug: str) -> dict:
        return dict(posting)

    return PlatformScanner(
        name=name,
        company_source=company_source,
        fetch_postings=_fetch,
        title_of=_title_of,
        posting_to_job=_posting_to_job,
    )


def _insert_equivalence_companies(path: str, slug_prefix: str) -> None:
    """Insert the 3-company fixture (greenhouse/lever/ashby; ashby is the
    error company) with a slug_prefix unique to this DB.

    Company name/name_raw (which feed dedup_key) are identical across both
    DBs so matching jobs land on the same dedup_key; only ats_slug differs,
    which keeps run_platform_scan's process-wide (scanner.name, slug) memo
    from serving the concurrent run's fetch out of the serial run's cache
    entry — each path independently calls the fixture's fetch_postings.
    """
    platforms = ["greenhouse", "lever", "ashby"]
    with open_connection(path) as conn:
        for i, platform in enumerate(platforms):
            conn.execute(
                """INSERT INTO companies
                   (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                    scan_enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'hit', 1, '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
                (f"Company{i}", f"Company{i}", platform, f"{slug_prefix}-company{i}"),
            )
        conn.commit()


def _run_equivalence_scan(db_path: str, slug_prefix: str, scan_concurrency: int) -> dict:
    """Seed one fresh DB with the 3-company fixture and run Phase A once."""
    _insert_equivalence_companies(db_path, slug_prefix)

    summary = _base_summary()
    all_new_job_keys: list[str] = []

    scanners = {
        "greenhouse": _make_fixture_scanner(
            "greenhouse", "Greenhouse", postings=_equivalence_postings()
        ),
        "lever": _make_fixture_scanner("lever", "Lever", postings=_equivalence_postings()),
        "ashby": _make_fixture_scanner(
            "ashby", "Ashby", raises=RuntimeError("simulated ashby API failure")
        ),
    }

    # make_scan_services()'s default upsert_job/set_jd_full are real (not
    # no-op) dedup_key-keyed fakes against this same db_path — see
    # tests/engine/helpers/ats_scan_services.py.
    services.set_services(make_scan_services(db_path))
    try:
        with patch("jobcannon.engine.ats_scanner._run._PLATFORM_SCANNERS", scanners):
            with open_connection(db_path) as conn:
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
                    scan_concurrency=scan_concurrency,
                )
    finally:
        services.clear_services()

    return summary


def _dump_jobs(db_path: str) -> dict[str, dict]:
    """dedup_key -> the columns the jd_full/comp-data UPDATE paths touch."""
    with open_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT dedup_key, jd_full, comp_data_json, is_remote, employment_type, "
            "department FROM jobs"
        ).fetchall()
    return {
        row["dedup_key"]: {
            "jd_full": row["jd_full"],
            "comp_data_json": row["comp_data_json"],
            "is_remote": row["is_remote"],
            "employment_type": row["employment_type"],
            "department": row["department"],
        }
        for row in rows
    }


def _dump_scan_log(db_path: str) -> list[tuple]:
    """(company_id, jobs_found, skipped_title_filter, error), sorted —
    company_id is comparable across the two DBs because both are freshly
    seeded with the same 3-row INSERT order (SQLite AUTOINCREMENT assigns
    1/2/3 identically); scanned_at (a timestamp) and row order are ignored.
    """
    with open_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT company_id, jobs_found, skipped_title_filter, error "
            "FROM company_scan_log ORDER BY company_id"
        ).fetchall()
    return sorted(
        (row["company_id"], row["jobs_found"], row["skipped_title_filter"], row["error"])
        for row in rows
    )


def test_serial_and_concurrent_paths_are_equivalent(tmp_path):
    """Equivalence test (Tests requirement): scan_concurrency=1
    (serial path) and scan_concurrency=3 (worker-pool path) must produce
    identical DB outcomes — summary totals, the set of upserted job
    dedup_keys, the jd_full/comp-data UPDATE columns on those jobs, and the
    company_scan_log rows (status/error per company) — given the same input
    data, including one company whose scan raises.
    """
    db_serial = str(tmp_path / "serial.db")
    db_concurrent = str(tmp_path / "concurrent.db")
    for path in (db_serial, db_concurrent):
        conn = sqlite3.connect(path)
        create_scan_schema(conn)
        conn.close()

    summary_serial = _run_equivalence_scan(db_serial, "serial", scan_concurrency=1)
    summary_concurrent = _run_equivalence_scan(db_concurrent, "concurrent", scan_concurrency=3)

    # companies_scanned only counts companies whose scan completed without
    # error (see _scan_one_company_via_ats_api / _scan_one_company_worker):
    # greenhouse + lever succeed, ashby raises and is excluded.
    assert summary_serial["companies_scanned"] == 2
    assert summary_serial["jobs_new"] == 2  # greenhouse + lever; ashby raised

    for key in ("companies_scanned", "jobs_discovered", "jobs_new"):
        assert summary_serial[key] == summary_concurrent[key], (
            f"{key}: serial={summary_serial[key]} concurrent={summary_concurrent[key]}"
        )
    assert summary_serial["errors"] == summary_concurrent["errors"]

    jobs_serial = _dump_jobs(db_serial)
    jobs_concurrent = _dump_jobs(db_concurrent)
    assert set(jobs_serial) == set(jobs_concurrent), (
        f"dedup_key sets differ: serial-only={set(jobs_serial) - set(jobs_concurrent)}, "
        f"concurrent-only={set(jobs_concurrent) - set(jobs_serial)}"
    )
    assert jobs_serial == jobs_concurrent, (
        "jd_full/comp UPDATE columns diverged between the serial and worker paths"
    )

    assert _dump_scan_log(db_serial) == _dump_scan_log(db_concurrent)

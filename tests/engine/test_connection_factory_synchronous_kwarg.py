"""``connection_factory(synchronous=...)`` kwarg-forwarding coverage —
review finding FIX D, PR #6.

The Phase 1A plan explicitly calls the ``synchronous`` kwarg load-bearing:
"the kwarg must be forwarded, never dropped." Two hot-path call sites in
``_run.py`` opt in to SQLite's ``NORMAL`` durability mode for multi-commit
throughput under WAL — ``_scan_one_company_worker`` (~line 593, the
concurrent-scan thread-pool path) and ``_scan_one_company_via_ats_api``
(~line 1041, the serial path's per-job upsert connection). Every other
``connection_factory()`` call site in ``ats_scanner`` uses the conservative
``FULL`` default (``_probe.py``'s ``probe_ats_slugs``, ``_run_html.py``'s
``_run_html_fallback_scan``, ``_run_playwright.py``'s per-company upsert).

Prior tests only ever asserted on the SIDE EFFECTS of these calls (rows
upserted, summary counts) — nothing asserted on the actual `synchronous`
value the fake connection_factory test doubles received, so a change that
silently dropped or hardcoded the kwarg (defeating the WAL performance
intent, or accidentally weakening durability elsewhere) would pass every
existing test. This file extends a connection_factory double to RECORD the
`synchronous` kwarg on every call and asserts it against each site.
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import patch

from jobcannon.engine import services
from jobcannon.engine.ats_scanner._probe import probe_ats_slugs
from jobcannon.engine.ats_scanner._run import (
    _scan_one_company_via_ats_api,
    _scan_one_company_worker,
)

from tests.engine.helpers.ats_scan_services import create_scan_schema


def _recording_connection_factory(db_path: str, calls: list):
    """A real (not fake) connection factory bound to db_path that appends
    the `synchronous` kwarg it received on every call, before opening a
    genuine sqlite3 connection so callers relying on real SQL still work."""

    @contextlib.contextmanager
    def factory(*, synchronous: str = "FULL"):
        calls.append(synchronous)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    return factory


def _make_services(db_path: str, calls: list) -> services.ScanServices:
    return services.ScanServices(
        connection_factory=_recording_connection_factory(db_path, calls),
        upsert_job=lambda *a, **k: None,
        set_jd_full=lambda *a, **k: None,
        upsert_company=lambda *a, **k: None,
        get_secret=lambda name, *, config=None: None,
        config={},
        jd_storage_max_chars=100_000,
    )


def _seed_company(conn: sqlite3.Connection, *, platform: str = "not_a_real_platform") -> dict:
    """Minimal eligible row. An unregistered platform name deliberately takes
    the "Unknown ATS platform" branch (job_dicts=[]) in both
    _scan_one_company_via_ats_api and _scan_one_company_worker — that branch
    still reaches the synchronous="NORMAL" connection_factory call further
    down, so this sidesteps needing a real PlatformScanner + run_platform_scan
    mock entirely."""
    cur = conn.execute(
        """INSERT INTO companies
           (name, name_raw, ats_platform, ats_slug, ats_probe_status,
            scan_enabled, created_at, updated_at)
           VALUES ('acme', 'Acme', ?, 'acme', 'hit', 1,
                   '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        (platform,),
    )
    conn.commit()
    return {
        "id": cur.lastrowid,
        "name_raw": "Acme",
        "ats_platform": platform,
        "ats_slug": "acme",
    }


def test_scan_one_company_via_ats_api_uses_normal_synchronous(tmp_path):
    """Serial-path hot site (~_run.py:1041) must forward synchronous='NORMAL'."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    create_scan_schema(conn)
    company = _seed_company(conn)

    calls: list[str] = []
    services.set_services(_make_services(db_path, calls))
    try:
        summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
        _scan_one_company_via_ats_api(conn, db_path, company, ["Engineer"], [], summary, [])
    finally:
        services.clear_services()
    conn.close()

    assert "NORMAL" in calls, f"expected a synchronous='NORMAL' call, got {calls}"


def test_scan_one_company_worker_uses_normal_synchronous(tmp_path):
    """Concurrent-path hot site (~_run.py:593) must forward synchronous='NORMAL'."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    create_scan_schema(conn)
    company = _seed_company(conn)
    conn.close()

    calls: list[str] = []
    services.set_services(_make_services(db_path, calls))
    try:
        result = _scan_one_company_worker(company, db_path, ["Engineer"], [], None)
    finally:
        services.clear_services()

    assert result.error is None
    assert "NORMAL" in calls, f"expected a synchronous='NORMAL' call, got {calls}"


def test_probe_ats_slugs_uses_default_full_synchronous(tmp_path):
    """A non-hot-path site (~_probe.py:336, probe_ats_slugs' own connection)
    must NOT opt in to NORMAL — it should use the conservative FULL default,
    proving the NORMAL opt-in is scoped to exactly the two hot-path sites
    above, not accidentally globalized."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_scan_schema(conn)
    # The shared minimal schema (tests/engine/helpers/ats_scan_services.py)
    # doesn't include ats_probe_attempted_at — it's only referenced by
    # _reset_stale_collision_misses, which probe_ats_slugs always runs before
    # the pending-company query this test cares about. Patch it in locally
    # rather than widening the shared helper for every other test file.
    conn.execute("ALTER TABLE companies ADD COLUMN ats_probe_attempted_at TEXT")
    conn.commit()
    conn.close()  # no pending companies — probe_ats_slugs still opens its conn

    calls: list[str] = []
    services.set_services(_make_services(db_path, calls))
    try:
        with patch("jobcannon.engine.ats_scanner._probe.time.sleep"):
            probe_ats_slugs(db_path, config={})
    finally:
        services.clear_services()

    assert calls, "probe_ats_slugs must open at least one connection"
    assert all(c == "FULL" for c in calls), f"expected only default 'FULL' calls, got {calls}"
    assert "NORMAL" not in calls

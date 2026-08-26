"""Regression coverage for ``_run_playwright.py``'s optional Playwright
dependency handling (review finding, PR #6).

FIX A: the ported ``_run_playwright_scan`` originally kept the private
repo's ``careers_crawler``-PEP-562-hook indirection (``_cc.sync_playwright``
inside a ``try/except ImportError``) to detect a missing Playwright install.
The engine's ``jobcannon/engine/careers_crawler/__init__.py`` is empty (Task
1 ported only ``_title_contract``/``_title_filters``) — it has no such hook,
so the attribute access raised ``AttributeError``, which ``except
ImportError`` cannot catch. That escaped uncaught from the UNGUARDED call
site at ``_run.py``'s ``_run_ats_scan_body`` (no try/except around
``_run_playwright_scan``), aborting the entire scan cycle (every phase after
it — homepage discovery, HTML fallback, scoring, activity log — never runs)
for any host with even one scan-eligible iCIMS company. The fix imports
``playwright.sync_api.sync_playwright`` directly inside the ``try/except
ImportError``, matching the already-proven pattern at
``ats_prober.py``'s ``static_fallthrough`` tier4 handling.

``test_run_playwright_scan_skips_gracefully_when_playwright_unavailable``
below is the regression test: it seeds one real Playwright-eligible (
``ats_platform='icims'``, ``ats_probe_status='hit'``, ``scan_enabled=1``)
company row, forces ``ImportError`` on ``from playwright.sync_api import
sync_playwright`` by nulling ``sys.modules['playwright.sync_api']`` (the
standard technique — deterministic whether or not the optional ``playwright``
extra happens to be installed in the environment running this test; it is
NOT installed by default here, since it lives behind the ``playwright``
pyproject extra, but the null-out makes the test's guarantee independent of
that), and asserts ``_run_playwright_scan`` returns cleanly with no
exception and no companies scanned. Every other test file in this suite
mocks ``_run_playwright_scan`` out entirely (see
``test_run_ats_scan_wiring.py``'s ``_patch_all_phases``), which is exactly
why this bug slipped through review the first time.
"""

from __future__ import annotations

import sqlite3
import sys
from unittest.mock import patch

from jobcannon.engine.ats_scanner._run_playwright import _run_playwright_scan

from tests.engine.helpers.ats_scan_services import create_scan_schema, make_scan_services


def _seed_icims_company(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO companies
           (name, name_raw, ats_platform, ats_slug, ats_probe_status,
            scan_enabled, created_at, updated_at)
           VALUES ('icims co', 'ICIMS Co', 'icims', 'icimsco', 'hit', 1,
                   '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
    )
    conn.commit()


def test_run_playwright_scan_skips_gracefully_when_playwright_unavailable(tmp_path):
    """FIX A regression test: one Playwright-eligible (icims/hit/enabled)
    company must not crash the phase when Playwright is unavailable.

    Pre-fix, this raised AttributeError (uncaught by `except ImportError`)
    because the engine's careers_crawler has no PEP-562 sync_playwright
    hook — the bug this test exists to catch.
    """
    db_path = tmp_path / "scan.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    create_scan_schema(conn)
    _seed_icims_company(conn)

    summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}

    # Simulate Playwright being uninstalled even though it IS installed in
    # this dev/CI environment: nulling a sys.modules entry is the standard
    # way to force the next `from X import Y` to raise ImportError.
    with patch.dict(sys.modules, {"playwright.sync_api": None}):
        _run_playwright_scan(
            conn,
            str(db_path),
            {},
            ["Engineer"],
            [],
            summary,
            [],
            high_score_threshold=999,  # skip history gate
        )

    # Graceful no-op: no exception escaped, nothing was scanned.
    assert summary["companies_scanned"] == 0
    assert summary["errors"] == []
    conn.close()


def test_run_playwright_scan_no_op_when_no_eligible_companies(tmp_path):
    """Sanity companion: with zero Playwright-eligible companies, the phase
    returns before ever touching the sync_playwright import (covers the
    early `if not companies: return` branch so the import-guard test above
    is known to be exercising the intended code path, not this one)."""
    db_path = tmp_path / "scan.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    create_scan_schema(conn)

    summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
    _run_playwright_scan(
        conn, str(db_path), {}, ["Engineer"], [], summary, [], high_score_threshold=999
    )

    assert summary["companies_scanned"] == 0
    conn.close()


def test_run_playwright_scan_imports_sync_playwright_directly_when_available(tmp_path):
    """Companion positive case: when Playwright IS importable, the phase
    does not swallow that success — it proceeds to open sync_playwright()
    and attempt the scan.

    Playwright is not installed in this dev/CI environment (an optional
    heavy dep — see module docstring), so `from playwright.sync_api import
    sync_playwright` has no real target to patch via the usual dotted-path
    `unittest.mock.patch()`. Instead this injects a fake `playwright.sync_api`
    module directly into `sys.modules`, which is what Python's import
    machinery actually consults — proving the fix's `from playwright.sync_api
    import sync_playwright` statement resolves and is CALLED (not merely
    present in the source), without requiring the real package."""
    db_path = tmp_path / "scan.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    create_scan_schema(conn)
    _seed_icims_company(conn)

    calls = []

    class _FakeBrowser:
        def close(self):
            pass

    class _FakePW:
        class chromium:
            @staticmethod
            def launch(headless=True):
                calls.append(headless)
                return _FakeBrowser()

    class _FakeContextManager:
        def __enter__(self):
            return _FakePW()

        def __exit__(self, *exc):
            return False

    fake_sync_api = type(sys)("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: _FakeContextManager()
    fake_playwright_pkg = type(sys)("playwright")
    fake_playwright_pkg.sync_api = fake_sync_api

    services = make_scan_services(str(db_path))
    from jobcannon.engine import services as services_module

    services_module.set_services(services)
    try:
        with (
            patch.dict(
                sys.modules,
                {"playwright": fake_playwright_pkg, "playwright.sync_api": fake_sync_api},
            ),
            patch(
                "jobcannon.engine.ats_scanner._run_playwright._scan_one_company_via_playwright"
            ) as mock_scan_one,
        ):
            summary = {"companies_scanned": 0, "jobs_discovered": 0, "jobs_new": 0, "errors": []}
            _run_playwright_scan(
                conn,
                str(db_path),
                {},
                ["Engineer"],
                [],
                summary,
                [],
                high_score_threshold=999,
            )
    finally:
        services_module.clear_services()

    assert calls == [True], "sync_playwright()'s chromium.launch(headless=True) must be reached"
    assert mock_scan_one.call_count == 1
    conn.close()

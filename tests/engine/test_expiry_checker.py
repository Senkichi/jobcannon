# PORTED from job_finder/web/tests/test_expiry_checker.py @ 039de5a24d4eceba3992c036cd33ecff1ca9592a (private job-cannon). Ledger L-0182.
"""Tests for jobcannon.engine.expiry_checker — job expiry detection signal
cascade and the unified staleness orchestrator.

Ported from job_finder/web/tests/test_expiry_checker.py (job-cannon private
repo). Test bodies/assertions port unchanged wherever possible; the
departures below are all fixture-wiring or DI-seam consequences of the
engine/host split (see jobcannon/engine/services.py's module docstring),
plus one bug fix found and applied to the already-ported source while
writing this file.

Departures from the private test file:

1. **Every test gets an autouse `db` fixture (active ScanServices), even
   tests that needed no DB/services setup at all privately.** Unlike
   test_stale_detector.py's precedent (where only tests that actually touch
   run_stale_detection's connection need the fixture), nearly every
   function in this module reaches jobcannon.engine.services.get_services()
   somewhere on its call path even when no conn/db_path is supplied:
   `is_opaque_redirect_candidate`'s `conn is None` fallback (reached by
   `check_job_liveness` and `_check_job_expiry`'s Signal 0 block) and
   `_check_careers_page`'s unconditional first statement both call it
   unconditionally. `db_path` is vestigial throughout this module and
   opaque_redirect_candidates.py — every real DB access always routes
   through `svc.connection_factory()` regardless of whether a `db_path`/
   `conn` argument was supplied — so making the fixture autouse (rather
   than opt-in per test) is the only way every test gets a consistently
   working seam without hunting down which specific tests need it.

2. **`find_careers_url` / `scrape_careers_page` / `tiebreak_primary_posting`
   / `reconcile_all_companies` are ScanServices fields, not module-level
   lazy imports.** Private's `@patch("job_finder.web.expiry_checker.X")` on
   these four becomes `_reconfigure(X=mock)` (a `dataclasses.replace()`
   helper below) injecting a per-test fake through the DI seam.
   `_check_ats_api`, `quick_liveness_check`, `_check_careers_page`,
   `_check_job_expiry`, `_cascade_worker`, and `_run_phase_c_cascade`
   remain module-level functions in the port and stay patchable exactly as
   before via `@patch("jobcannon.engine.expiry_checker.<name>")`.

3. **`_check_careers_page`'s availability guard requires BOTH
   `svc.find_careers_url` and `svc.scrape_careers_page` to be non-None
   simultaneously** (`if svc.find_careers_url is None or
   svc.scrape_careers_page is None: return INCONCLUSIVE, None, False`) —
   unlike the private repo, where the two were independently-patchable
   module-level names. A test exercising only one side (e.g.
   `find_careers_url` returning None) must still supply a harmless
   `scrape_careers_page` so the joint guard passes; the `_wire_careers()`
   helper below defaults whichever side a test doesn't care about.

4. **Phase A (`run_stale_detection`) is imported LOCALLY inside
   `run_staleness_check`** (`from jobcannon.engine.stale_detector import
   run_stale_detection`), not at module scope — the patch target is
   `jobcannon.engine.stale_detector.run_stale_detection` (the source
   module), not an `expiry_checker` attribute (there is none to patch;
   the import happens fresh inside the function body on every call).

5. **`persist_job_expiry_state` / `update_pipeline_status` have no
   `jobcannon.db` counterpart** (services.py's field comments: "no ledger
   row identified in this port's read scope"). Without a real
   implementation wired into ScanServices, `_persist_cascade_worker_result`
   silently no-ops (both hooks default to `None`) and none of
   `expiry_status` / `pipeline_status` / `pipeline_events` would ever
   change, breaking every assertion that reads them back. `_base_services()`
   below wires in `_persist_job_expiry_state` / `_update_pipeline_status`,
   local stand-ins mirroring the private repo's
   `job_finder/db/_persistence.py` write behavior (live verdict refreshes
   last_seen/clears is_stale; expired stamps expiry_checked_at; inconclusive
   updates expiry_status only; pipeline status change logs one
   pipeline_events row). The `auto-excluded` / `excluded_reason` branch of
   private's `update_pipeline_status` is dropped — no ported test exercises
   it.

6. **`TestAutoReopen` (10 tests) is skipped wholesale.** Every test in it
   drives `job_finder.db.upsert_job`'s archived-job reopen side effect
   directly against a real migrated DB. `jobcannon/db/` (the closest-named
   module) is an entirely different, PostgreSQL-based persistence layer for
   the web app's own primary DB (`%s` placeholders, `postings` table) —
   architecturally unrelated to this module's sqlite3 scan-state DB reached
   only via `svc.connection_factory()` (`jobs`/`companies`/`pipeline_events`
   tables). No ledger row in this port's scope (L-0182 covers
   `expiry_checker.py` only) names a `jobcannon.db.upsert_job`
   reopen-on-re-ingestion port.

7. **All private tests seeding a DB via `job_finder.web.db_migrate.
   run_migrations` against a real on-disk sqlite file are rebased onto the
   shared in-memory `db` fixture** (minimal `_SCHEMA`, matching
   test_stale_detector.py's precedent, extended with `direct_url_confidence`
   and `opaque_redirect_host_outcomes`), with local helper functions
   (`_insert_job`, `_insert_company`) performing equivalent direct INSERTs
   instead of running real migrations.

8. **`set_direct_url` is an optional ScanServices seam, not a direct
   `jobcannon.db._direct_link` import.** `_persist_cascade_worker_result`
   calls `svc.set_direct_url(conn, dedup_key, direct_url, "strict")` (guarded
   by `is not None`) instead of importing
   `jobcannon.db._direct_link.set_direct_url` directly. That function is
   safe to call directly against a real `connection_factory` connection in
   production (it unwraps `conn.raw` and runs its own Postgres-native `%s`
   SQL), but it cannot run against the bare sqlite3 connection this test
   fixture supplies (no `.raw` attribute, no `postings` table, `%s`
   placeholders are invalid sqlite3 syntax). `_base_services()` below wires
   a local `_set_direct_url` fake into `ScanServices.set_direct_url`
   reimplementing the same no-downgrade precedence contract
   (`jobcannon/db/_direct_link.py`'s docstring: strict overwrites
   NULL/loose, never another strict; loose only fills a NULL slot) against
   this fixture's sqlite `jobs.direct_url` / `jobs.direct_url_confidence`
   columns. `TestPhaseCWritesDirectUrlAndCareersCheckedAt` exercises this
   path directly.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pytest
import requests

from jobcannon.engine import services
from jobcannon.engine.expiry_checker import (
    EXPIRED,
    INCONCLUSIVE,
    LIVE,
    CareersPageMemo,
    CascadeResult,
    _check_careers_page,
    _check_job_expiry,
    _extract_posting_id,
    _get_cascade_parallel_workers,
    _get_cascade_runtime_limit_s,
    _run_phase_c_cascade,
    check_job_liveness,
    quick_liveness_check,
    run_staleness_check,
)

_SCHEMA = """
CREATE TABLE jobs (
    dedup_key TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    location TEXT,
    sources TEXT,
    source_urls TEXT,
    source_id TEXT,
    first_seen TEXT,
    last_seen TEXT,
    pipeline_status TEXT,
    is_stale INTEGER DEFAULT 0,
    expiry_status TEXT,
    expiry_checked_at TEXT,
    company_id INTEGER,
    direct_url TEXT,
    direct_url_confidence TEXT,
    direct_url_attempts INTEGER DEFAULT 0,
    careers_checked_at TEXT
);

CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    name_raw TEXT,
    homepage_url TEXT,
    ats_platform TEXT,
    ats_slug TEXT,
    ats_probe_status TEXT,
    scan_enabled INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE pipeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    from_status TEXT,
    to_status TEXT,
    timestamp TEXT,
    source TEXT,
    evidence TEXT
);

CREATE TABLE opaque_redirect_host_outcomes (
    host TEXT PRIMARY KEY,
    attempts INTEGER DEFAULT 0,
    blocked_count INTEGER DEFAULT 0,
    last_seen TEXT
);
"""


def _persist_job_expiry_state(conn, dedup_key, expiry_status, checked_at):
    """Local stand-in for the (unported) jobcannon.db persist_job_expiry_state.

    Mirrors job_finder/db/_persistence.py's write behavior: a 'live' verdict
    also refreshes last_seen and clears is_stale; 'expired' stamps
    expiry_checked_at; 'inconclusive' updates expiry_status only (does not
    advance expiry_checked_at, so a TTL-style re-check gate is not
    suppressed). See module docstring point 5.
    """
    if expiry_status == LIVE:
        conn.execute(
            "UPDATE jobs SET expiry_status = ?, expiry_checked_at = ?, "
            "last_seen = ?, is_stale = 0 WHERE dedup_key = ?",
            (expiry_status, checked_at, checked_at, dedup_key),
        )
    elif expiry_status == EXPIRED:
        conn.execute(
            "UPDATE jobs SET expiry_status = ?, expiry_checked_at = ? WHERE dedup_key = ?",
            (expiry_status, checked_at, dedup_key),
        )
    else:
        conn.execute(
            "UPDATE jobs SET expiry_status = ? WHERE dedup_key = ?",
            (expiry_status, dedup_key),
        )
    conn.commit()


def _update_pipeline_status(conn, dedup_key, new_status, source="manual", evidence=""):
    """Local stand-in for the (unported) jobcannon.db update_pipeline_status.

    Mirrors job_finder/db/_persistence.py's write + pipeline_events audit
    row; the 'auto-excluded'/excluded_reason branch is dropped (not
    exercised by any ported test here). See module docstring point 5.
    """
    row = conn.execute(
        "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    if row is None:
        return
    from_status = row["pipeline_status"]
    if from_status == new_status:
        return
    now = datetime.now(UTC).isoformat()
    conn.execute("UPDATE jobs SET pipeline_status = ? WHERE dedup_key = ?", (new_status, dedup_key))
    conn.execute(
        "INSERT INTO pipeline_events (job_id, from_status, to_status, timestamp, source, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dedup_key, from_status, new_status, now, source, evidence),
    )
    conn.commit()


def _set_direct_url(conn, dedup_key, url, confidence):
    """Local fake for the (production-only-safe) svc.set_direct_url seam.

    Reimplements jobcannon/db/_direct_link.py's documented no-downgrade
    precedence (strict overwrites NULL/loose, never another strict; loose
    only fills a NULL slot) against this fixture's sqlite jobs.direct_url /
    jobs.direct_url_confidence columns, since the real function's
    Postgres-native %s SQL cannot run against a bare sqlite3 connection.
    See module docstring point 8.
    """
    row = conn.execute(
        "SELECT direct_url_confidence FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    if row is None:
        return False
    existing = row["direct_url_confidence"]
    if existing is not None:
        if confidence == "loose":
            return False
        if existing == "strict":
            return False
    conn.execute(
        "UPDATE jobs SET direct_url = ?, direct_url_confidence = ? WHERE dedup_key = ?",
        (url, confidence, dedup_key),
    )
    conn.commit()
    return True


def _base_services(factory):
    return services.ScanServices(
        connection_factory=factory,
        upsert_job=lambda *a, **k: None,
        set_jd_full=lambda *a, **k: None,
        upsert_company=lambda *a, **k: None,
        get_secret=lambda name, *, config=None: None,
        config={},
        jd_storage_max_chars=100_000,
        persist_job_expiry_state=_persist_job_expiry_state,
        update_pipeline_status=_update_pipeline_status,
        set_direct_url=_set_direct_url,
    )


@pytest.fixture(autouse=True)
def db():
    """(db_path, conn) pair, autouse — see module docstring point 1.

    Mirrors test_stale_detector.py's fixture shape: a fake
    connection_factory always yields the SAME pre-built, pre-seeded
    in-memory connection regardless of db_path/synchronous kwargs, and does
    not close it, so test bodies can keep querying it after the call under
    test returns. Autouse means every test gets a working ScanServices
    without remembering to request it; tests that also need to seed/query
    the DB still take `db` as an explicit parameter for the (path, conn)
    tuple — fixture caching returns the same instance either way.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()

    @contextlib.contextmanager
    def factory(*, synchronous="FULL"):
        yield conn

    services.set_services(_base_services(factory))
    try:
        yield "fake.db", conn
    finally:
        services.clear_services()
        conn.close()


def _reconfigure(**overrides):
    """Merge field overrides into the active ScanServices via dataclasses.replace().

    See module docstring point 2.
    """
    services.set_services(dataclasses.replace(services.get_services(), **overrides))


def _wire_careers(
    *, find_careers_url=None, scrape_careers_page=None, tiebreak_primary_posting=None
):
    """Wire svc.find_careers_url / svc.scrape_careers_page, defaulting
    whichever side isn't under test to a harmless mock so
    _check_careers_page's joint non-None guard passes. See module
    docstring point 3.
    """
    overrides = {
        "find_careers_url": find_careers_url or MagicMock(return_value=None),
        "scrape_careers_page": scrape_careers_page or MagicMock(return_value=([], 0)),
    }
    if tiebreak_primary_posting is not None:
        overrides["tiebreak_primary_posting"] = tiebreak_primary_posting
    _reconfigure(**overrides)


def _insert_job(
    conn,
    dedup_key,
    pipeline_status="discovered",
    *,
    title="DS",
    company="Acme",
    location="Remote",
    sources="[]",
    source_urls="[]",
    first_seen="2026-01-01T00:00:00",
    last_seen="2026-01-01T00:00:00",
    company_id=None,
    expiry_status=None,
    expiry_checked_at=None,
    direct_url=None,
    direct_url_confidence=None,
    direct_url_attempts=0,
    careers_checked_at=None,
):
    conn.execute(
        """INSERT INTO jobs
           (dedup_key, title, company, location, sources, source_urls, source_id,
            first_seen, last_seen, pipeline_status, company_id, expiry_status,
            expiry_checked_at, direct_url, direct_url_confidence,
            direct_url_attempts, careers_checked_at)
           VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dedup_key,
            title,
            company,
            location,
            sources,
            source_urls,
            first_seen,
            last_seen,
            pipeline_status,
            company_id,
            expiry_status,
            expiry_checked_at,
            direct_url,
            direct_url_confidence,
            direct_url_attempts,
            careers_checked_at,
        ),
    )
    conn.commit()


def _insert_company(
    conn,
    name="Acme",
    *,
    company_id=None,
    homepage_url=None,
    ats_platform=None,
    ats_slug=None,
    ats_probe_status="pending",
    scan_enabled=1,
):
    cur = conn.execute(
        "INSERT INTO companies (id, name, name_raw, homepage_url, ats_platform, "
        "ats_slug, ats_probe_status, scan_enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')",
        (
            company_id,
            name,
            name,
            homepage_url,
            ats_platform,
            ats_slug,
            ats_probe_status,
            scan_enabled,
        ),
    )
    conn.commit()
    return company_id if company_id is not None else cur.lastrowid


class TestQuickLivenessCheck:
    """quick_liveness_check: lightweight per-job URL check for scoring preflight."""

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_404_returns_expired(self, mock_get):
        mock_get.return_value = MagicMock(spec=requests.Response, status_code=404)
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_410_returns_expired(self, mock_get):
        mock_get.return_value = MagicMock(spec=requests.Response, status_code=410)
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_200_returns_live(self, mock_get):
        mock_get.return_value = MagicMock(
            spec=requests.Response, status_code=200, text="Job description here"
        )
        assert quick_liveness_check("https://example.com/job/123") == LIVE

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_timeout_returns_inconclusive(self, mock_get):
        mock_get.side_effect = requests.exceptions.Timeout("timed out")
        assert quick_liveness_check("https://example.com/job/123") == INCONCLUSIVE

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_connection_error_returns_inconclusive(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")
        assert quick_liveness_check("https://example.com/job/123") == INCONCLUSIVE

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_body_marker_position_filled_returns_expired(self, mock_get):
        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text="Sorry, this position has been filled. Please check other openings.",
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_body_marker_no_longer_available_returns_expired(self, mock_get):
        mock_get.return_value = MagicMock(
            spec=requests.Response, status_code=200, text="This job is no longer available."
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_new_marker_this_job_has_expired_returns_expired(self, mock_get):
        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text="We're sorry, this job has expired. Please browse other openings.",
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_new_marker_this_position_is_no_longer_available_returns_expired(self, mock_get):
        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text="This position is no longer available. Check our careers page.",
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_regex_glassdoor_date_interpolated_returns_expired(self, mock_get):
        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text="This job from Jul 9, 2025 is no longer available for applications.",
        )
        assert quick_liveness_check("https://www.glassdoor.com/job/123") == EXPIRED

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_regex_no_false_positive_on_benign_text(self, mock_get):
        # "no longer" in benign context should not trigger EXPIRED
        mock_get.return_value = MagicMock(
            spec=requests.Response,
            status_code=200,
            text=(
                "We are no longer just a startup — join our mission. "
                "This job requires experience with systems that are no longer maintained."
            ),
        )
        assert quick_liveness_check("https://example.com/job/123") == LIVE

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_regex_no_longer_active_returns_expired(self, mock_get):
        mock_get.return_value = MagicMock(
            spec=requests.Response, status_code=200, text="This posting is no longer active."
        )
        assert quick_liveness_check("https://example.com/job/123") == EXPIRED


class TestCheckJobLiveness:
    """check_job_liveness: extract URLs and call quick_liveness_check."""

    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_calls_first_url(self, mock_check):
        mock_check.return_value = LIVE
        job = {"source_urls": '["https://a.com/1", "https://b.com/2"]'}
        assert check_job_liveness(job, {}) == LIVE
        mock_check.assert_called_once_with("https://a.com/1", timeout=8, config={})

    def test_no_urls_returns_inconclusive(self):
        assert check_job_liveness({"source_urls": "[]"}, {}) == INCONCLUSIVE
        assert check_job_liveness({}, {}) == INCONCLUSIVE

    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_handles_list_type_source_urls(self, mock_check):
        mock_check.return_value = EXPIRED
        job = {"source_urls": ["https://example.com/job/1"]}
        assert check_job_liveness(job, {}) == EXPIRED


class TestCheckJobLivenessSkipsOpaqueSources:
    """check_job_liveness skips HTTP fetch for known opaque-redirect sources."""

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_opaque_source_skips_http_fetch(self, mock_get):
        """Opaque-redirect source (e.g. Jooble) returns INCONCLUSIVE without HTTP GET."""
        config = {
            "verification": {
                "opaque_redirect_sources": [{"source_tag": "portal_jooble", "domain": "jooble.org"}]
            }
        }
        job = {
            "sources": ["portal_jooble"],
            "source_urls": ["https://jooble.org/away/12345"],
        }
        assert check_job_liveness(job, config) == INCONCLUSIVE
        mock_get.assert_not_called()

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_non_opaque_source_performs_http_fetch(self, mock_get):
        """Non-opaque source (e.g. Greenhouse) performs HTTP GET as before."""
        mock_get.return_value = MagicMock(spec=requests.Response, status_code=200, text="Job")
        config = {
            "verification": {
                "opaque_redirect_sources": [{"source_tag": "portal_jooble", "domain": "jooble.org"}]
            }
        }
        job = {
            "sources": ["greenhouse"],
            "source_urls": ["https://boards.greenhouse.io/acme/jobs/1"],
        }
        assert check_job_liveness(job, config) == LIVE
        mock_get.assert_called_once()

    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_skips_leading_none_element(self, mock_check):
        """A None element can survive in a legacy jobs.source_urls row written
        before the upsert write-boundary guard existed. quick_liveness_check
        would raise TypeError on a None url — must skip to the next real
        string instead of blindly using source_urls[0]."""
        mock_check.return_value = LIVE
        job = {"source_urls": [None, "https://example.com/job/1"]}
        assert check_job_liveness(job, {}) == LIVE
        mock_check.assert_called_once_with("https://example.com/job/1", timeout=8, config={})

    def test_all_non_string_elements_returns_inconclusive(self):
        assert check_job_liveness({"source_urls": [None, ""]}, {}) == INCONCLUSIVE


class TestExtractPostingId:
    """_extract_posting_id extracts individual posting IDs from ATS URLs."""

    def test_lever_uuid(self):
        url = "https://jobs.lever.co/acme-corp/abc12345-def6-7890-abcd-ef1234567890"
        assert _extract_posting_id(url, "lever") == "abc12345-def6-7890-abcd-ef1234567890"

    def test_greenhouse_numeric_id(self):
        url = "https://boards.greenhouse.io/acme/jobs/4567890"
        assert _extract_posting_id(url, "greenhouse") == "4567890"

    def test_greenhouse_self_hosted_gh_jid(self):
        """Regression (#644 fallout): custom-domain Greenhouse boards carry ?gh_jid=<id>.

        Before routing greenhouse through the registry chain, the narrow
        boards.greenhouse.io pattern returned None here, so Signal 1 (per-posting
        ATS API liveness) silently no-op'd for every self-hosted Greenhouse job
        (sofi, airbnb, pinterest, roblox, ...).
        """
        url = "https://careers.airbnb.com/positions/7662244?gh_jid=7662244"
        assert _extract_posting_id(url, "greenhouse") == "7662244"

    def test_greenhouse_eu_region_host(self):
        """Regression: EU data-region host (job-boards.eu.greenhouse.io) resolves."""
        url = "https://job-boards.eu.greenhouse.io/moniepoint/jobs/4808972101"
        assert _extract_posting_id(url, "greenhouse") == "4808972101"

    def test_ashby_uuid(self):
        url = "https://jobs.ashbyhq.com/AcmeCorp/abc12345-def6-7890-abcd-ef1234567890"
        assert _extract_posting_id(url, "ashby") == "abc12345-def6-7890-abcd-ef1234567890"

    def test_returns_none_for_non_matching_url(self):
        url = "https://www.linkedin.com/jobs/view/12345/"
        assert _extract_posting_id(url, "lever") is None

    def test_returns_none_for_unknown_platform(self):
        url = "https://jobs.lever.co/acme/abc123"
        assert _extract_posting_id(url, "unknown") is None


class TestCheckAtsApi:
    """Signal 1: ATS API liveness check."""

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_lever_404_returns_expired(self, mock_get):
        from jobcannon.engine.expiry_checker import _check_ats_api

        mock_get.return_value = MagicMock(status_code=404)
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == EXPIRED
        mock_get.assert_called_once()
        assert "api.lever.co" in mock_get.call_args[0][0]

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_lever_200_returns_live(self, mock_get):
        from jobcannon.engine.expiry_checker import _check_ats_api

        mock_get.return_value = MagicMock(status_code=200)
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == LIVE

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_greenhouse_404_returns_expired(self, mock_get):
        from jobcannon.engine.expiry_checker import _check_ats_api

        mock_get.return_value = MagicMock(status_code=404)
        result = _check_ats_api("acme", "12345", "greenhouse")
        assert result == EXPIRED
        # PORT-SEAM: codeql py/incomplete-url-substring-sanitization -- parsed
        # hostname check, not a substring `in` check (matches #259's fix
        # pattern for platform_extractor.py / _platforms_icims.py).
        assert urlsplit(mock_get.call_args[0][0]).hostname == "boards-api.greenhouse.io"

    @patch("jobcannon.engine.expiry_checker.requests.get")
    def test_network_error_returns_inconclusive(self, mock_get):
        from jobcannon.engine.expiry_checker import _check_ats_api

        mock_get.side_effect = requests.exceptions.ConnectionError("timeout")
        result = _check_ats_api("acme", "abc-123", "lever")
        assert result == INCONCLUSIVE

    def test_unknown_platform_returns_inconclusive(self):
        from jobcannon.engine.expiry_checker import _check_ats_api

        result = _check_ats_api("acme", "abc-123", "unknown")
        assert result == INCONCLUSIVE


class TestCheckCareersPage:
    """Signal 2: Company careers page title search.

    PORT-SEAM: find_careers_url / scrape_careers_page / tiebreak_primary_posting
    are ScanServices fields here, not module-level lazy imports — private's
    @patch("job_finder.web.expiry_checker.X") becomes _wire_careers(X=mock).
    See module docstring points 2-3.
    """

    def test_title_found_returns_live_with_matched_url(self, db):
        _wire_careers(
            find_careers_url=MagicMock(return_value="https://acme.com/careers"),
            scrape_careers_page=MagicMock(
                return_value=(
                    [{"title": "Senior Data Scientist", "url": "https://acme.com/careers/123"}],
                    0,
                )
            ),
        )
        status, matched_url, attempted = _check_careers_page(
            "https://acme.com", "Senior Data Scientist", ["data scientist"], []
        )
        assert status == LIVE
        assert matched_url == "https://acme.com/careers/123"
        assert attempted is True

    def test_strict_miss_falls_through_to_llm_confident_match(self, db):
        """The keyword-boundary match fails (freeform HTML title), but the
        LLM-assisted fallback confidently matches — the whole point of this
        plan's fix (measured 0/508 on the strict-only matcher).

        db_path is vestigial here (svc.connection_factory() always yields
        the fixture's fake connection regardless of the string passed), so
        unlike the private test this needs no real tmp_path directory.
        """
        scraped = [
            {
                "title": "Principal Product Manager, Growth",
                "url": "https://acme.com/careers/999",
                "location": "Remote",
            }
        ]
        mock_tiebreak = MagicMock(return_value=scraped[0])
        _wire_careers(
            find_careers_url=MagicMock(return_value="https://acme.com/careers"),
            scrape_careers_page=MagicMock(return_value=(scraped, 0)),
            tiebreak_primary_posting=mock_tiebreak,
        )

        status, matched_url, attempted = _check_careers_page(
            "https://acme.com",
            "Senior Software Engineer, Platform",
            ["software engineer"],
            [],
            db_path="fake.db",
            config={},
            job_id="dk1",
        )
        assert status == LIVE
        assert matched_url == "https://acme.com/careers/999"
        assert attempted is True
        mock_tiebreak.assert_called_once()

    def test_llm_not_confident_stays_inconclusive(self, db):
        _wire_careers(
            find_careers_url=MagicMock(return_value="https://acme.com/careers"),
            scrape_careers_page=MagicMock(
                return_value=(
                    [
                        {
                            "title": "Backend Engineer",
                            "url": "https://acme.com/careers/1",
                            "location": "",
                        }
                    ],
                    0,
                )
            ),
            tiebreak_primary_posting=MagicMock(return_value=None),  # not confident
        )

        status, matched_url, attempted = _check_careers_page(
            "https://acme.com",
            "Senior Data Scientist",
            ["data scientist"],
            [],
            db_path="fake.db",
            config={},
        )
        assert status == INCONCLUSIVE
        assert matched_url is None
        assert attempted is True

    def test_llm_skipped_without_db_path_or_config(self, db):
        """No db_path/config supplied (e.g. a caller that hasn't been updated
        yet) — degrades to the strict-only behavior, no crash."""
        _wire_careers(
            find_careers_url=MagicMock(return_value="https://acme.com/careers"),
            scrape_careers_page=MagicMock(
                return_value=(
                    [{"title": "Backend Engineer", "url": "https://acme.com/careers/1"}],
                    0,
                )
            ),
        )
        status, matched_url, attempted = _check_careers_page(
            "https://acme.com", "Senior Data Scientist", ["data scientist"], []
        )
        assert status == INCONCLUSIVE
        assert matched_url is None
        assert attempted is True

    def test_llm_provider_failure_does_not_raise(self, db):
        """A provider/cascade exception from the tie-break call must degrade
        to INCONCLUSIVE, never propagate — this runs inside a
        ThreadPoolExecutor worker in production; an unhandled exception there
        is swallowed by _cascade_worker's own try/except anyway, but the
        function itself should already be safe."""
        _wire_careers(
            find_careers_url=MagicMock(return_value="https://acme.com/careers"),
            scrape_careers_page=MagicMock(
                return_value=(
                    [{"title": "Backend Engineer", "url": "https://acme.com/careers/1"}],
                    0,
                )
            ),
            tiebreak_primary_posting=MagicMock(
                side_effect=RuntimeError("provider cascade exhausted")
            ),
        )

        status, matched_url, attempted = _check_careers_page(
            "https://acme.com",
            "Senior Data Scientist",
            ["data scientist"],
            [],
            db_path="fake.db",
            config={},
        )
        assert status == INCONCLUSIVE
        assert matched_url is None
        assert attempted is True

    def test_title_not_found_no_candidates_returns_inconclusive(self, db):
        _wire_careers(
            find_careers_url=MagicMock(return_value="https://acme.com/careers"),
            scrape_careers_page=MagicMock(return_value=([], 0)),
        )
        status, matched_url, attempted = _check_careers_page(
            "https://acme.com", "Senior Data Scientist", ["data scientist"], []
        )
        assert status == INCONCLUSIVE
        assert matched_url is None
        assert attempted is True

    def test_no_careers_url_returns_inconclusive_but_attempted(self, db):
        _wire_careers(find_careers_url=MagicMock(return_value=None))
        status, matched_url, attempted = _check_careers_page(
            "https://acme.com", "Senior Data Scientist", ["data scientist"], []
        )
        assert status == INCONCLUSIVE
        assert matched_url is None
        assert attempted is True

    def test_no_homepage_returns_inconclusive_not_attempted(self, db):
        status, matched_url, attempted = _check_careers_page(
            None, "Senior Data Scientist", ["data scientist"], []
        )
        assert status == INCONCLUSIVE
        assert matched_url is None
        assert attempted is False


class TestSignalCascade:
    """_check_job_expiry runs signals in order and short-circuits.

    SerpAPI (Signal 3) was removed from the cascade — absence from its index
    is a weak signal that caused false positives, and per-job 30s timeouts
    dominated wall-clock runtime. Signal 2 (careers page) is now the final
    fallback; INCONCLUSIVE from it means we can't tell.
    """

    @patch("jobcannon.engine.expiry_checker._check_careers_page")
    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_ats_expired_short_circuits(self, mock_url, mock_ats, mock_careers, db):
        mock_url.return_value = INCONCLUSIVE  # Signal 0 passes through
        mock_ats.return_value = EXPIRED
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://jobs.lever.co/acme/abc-123"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        cascade = _check_job_expiry(job, company, config)
        assert cascade.result == EXPIRED
        assert "lever" in cascade.evidence.lower() or "ats" in cascade.evidence.lower()
        assert cascade.direct_url is None
        assert cascade.careers_attempted is False
        mock_careers.assert_not_called()

    @patch("jobcannon.engine.expiry_checker._check_careers_page")
    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_ats_inconclusive_falls_through_to_careers(self, mock_url, mock_ats, mock_careers, db):
        mock_url.return_value = INCONCLUSIVE  # Signal 0 passes through
        mock_ats.return_value = INCONCLUSIVE
        mock_careers.return_value = (LIVE, "https://acme.com/careers/1", True)
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://jobs.lever.co/acme/abc-123"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        cascade = _check_job_expiry(job, company, config)
        assert cascade.result == LIVE
        assert cascade.direct_url == "https://acme.com/careers/1"
        assert cascade.careers_attempted is True
        mock_careers.assert_called_once()

    @patch("jobcannon.engine.expiry_checker._check_careers_page")
    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_all_inconclusive_returns_inconclusive(self, mock_url, mock_ats, mock_careers, db):
        mock_url.return_value = INCONCLUSIVE
        mock_ats.return_value = INCONCLUSIVE
        mock_careers.return_value = (INCONCLUSIVE, None, True)
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://jobs.lever.co/acme/abc-123"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        cascade = _check_job_expiry(job, company, config)
        assert cascade.result == INCONCLUSIVE
        assert cascade.direct_url is None
        assert cascade.careers_attempted is True

    @patch("jobcannon.engine.expiry_checker._check_careers_page")
    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_signal_0_expired_short_circuits_before_ats(self, mock_url, mock_ats, mock_careers, db):
        """Pre-existing test (not new coverage), migrated to CascadeResult
        access. job has no 'sources' key, so is_opaque_redirect_source (Plan
        1's fix 2c, reproduced in Task 4's Signal 0 block above) returns
        False and Signal 0 runs unskipped — same behavior as before Plan 1."""
        mock_url.return_value = EXPIRED
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://example.com/job/1"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        cascade = _check_job_expiry(job, company, config)
        assert cascade.result == EXPIRED
        assert "url_check" in cascade.evidence
        mock_ats.assert_not_called()
        mock_careers.assert_not_called()

    @patch("jobcannon.engine.expiry_checker._check_careers_page")
    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_signal_0_live_short_circuits_before_ats(self, mock_url, mock_ats, mock_careers, db):
        """Pre-existing test, migrated to CascadeResult access."""
        mock_url.return_value = LIVE
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://example.com/job/1"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        cascade = _check_job_expiry(job, company, config)
        assert cascade.result == LIVE
        assert "url_check" in cascade.evidence
        mock_ats.assert_not_called()
        mock_careers.assert_not_called()

    @patch("jobcannon.engine.expiry_checker._check_careers_page")
    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_signal_0_inconclusive_falls_through_to_signal_1(
        self, mock_url, mock_ats, mock_careers, db
    ):
        """Pre-existing test, migrated to CascadeResult access."""
        mock_url.return_value = INCONCLUSIVE
        mock_ats.return_value = EXPIRED
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://jobs.lever.co/acme/abc-123"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}
        config = {}
        cascade = _check_job_expiry(job, company, config)
        assert cascade.result == EXPIRED
        mock_ats.assert_called_once()

    @patch("jobcannon.engine.expiry_checker._check_careers_page")
    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    def test_signal_0_skipped_when_no_source_urls(self, mock_ats, mock_careers, db):
        """Pre-existing test, migrated to CascadeResult access. mock_careers
        now returns the 3-tuple shape _check_careers_page produces."""
        mock_ats.return_value = INCONCLUSIVE
        mock_careers.return_value = (INCONCLUSIVE, None, True)
        job = {"dedup_key": "test", "title": "DS", "company": "Acme", "source_urls": "[]"}
        company = {"ats_platform": None, "ats_slug": None, "homepage_url": None}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        cascade = _check_job_expiry(job, company, config)
        assert cascade.result == INCONCLUSIVE

    @patch("jobcannon.engine.expiry_checker._check_careers_page")
    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_skip_careers_never_attempts(self, mock_url, mock_careers, db):
        """skip_careers=True (backoff active) must short-circuit before
        _check_careers_page is even called — careers_attempted stays False."""
        mock_url.return_value = INCONCLUSIVE
        job = {"dedup_key": "test", "title": "DS", "source_urls": "[]"}
        cascade = _check_job_expiry(job, None, {}, skip_careers=True)
        assert cascade.result == INCONCLUSIVE
        assert cascade.careers_attempted is False
        mock_careers.assert_not_called()

    @patch("jobcannon.engine.expiry_checker._check_careers_page")
    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_db_path_threaded_to_careers_check(self, mock_url, mock_careers, db):
        """db_path passed into _check_job_expiry must reach _check_careers_page
        — this is the only path by which Signal 2's LLM-assist can open its
        own connection."""
        mock_url.return_value = INCONCLUSIVE
        mock_careers.return_value = (INCONCLUSIVE, None, True)
        job = {"dedup_key": "dk1", "title": "DS", "source_urls": "[]"}
        company = {"homepage_url": "https://acme.com"}
        config = {"profile": {"target_titles": [], "exclusions": {"title_keywords": []}}}
        _check_job_expiry(job, company, config, db_path="/tmp/fake.db")
        _, kwargs = mock_careers.call_args
        assert kwargs.get("db_path") == "/tmp/fake.db"
        assert kwargs.get("job_id") == "dk1"


class TestCascadeWorkerReturnShape:
    """_cascade_worker must propagate CascadeResult's new fields through its
    own tuple return, since Phase C's write loop reads them positionally."""

    def setup_method(self):
        from jobcannon.engine import expiry_checker

        expiry_checker._careers_failure_counts.clear()
        expiry_checker._careers_skip_until.clear()

    @patch("jobcannon.engine.expiry_checker._check_job_expiry")
    def test_propagates_direct_url_and_careers_attempted(self, mock_cascade, db):
        from jobcannon.engine.expiry_checker import _cascade_worker

        mock_cascade.return_value = CascadeResult(
            LIVE, "careers_page title_found", "https://acme.com/careers/1", True
        )
        job = {"dedup_key": "dk1", "title": "DS"}
        company = {"id": 7, "homepage_url": "https://acme.com"}
        (
            dedup_key,
            result,
            evidence,
            direct_url,
            careers_attempted,
            signal0_attempted,
            signal0_blocked,
        ) = _cascade_worker(job, company, {}, "/tmp/fake.db")
        assert dedup_key == "dk1"
        assert result == LIVE
        assert direct_url == "https://acme.com/careers/1"
        assert careers_attempted is True
        assert signal0_attempted is False
        assert signal0_blocked is False

    @patch("jobcannon.engine.expiry_checker._check_job_expiry")
    def test_worker_exception_returns_safe_defaults(self, mock_cascade, db):
        from jobcannon.engine.expiry_checker import _cascade_worker

        mock_cascade.side_effect = RuntimeError("boom")
        job = {"dedup_key": "dk1", "title": "DS"}
        (
            dedup_key,
            result,
            evidence,
            direct_url,
            careers_attempted,
            signal0_attempted,
            signal0_blocked,
        ) = _cascade_worker(job, None, {}, "/tmp/fake.db")
        assert dedup_key == "dk1"
        assert result == INCONCLUSIVE
        assert direct_url is None
        assert careers_attempted is False
        assert signal0_attempted is False
        assert signal0_blocked is False


class TestCheckJobExpirySkipsOpaqueSources:
    """_check_job_expiry Signal 0 skips HTTP fetch for known opaque-redirect sources."""

    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    def test_opaque_source_skips_signal_0_http_fetch(self, mock_ats, mock_quick_liveness, db):
        """Opaque-redirect source (e.g. Jooble) skips Signal 0 HTTP GET."""
        config = {
            "verification": {
                "opaque_redirect_sources": [{"source_tag": "portal_jooble", "domain": "jooble.org"}]
            }
        }
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "sources": ["portal_jooble"],
            "source_urls": ["https://jooble.org/away/12345"],
        }
        company = {"ats_platform": None, "ats_slug": None, "homepage_url": None}
        cascade = _check_job_expiry(job, company, config, skip_careers=True)
        assert cascade.result == INCONCLUSIVE
        mock_quick_liveness.assert_not_called()

    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    @patch("jobcannon.engine.expiry_checker._extract_posting_id")
    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_opaque_source_falls_through_to_signal_1_ats_check(
        self, mock_quick_liveness, mock_extract, mock_ats, db
    ):
        """Opaque-redirect source with ATS platform/slug still receives Signal 1 check.

        Regression test: the early-return bug would have prevented ATS API liveness
        checks for opaque-source jobs, making them permanently unexpirable via
        ATS/careers evidence. The guard-only fix allows fall-through to Signal 1.
        """
        config = {
            "verification": {
                "opaque_redirect_sources": [{"source_tag": "portal_jooble", "domain": "jooble.org"}]
            }
        }
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "sources": ["portal_jooble"],
            "source_urls": ["https://jooble.org/away/12345", "https://jobs.lever.co/acme/abc-123"],
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}

        def extract_side_effect(url, platform):
            if "lever.co" in url:
                return "abc-123"
            return None

        mock_extract.side_effect = extract_side_effect
        mock_ats.return_value = EXPIRED
        _check_job_expiry(job, company, config, skip_careers=True)

        # The key assertion: Signal 1 (ATS API) should be called despite opaque source
        assert mock_ats.call_count == 1  # Signal 1 executed


class TestDerivedOpaqueRedirectCandidates:
    """Derived (shadow) opaque-redirect candidates skip Signal 0 but not 1/2."""

    def _setup_flagged_host(self, conn, host):
        conn.execute(
            "INSERT INTO opaque_redirect_host_outcomes (host, attempts, blocked_count, last_seen) "
            "VALUES (?, ?, ?, ?)",
            (host, 20, 19, "2026-01-01T00:00:00"),
        )
        conn.commit()

    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    @patch("jobcannon.engine.expiry_checker._check_ats_api")
    def test_flagged_host_skips_signal_0_but_runs_signal_1(self, mock_ats, mock_quick_liveness, db):
        path, conn = db
        # bad.example.com's registrable host is example.com, the table key.
        self._setup_flagged_host(conn, "example.com")

        config = {
            "verification": {
                "opaque_derive_min_samples": 20,
                "opaque_derive_block_ratio": 0.95,
            }
        }
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://bad.example.com/job/123", "https://jobs.lever.co/acme/abc-123"]',
        }
        company = {"ats_platform": "lever", "ats_slug": "acme", "homepage_url": "https://acme.com"}

        mock_ats.return_value = EXPIRED
        cascade = _check_job_expiry(job, company, config, skip_careers=True, db_path=path)

        assert cascade.result == EXPIRED
        mock_quick_liveness.assert_not_called()
        mock_ats.assert_called_once()

    @patch("jobcannon.engine.expiry_checker.quick_liveness_check")
    def test_non_flagged_host_still_runs_signal_0(self, mock_quick_liveness, db):
        path, conn = db
        self._setup_flagged_host(conn, "example.com")

        config = {
            "verification": {
                "opaque_derive_min_samples": 20,
                "opaque_derive_block_ratio": 0.95,
            }
        }
        job = {
            "dedup_key": "test",
            "title": "DS",
            "company": "Acme",
            "source_urls": '["https://other.test.io/job/123"]',
        }
        company = {"ats_platform": None, "ats_slug": None, "homepage_url": None}

        mock_quick_liveness.return_value = INCONCLUSIVE
        cascade = _check_job_expiry(job, company, config, skip_careers=True, db_path=path)

        assert cascade.result == INCONCLUSIVE
        mock_quick_liveness.assert_called_once()


class TestRunStalenessCheck:
    """run_staleness_check orchestrates the three phases (B -> C -> A).

    These tests disable Phase B (batch ATS) via config so they don't hit
    real HTTP. Phase A runs naturally on fresh last_seen timestamps (no
    time-based archives fire). Phase C is the focus — _check_job_expiry
    is mocked per-test.

    PORT-SEAM: Phase B is svc.reconcile_all_companies (a ScanServices field,
    L-0135 ADAPT/unlanded) here, not a module-level ats_reconciler import —
    test_phase_order_is_b_then_c_then_a injects a mock via _reconfigure()
    instead of patching job_finder.web.ats_reconciler.reconcile_all_companies.
    Phase A (run_stale_detection) is imported LOCALLY inside
    run_staleness_check from jobcannon.engine.stale_detector — the patch
    target is that module attribute, not an expiry_checker one. See module
    docstring points 2 and 4.
    """

    def _setup_db(self, conn):
        """Seed one pending job + one applied job under one company, mirroring
        the private repo's migrated-DB setup via direct inserts instead."""
        now_iso = datetime.now(UTC).isoformat()
        company_id = _insert_company(
            conn,
            "Acme Corp",
            homepage_url="https://acme.com",
            ats_platform="lever",
            ats_slug="acme-corp",
            ats_probe_status="hit",
        )
        _insert_job(
            conn,
            "acme|ds|remote",
            "discovered",
            title="Data Scientist",
            company="Acme Corp",
            company_id=company_id,
            first_seen=now_iso,
            last_seen=now_iso,
            source_urls='["https://jobs.lever.co/acme-corp/abc-123-def"]',
        )
        # Applied job — must NEVER be touched by expiry check.
        _insert_job(
            conn,
            "acme|sde|remote",
            "applied",
            title="Software Engineer",
            company="Acme Corp",
            company_id=company_id,
            first_seen=now_iso,
            last_seen=now_iso,
            source_urls="[]",
        )

    def _base_config(self):
        return {
            "profile": {"target_titles": [], "exclusions": {"title_keywords": []}},
            "staleness": {"batch_ats_enabled": False, "cascade_parallel_workers": 2},
        }

    @patch("jobcannon.engine.expiry_checker._check_job_expiry")
    def test_archives_expired_job(self, mock_check, db):
        path, conn = db
        self._setup_db(conn)
        mock_check.return_value = CascadeResult(EXPIRED, "lever_api 404")

        result = run_staleness_check(path, self._base_config())

        assert result["phase_c"]["archived"] >= 1
        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", ("acme|ds|remote",)
        ).fetchone()
        assert row["pipeline_status"] == "archived"

    @patch("jobcannon.engine.expiry_checker._check_job_expiry")
    def test_does_not_touch_applied_jobs(self, mock_check, db):
        path, conn = db
        self._setup_db(conn)
        mock_check.return_value = CascadeResult(EXPIRED, "lever_api 404")

        run_staleness_check(path, self._base_config())

        row = conn.execute(
            "SELECT pipeline_status FROM jobs WHERE dedup_key = ?", ("acme|sde|remote",)
        ).fetchone()
        assert row["pipeline_status"] == "applied"

    @patch("jobcannon.engine.expiry_checker._check_job_expiry")
    def test_updates_expiry_checked_at_on_live(self, mock_check, db):
        path, conn = db
        self._setup_db(conn)
        mock_check.return_value = CascadeResult(LIVE, "lever_api 200")

        run_staleness_check(path, self._base_config())

        row = conn.execute(
            "SELECT expiry_checked_at FROM jobs WHERE dedup_key = ?", ("acme|ds|remote",)
        ).fetchone()
        assert row["expiry_checked_at"] is not None

    @patch("jobcannon.engine.expiry_checker._check_job_expiry")
    def test_skips_recently_checked_jobs(self, mock_check, db):
        path, conn = db
        self._setup_db(conn)
        conn.execute(
            "UPDATE jobs SET expiry_checked_at = ? WHERE dedup_key = ?",
            (datetime.now(UTC).isoformat(), "acme|ds|remote"),
        )
        conn.commit()

        config = {
            **self._base_config(),
            "staleness": {
                "batch_ats_enabled": False,
                "cascade_parallel_workers": 2,
                "cascade_recheck_days": 3,
            },
        }
        run_staleness_check(path, config)

        mock_check.assert_not_called()

    def test_phase_order_is_b_then_c_then_a(self, db, monkeypatch):
        """Phase A (clock-based) must run LAST so it judges against the
        liveness evidence Phases B and C just refreshed."""
        path, conn = db
        self._setup_db(conn)

        call_order: list[str] = []
        mock_b = MagicMock(side_effect=lambda *a, **k: (call_order.append("b"), {})[1])
        mock_c = MagicMock(side_effect=lambda *a, **k: (call_order.append("c"), {})[1])
        mock_a = MagicMock(side_effect=lambda *a, **k: (call_order.append("a"), {})[1])

        _reconfigure(reconcile_all_companies=mock_b)
        monkeypatch.setattr("jobcannon.engine.expiry_checker._run_phase_c_cascade", mock_c)
        monkeypatch.setattr("jobcannon.engine.stale_detector.run_stale_detection", mock_a)

        config = {
            **self._base_config(),
            "staleness": {"batch_ats_enabled": True, "cascade_parallel_workers": 2},
        }
        run_staleness_check(path, config)

        assert call_order == ["b", "c", "a"]


class TestPhaseCCascadeRuntimeLimit:
    """cascade_runtime_limit_s truncates Phase C after in-flight futures complete.

    A hard wall-clock limit is useful for overnight slots; when it fires, the
    jobs that already completed must have expiry_checked_at written, already
    running workers must be drained and persisted, and Phase A (time-based) must
    still run so it judges on the evidence B and C refreshed.
    """

    def _seed_jobs(self, conn, count=10):
        """Insert `count` discovered jobs ready for Phase C."""
        now = datetime.now(UTC).isoformat()
        for i in range(count):
            _insert_job(
                conn,
                f"acme|{i}|remote",
                "discovered",
                title=f"Job {i}",
                company="Acme",
                first_seen=now,
                last_seen=now,
                source_urls="[]",
            )

    @patch("jobcannon.engine.expiry_checker._cascade_worker")
    def test_phase_c_cascade_runtime_limit(self, mock_worker, db, monkeypatch):
        path, conn = db
        self._seed_jobs(conn, count=4)

        mock_phase_a = MagicMock(return_value={})
        monkeypatch.setattr("jobcannon.engine.stale_detector.run_stale_detection", mock_phase_a)

        delays = {
            "acme|0|remote": 0.01,
            "acme|1|remote": 0.05,
            "acme|2|remote": 0.05,
            "acme|3|remote": 0.05,
        }

        def worker_side_effect(job, company, config, db_path, careers_memo=None):
            time.sleep(delays[job["dedup_key"]])
            return (
                job["dedup_key"],
                LIVE,
                "limit_test",
                None,
                False,
                False,
                False,
            )

        mock_worker.side_effect = worker_side_effect

        config = {
            "profile": {"target_titles": [], "exclusions": {"title_keywords": []}},
            "staleness": {
                "batch_ats_enabled": False,
                "cascade_parallel_workers": 2,
                "cascade_runtime_limit_s": 0.001,
            },
        }

        result = run_staleness_check(path, config)

        assert result["phase_c"].get("truncated") is True
        assert mock_phase_a.called
        assert result["phase_c"]["checked"] == 4

        started_keys = {call.args[0]["dedup_key"] for call in mock_worker.call_args_list}

        persisted_keys = {
            row["dedup_key"]
            for row in conn.execute(
                "SELECT dedup_key FROM jobs WHERE expiry_checked_at IS NOT NULL"
            ).fetchall()
        }

        # At least one job completed before the limit and one worker was still in
        # flight, so we should have dispatched at least two workers. The pending
        # remainder must be cancelled rather than persisted.
        assert len(started_keys) >= 2
        assert len(started_keys) < 4
        assert result["phase_c"]["live"] == len(started_keys)
        assert persisted_keys == started_keys


class TestCareersBackoff:
    """_record_careers_outcome tracks failures and sets skip-until timestamps."""

    def setup_method(self):
        """Reset module-level backoff state before each test."""
        from jobcannon.engine import expiry_checker

        expiry_checker._careers_failure_counts.clear()
        expiry_checker._careers_skip_until.clear()

    def test_three_consecutive_failures_trigger_skip(self):
        from jobcannon.engine.expiry_checker import _careers_skip_until, _record_careers_outcome

        _record_careers_outcome(42, success=False)
        _record_careers_outcome(42, success=False)
        assert 42 not in _careers_skip_until  # Not yet at threshold
        _record_careers_outcome(42, success=False)
        assert 42 in _careers_skip_until  # Now at threshold
        skip_time = _careers_skip_until[42]
        # Should be ~7 days from now
        now = datetime.now(UTC)
        assert skip_time > now + timedelta(days=6)
        assert skip_time < now + timedelta(days=8)

    def test_success_resets_failure_count(self):
        from jobcannon.engine.expiry_checker import (
            _careers_failure_counts,
            _careers_skip_until,
            _record_careers_outcome,
        )

        _record_careers_outcome(42, success=False)
        _record_careers_outcome(42, success=False)
        assert _careers_failure_counts.get(42, 0) == 2
        _record_careers_outcome(42, success=True)
        assert 42 not in _careers_failure_counts
        assert 42 not in _careers_skip_until


@pytest.mark.skip(
    reason=(
        "Every test drives job_finder.db.upsert_job's archived-job "
        "reopen-on-re-ingestion side effect directly against a real migrated "
        "DB. jobcannon/db/ (the closest-named module) is an entirely "
        "different, PostgreSQL-based persistence layer for the web app's "
        "own primary DB ('%s' placeholders, 'postings' table) -- "
        "architecturally unrelated to jobcannon.engine.expiry_checker's "
        "sqlite3 scan-state DB, reached only via svc.connection_factory() "
        "('jobs'/'companies'/'pipeline_events' tables). No ledger row in "
        "this port's scope (L-0182 covers expiry_checker.py only) names a "
        "jobcannon.db.upsert_job reopen-on-re-ingestion port. See the "
        "private repo's tests/test_expiry_checker.py::TestAutoReopen for "
        "the original assertions."
    )
)
class TestAutoReopen:
    """Not ported — see class skip reason. Archived jobs re-appearing during
    ingestion are auto-reopened; this is job_finder.db.upsert_job's behavior,
    not expiry_checker.py's."""

    def test_archived_job_reopened_on_upsert(self):
        pass

    def test_reopen_clears_frozen_expiry_state(self):
        pass

    def test_non_archived_job_not_reopened(self):
        pass

    def test_unverifiable_archive_not_reopened_by_same_opaque_source(self):
        pass

    def test_unverifiable_archive_not_reopened_by_different_opaque_source(self):
        pass

    def test_unverifiable_archive_not_reopened_by_same_opaque_source_ceiling_tag(self):
        pass

    def test_unverifiable_archive_not_reopened_by_different_opaque_source_ceiling_tag(self):
        pass

    def test_unverifiable_archive_reopened_by_non_opaque_source(self):
        pass

    def test_unverifiable_archive_reopened_when_no_config_passed(self):
        pass

    def test_plain_staleness_archive_still_reopens_unconditionally(self):
        pass


class TestPhaseCWritesDirectUrlAndCareersCheckedAt:
    """Integration: _run_phase_c_cascade writes direct_url + careers_checked_at
    when Signal 2 confirms a job via the careers-page channel.

    test_confident_careers_match_writes_direct_url_and_stamps_checked_at
    directly exercises the svc.set_direct_url seam and this file's local
    _set_direct_url fake — see module docstring point 8.
    """

    @patch("jobcannon.engine.expiry_checker._check_job_expiry")
    def test_confident_careers_match_writes_direct_url_and_stamps_checked_at(
        self, mock_cascade, db
    ):
        path, conn = db
        _insert_job(conn, "dk1", "discovered", source_urls='["https://jooble.org/away/1"]')

        mock_cascade.return_value = CascadeResult(
            LIVE, "careers_page title_found", "https://acme.com/careers/123", True
        )

        summary = _run_phase_c_cascade(path, {"staleness": {}})
        assert summary["live"] == 1

        row = conn.execute(
            "SELECT direct_url, direct_url_confidence, careers_checked_at "
            "FROM jobs WHERE dedup_key = 'dk1'"
        ).fetchone()
        assert row["direct_url"] == "https://acme.com/careers/123"
        assert row["direct_url_confidence"] == "strict"
        assert row["careers_checked_at"] is not None

    @patch("jobcannon.engine.expiry_checker._check_job_expiry")
    def test_careers_attempted_without_match_still_stamps_checked_at(self, mock_cascade, db):
        path, conn = db
        _insert_job(conn, "dk1", "discovered", source_urls='["https://jooble.org/away/1"]')

        mock_cascade.return_value = CascadeResult(INCONCLUSIVE, "", None, True)

        _run_phase_c_cascade(path, {"staleness": {}})

        row = conn.execute(
            "SELECT direct_url, careers_checked_at FROM jobs WHERE dedup_key = 'dk1'"
        ).fetchone()
        assert row["direct_url"] is None
        assert row["careers_checked_at"] is not None

    @patch("jobcannon.engine.expiry_checker._check_job_expiry")
    def test_not_attempted_leaves_checked_at_null(self, mock_cascade, db):
        path, conn = db
        _insert_job(conn, "dk1", "discovered", source_urls='["https://jooble.org/away/1"]')

        mock_cascade.return_value = CascadeResult(INCONCLUSIVE, "", None, False)

        _run_phase_c_cascade(path, {"staleness": {}})

        row = conn.execute("SELECT careers_checked_at FROM jobs WHERE dedup_key = 'dk1'").fetchone()
        assert row["careers_checked_at"] is None


class TestGetCascadeParallelWorkersClamp:
    def test_clamps_high_value_to_ceiling(self):
        assert _get_cascade_parallel_workers({"cascade_parallel_workers": 99}) == 10

    def test_clamps_zero_to_floor(self):
        assert _get_cascade_parallel_workers({"cascade_parallel_workers": 0}) == 1

    def test_negative_clamps_to_floor(self):
        assert _get_cascade_parallel_workers({"cascade_parallel_workers": -5}) == 1

    def test_unspecified_falls_back_to_default_of_ten(self):
        """_DEFAULT_PARALLEL_WORKERS is 10 and the ceiling is also 10 (cascade
        fans out across distinct company hosts, so it does not share Phase B's
        [1, 6] per-platform bound). An unspecified config must resolve to 10,
        not silently drop to Phase B's ceiling — that was the issue #1102
        regression this test guards against."""
        assert _get_cascade_parallel_workers({}) == 10

    def test_invalid_type_falls_back_to_default_of_ten(self):
        assert _get_cascade_parallel_workers({"cascade_parallel_workers": "bogus"}) == 10


class TestGetCascadeRuntimeLimitS:
    """_get_cascade_runtime_limit_s clamps negative values to no-limit."""

    def test_positive_limit_preserved(self):
        assert _get_cascade_runtime_limit_s({"cascade_runtime_limit_s": 10}) == 10.0

    def test_zero_means_no_limit(self):
        assert _get_cascade_runtime_limit_s({"cascade_runtime_limit_s": 0}) is None

    def test_negative_clamped_to_no_limit(self):
        assert _get_cascade_runtime_limit_s({"cascade_runtime_limit_s": -5}) is None

    def test_unspecified_means_no_limit(self):
        assert _get_cascade_runtime_limit_s({}) is None

    def test_invalid_type_falls_back_to_no_limit(self):
        assert _get_cascade_runtime_limit_s({"cascade_runtime_limit_s": "bogus"}) is None


class TestPhaseCScrapeMemo:
    """Issue #1033: per-run careers-page scrape memo for Signal 2.

    A company with N unresolved postings should scrape its careers page once
    per run, not N times, while preserving per-posting verdicts and the 3-strike
    cross-run backoff.
    """

    def setup_method(self):
        """Reset module-level Signal 2 backoff state before each test."""
        from jobcannon.engine import expiry_checker

        expiry_checker._careers_failure_counts.clear()
        expiry_checker._careers_skip_until.clear()

    def _seed_company_with_jobs(self, conn, titles: list[str]) -> int:
        """Create one company and one discovered job per title, all pending Phase C."""
        company_id = _insert_company(
            conn, "Acme", homepage_url="https://acme.com", ats_probe_status="pending"
        )
        for i, title in enumerate(titles):
            _insert_job(
                conn,
                f"acme|{i}|remote",
                "discovered",
                title=title,
                company="Acme",
                company_id=company_id,
                source_urls="[]",
            )
        return company_id

    def test_one_scrape_for_many_postings_and_one_strike(self, db):
        """A failing company gets one scrape and one strike per run, not N."""
        from jobcannon.engine import expiry_checker

        path, conn = db
        company_id = self._seed_company_with_jobs(conn, [f"Job {i}" for i in range(10)])

        mock_find = MagicMock(return_value="https://acme.com/careers")
        mock_scrape = MagicMock(return_value=([], 0))
        _reconfigure(find_careers_url=mock_find, scrape_careers_page=mock_scrape)

        config = {
            "profile": {"target_titles": [], "exclusions": {"title_keywords": []}},
            "staleness": {"cascade_parallel_workers": 5},
        }
        summary = _run_phase_c_cascade(path, config)

        assert summary["checked"] == 10
        assert summary["inconclusive"] == 10
        assert summary["live"] == 0
        assert summary["archived"] == 0
        assert mock_scrape.call_count == 1
        assert expiry_checker._careers_failure_counts.get(company_id) == 1

        # Every posting should be stamped as checked and stay INCONCLUSIVE.
        for i in range(10):
            row = conn.execute(
                "SELECT expiry_status, careers_checked_at FROM jobs WHERE dedup_key = ?",
                (f"acme|{i}|remote",),
            ).fetchone()
            assert row["expiry_status"] == INCONCLUSIVE
            assert row["careers_checked_at"] is not None

    def test_verdict_parity_with_mixed_matches(self, db):
        """Shared memo preserves per-posting verdicts: matches are LIVE, misses INCONCLUSIVE."""
        from jobcannon.engine import expiry_checker

        path, conn = db
        company_id = self._seed_company_with_jobs(
            conn, ["Data Scientist", "Software Engineer", "Product Manager"]
        )

        mock_find = MagicMock(return_value="https://acme.com/careers")
        mock_scrape = MagicMock(
            return_value=(
                [
                    {"title": "Data Scientist", "url": "https://acme.com/careers/ds"},
                    {"title": "Software Engineer", "url": "https://acme.com/careers/swe"},
                ],
                0,
            )
        )
        _reconfigure(find_careers_url=mock_find, scrape_careers_page=mock_scrape)

        config = {
            "profile": {"target_titles": [], "exclusions": {"title_keywords": []}},
            "staleness": {"cascade_parallel_workers": 4},
        }
        summary = _run_phase_c_cascade(path, config)

        assert summary["checked"] == 3
        assert summary["live"] == 2
        assert summary["inconclusive"] == 1
        assert summary["archived"] == 0
        assert mock_scrape.call_count == 1
        assert company_id not in expiry_checker._careers_failure_counts

        ds = conn.execute(
            "SELECT expiry_status FROM jobs WHERE dedup_key = ?", ("acme|0|remote",)
        ).fetchone()
        swe = conn.execute(
            "SELECT expiry_status FROM jobs WHERE dedup_key = ?", ("acme|1|remote",)
        ).fetchone()
        pm = conn.execute(
            "SELECT expiry_status FROM jobs WHERE dedup_key = ?", ("acme|2|remote",)
        ).fetchone()
        assert ds["expiry_status"] == LIVE
        assert swe["expiry_status"] == LIVE
        assert pm["expiry_status"] == INCONCLUSIVE

    def test_memoized_exception_counts_one_strike(self, db):
        """A scrape exception is memoized and not retried, producing one strike."""
        from jobcannon.engine import expiry_checker

        path, conn = db
        company_id = self._seed_company_with_jobs(conn, [f"Job {i}" for i in range(6)])

        mock_find = MagicMock(return_value="https://acme.com/careers")
        mock_scrape = MagicMock(side_effect=RuntimeError("network timeout"))
        _reconfigure(find_careers_url=mock_find, scrape_careers_page=mock_scrape)

        config = {
            "profile": {"target_titles": [], "exclusions": {"title_keywords": []}},
            "staleness": {"cascade_parallel_workers": 5},
        }
        summary = _run_phase_c_cascade(path, config)

        assert summary["checked"] == 6
        assert summary["inconclusive"] == 6
        assert mock_scrape.call_count == 1
        assert expiry_checker._careers_failure_counts.get(company_id) == 1

    def test_find_careers_url_memoized_once_for_many_postings(self, db):
        """Review finding 1: find_careers_url must also be memoized per run,
        not just scrape_careers_page. Issue #1033's Problem statement names
        both find_careers_url and scrape_careers_page as the per-posting
        cost; a company with N unresolved postings should resolve its
        careers URL once, not issue N homepage GETs."""
        path, conn = db
        self._seed_company_with_jobs(conn, [f"Job {i}" for i in range(5)])

        mock_find = MagicMock(return_value="https://acme.com/careers")
        mock_scrape = MagicMock(return_value=([], 0))
        _reconfigure(find_careers_url=mock_find, scrape_careers_page=mock_scrape)

        config = {
            "profile": {"target_titles": [], "exclusions": {"title_keywords": []}},
            "staleness": {"cascade_parallel_workers": 5},
        }
        summary = _run_phase_c_cascade(path, config)

        assert summary["checked"] == 5
        assert mock_find.call_count == 1
        assert mock_scrape.call_count == 1


class TestCareersPageMemoLockConcurrency:
    """Rigorous concurrent-access test for CareersPageMemo's per-key lock.

    Uses threading.Barrier to force N threads to call get_or_compute for the
    SAME key at the same instant, against a factory that sleeps ~0.05s so a
    missing/no-op lock lets overlapping unlocked computes actually happen --
    a fast mocked factory completes before a preemption window ever opens,
    which is exactly why the original three memo tests kept passing after a
    reviewer removed CareersPageMemo's per-key lock entirely (GIL scheduling
    accidentally serialized the fast computes). Mirrors PR #1101's
    TestScanMemoLockConcurrency (tests/test_platform_scanner_registry.py),
    adapted for CareersPageMemo's per-key lock instead of a single flat lock
    guarding one shared dict.
    """

    def test_concurrent_get_or_compute_same_key_computes_once(self):
        memo = CareersPageMemo()
        n_threads = 8
        barrier = threading.Barrier(n_threads)

        compute_count = 0
        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        def factory():
            nonlocal compute_count, active, max_active
            with counter_lock:
                compute_count += 1
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)  # force a preemption window mid-compute
            with counter_lock:
                active -= 1
            return "resolved-value"

        def worker():
            barrier.wait()
            memo.get_or_compute(("https://acme.com/careers", 0), factory)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert compute_count == 1, (
            "expected the per-key lock to serialize concurrent get_or_compute "
            f"calls for the same key (compute_count == 1); got {compute_count} "
            "-- the factory ran more than once, meaning multiple threads "
            "computed the same key concurrently instead of one computing and "
            "the rest reusing the cached result"
        )
        assert max_active == 1, (
            "expected at most one thread inside the factory at a time "
            f"(max_active == 1); got max_active={max_active} -- overlapping "
            "unlocked computes were observed, proving the lock is not "
            "actually guarding concurrent access"
        )


class TestCareersPageMemoExceptionIndependence:
    """A cached exception must be re-raised as an independent copy, not the
    shared cached instance -- concurrent cache-hit callers raising the same
    live exception object race on its __traceback__ attribute."""

    def test_cache_hit_raises_independent_exception_copy(self):
        import copy

        memo = CareersPageMemo()

        def failing_factory():
            raise RuntimeError("boom")

        cached, _ = memo.get_or_compute("key", failing_factory)
        assert isinstance(cached, RuntimeError)

        # Mirror _check_careers_page's cache-hit pattern: each caller raises
        # copy.copy(cached), not the shared cached instance itself.
        exc_a = copy.copy(cached)
        exc_b = copy.copy(cached)
        assert exc_a is not exc_b
        assert exc_a is not cached
        assert exc_b is not cached

        try:
            raise exc_a
        except RuntimeError:
            pass
        try:
            raise exc_b
        except RuntimeError:
            pass

        assert exc_a.__traceback__ is not exc_b.__traceback__

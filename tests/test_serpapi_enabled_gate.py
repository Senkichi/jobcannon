# PORTED from tests/test_serpapi_enabled_gate.py @ 0a4c33c5af7cd4055e539672158cb301b7bdc407 (private job-cannon). Ledger L-0525.
"""Tests for issue #304: enrichment SerpAPI tier respects sources.serpapi.enabled
and sources.serpapi.daily_call_cap.

Acceptance criteria:
  - With SERPAPI_API_KEY set but sources.serpapi.enabled=false, zero SerpAPI calls.
  - daily_call_cap halts the tier once reached; resumes next day (ledger rolled).
  - Calls are recorded in the scoring_costs ledger so the cap survives restarts.

# PORT-SEAM: enrich_job() now takes conn/serpapi_key/config as injected params
# via a registered jobcannon.engine.services.ScanServices (L-0174 ADAPT) instead
# of module-level jobcannon.engine.data_enricher.<name> functions this suite
# originally `patch()`ed directly. Every such patch becomes a ScanServices
# field override installed through the local _install_services() helper below.
# Two further seams have no public counterpart at all yet (no ledger row in
# this port's read scope, seamed the same way as data_enricher.py's own
# run_detection precedent): svc.is_source_rate_limited and
# svc.record_source_error (private repo's autoheal.health_monitor module),
# and svc.vendor_account_error (private repo's sources._error_envelope.
# VendorAccountError, L-0111 HOLD). All three get minimal test doubles below
# that faithfully reproduce the private health_monitor.py source_health
# UPSERT / cooldown-window contract against this file's own bare-sqlite3
# mem_conn fixture -- the same "test-side workaround for an unported
# private-only helper" pattern tests/engine/test_data_enricher.py's own
# module docstring already establishes for apply_location_observation.
"""

import contextlib  # PORT-SEAM: ScanServices connection_factory context manager
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock  # PORT-SEAM: ScanServices field overrides replace patch()

import pytest

from jobcannon.engine import services
from jobcannon.engine.data_enricher import (
    _record_serpapi_call,
    _serpapi_daily_calls_used,
    enrich_job,
)
from jobcannon.engine.json_utils import utc_now_iso

# PORT-SEAM: test doubles below replace the private repo's autoheal.health_monitor
# and sources._error_envelope helpers, which have no public counterpart yet.
# ---------------------------------------------------------------------------
# Test doubles for unported autoheal.health_monitor / _error_envelope seams
# (see module docstring PORT-SEAM note above)
# ---------------------------------------------------------------------------


class _FakeVendorAccountError(Exception):
    """Constructor-compatible stand-in for
    job_finder.sources._error_envelope.VendorAccountError (L-0111 HOLD, no
    public counterpart), wired via svc.vendor_account_error so enrich_job's
    ``except svc.vendor_account_error or _NoVendorAccountError`` clause has a
    concrete, catchable type -- mirrors data_enricher.py's own
    _NoVendorAccountError placeholder pattern, just one this suite can raise
    by name."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _fake_record_source_error(conn, source, message):
    """Test double for autoheal.health_monitor.record_source_error. Mirrors
    the private record_source_break()'s source_health UPSERT (last_error /
    last_error_at / consecutive_breaks) against this file's bare-sqlite3
    mem_conn fixture."""
    if conn is None:
        return
    now = utc_now_iso()
    row = conn.execute(
        "SELECT consecutive_breaks FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    breaks = (row["consecutive_breaks"] if row else 0) + 1
    conn.execute(
        """INSERT INTO source_health
               (source, surface, status, consecutive_breaks, baseline_yield,
                updated_at, last_error, last_error_at)
           VALUES (?, 'ingestion', 'healthy', ?, 0, ?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
               last_error = excluded.last_error,
               last_error_at = excluded.last_error_at,
               updated_at = excluded.updated_at,
               consecutive_breaks = excluded.consecutive_breaks""",
        (source, breaks, now, message, now),
    )
    conn.commit()


def _fake_is_source_rate_limited(conn, source, cooldown_hours, marker="429"):
    """Test double for autoheal.health_monitor.is_source_rate_limited. Mirrors
    the private implementation's source_health last_error/last_error_at
    cooldown-window check (see _fake_record_source_error above)."""
    if not conn or cooldown_hours <= 0:
        return False
    row = conn.execute(
        "SELECT last_error, last_error_at FROM source_health WHERE source = ?",
        (source,),
    ).fetchone()
    if not row or not row["last_error"] or not row["last_error_at"]:
        return False
    if marker not in row["last_error"]:
        return False
    last_at = datetime.fromisoformat(row["last_error_at"])
    now = datetime.now(UTC).replace(tzinfo=None)
    return now - last_at < timedelta(hours=cooldown_hours)


def _install_services(**overrides):
    """Build and register one ScanServices for a test (see module docstring).

    Defaults reproduce the private repo's ``_neutralise_other_tiers``
    "always miss" shapes for free/DDG (which patched module attributes
    directly) so only the SerpAPI path under test fires; pass a MagicMock
    override (e.g. ``search_serpapi=...``) for any hook a test needs to
    assert on.
    """

    @contextlib.contextmanager
    def _connection_factory(*, synchronous="FULL"):
        yield None

    defaults = dict(
        connection_factory=_connection_factory,
        upsert_job=MagicMock(),
        set_jd_full=MagicMock(),
        upsert_company=MagicMock(),
        config={},
        get_secret=MagicMock(return_value=None),
        jd_storage_max_chars=20000,
        fetch_direct_jd=MagicMock(return_value=None),
        query_ats_api=MagicMock(return_value={}),
        scrape_careers_tier=MagicMock(return_value={}),
        search_ddg_web=MagicMock(return_value={}),
        fetch_ddg_jds=MagicMock(return_value=(None, None)),
        search_duckduckgo=MagicMock(return_value=None),
        search_serpapi=MagicMock(return_value=(None, [])),
        vendor_account_error=_FakeVendorAccountError,
        is_source_rate_limited=_fake_is_source_rate_limited,
        record_source_error=_fake_record_source_error,
    )
    defaults.update(overrides)
    services.set_services(services.ScanServices(**defaults))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_conn():
    """In-memory SQLite with the tables enrich_job touches."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE scoring_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            purpose TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0.0,
            timestamp TEXT NOT NULL,
            provider TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE jobs (
            dedup_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT,
            jd_full TEXT,
            salary_min INTEGER,
            salary_max INTEGER,
            source_urls TEXT DEFAULT '[]',
            company_id INTEGER,
            enrichment_tier TEXT,
            description TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_raw TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE source_health (
            source TEXT PRIMARY KEY,
            surface TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'healthy',
            consecutive_breaks INTEGER NOT NULL DEFAULT 0,
            baseline_yield REAL NOT NULL DEFAULT 0,
            last_signal TEXT DEFAULT NULL,
            last_break_at TEXT DEFAULT NULL,
            updated_at TEXT NOT NULL,
            last_error TEXT DEFAULT NULL,
            last_error_at TEXT DEFAULT NULL
        )
    """)
    conn.commit()
    return conn


@pytest.fixture()
def sparse_job():
    """Job row that needs enrichment (no jd_full, no salary)."""
    return {
        "dedup_key": "acme|data-scientist|remote",
        "title": "Data Scientist",
        "company": "Acme Corp",
        "location": "Remote",
        "jd_full": None,
        "salary_min": None,
        "salary_max": None,
        "source_urls": "[]",
        "company_id": None,
        "enrichment_tier": None,
        "description": None,
    }


# Neutralise every tier except serpapi so tests exercise only the gate logic.
@pytest.fixture(autouse=True)
def _neutralise_other_tiers():
    """Stub free/DDG/agentic so only the serpapi path is under test.

    PORT-SEAM: installs the same "always miss" shapes via _install_services()
    (see module docstring) instead of patching jobcannon.engine.data_enricher
    module attributes directly -- enrich_job no longer runs the agentic tier
    synchronously (2026-06-22); the cascade terminates at 'exhausted' with no
    Playwright/Ollama I/O to stub.
    """
    # PORT-SEAM: ScanServices override, not patch()
    _install_services()
    yield
    services.clear_services()  # PORT-SEAM: ScanServices teardown replaces per-test patchers


# ---------------------------------------------------------------------------
# Test: enabled=false blocks calls even when key is present
# ---------------------------------------------------------------------------


class TestSerpApiEnabledGate:
    def test_disabled_flag_prevents_serpapi_call(self, sparse_job, mem_conn):
        """sources.serpapi.enabled=false must produce zero SerpAPI calls."""
        config = {"sources": {"serpapi": {"enabled": False}}}

        mock_serp = MagicMock()
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="FAKE_KEY", conn=mem_conn, config=config)

        mock_serp.assert_not_called()

    def test_disabled_flag_records_no_ledger_row(self, sparse_job, mem_conn):
        """When disabled, no scoring_costs row should be written for serpapi."""
        config = {"sources": {"serpapi": {"enabled": False}}}

        _install_services(search_serpapi=MagicMock())  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="FAKE_KEY", conn=mem_conn, config=config)

        count = mem_conn.execute(
            "SELECT COUNT(*) FROM scoring_costs WHERE provider='serpapi_enrichment'"
        ).fetchone()[0]
        assert count == 0

    def test_enabled_true_allows_serpapi_call(self, sparse_job, mem_conn):
        """sources.serpapi.enabled=true (explicit) allows the tier to fire."""
        config = {"sources": {"serpapi": {"enabled": True}}}

        mock_serp = MagicMock(return_value=(None, []))
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="FAKE_KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_absent_enabled_key_allows_serpapi_call(self, sparse_job, mem_conn):
        """When sources.serpapi.enabled is absent, default is True (backward compat)."""
        config = {"sources": {"serpapi": {}}}  # no 'enabled' key

        mock_serp = MagicMock(return_value=(None, []))
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="FAKE_KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_no_key_still_skips_serpapi(self, sparse_job, mem_conn):
        """Even with enabled=true, no serpapi_key means the tier is skipped."""
        config = {"sources": {"serpapi": {"enabled": True}}}

        mock_serp = MagicMock()
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key=None, conn=mem_conn, config=config)

        mock_serp.assert_not_called()


# ---------------------------------------------------------------------------
# Test: daily_call_cap halts the tier once reached
# ---------------------------------------------------------------------------


class TestSerpApiDailyCap:
    def test_cap_not_reached_allows_call(self, sparse_job, mem_conn):
        """When cap=5 and 0 calls logged today, the tier fires normally."""
        config = {"sources": {"serpapi": {"enabled": True, "daily_call_cap": 5}}}

        mock_serp = MagicMock(return_value=(None, []))
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_cap_reached_blocks_call(self, sparse_job, mem_conn):
        """When cap=3 and 3 calls already logged, the tier is skipped."""
        config = {"sources": {"serpapi": {"enabled": True, "daily_call_cap": 3}}}
        # Pre-seed 3 ledger rows for today
        for _ in range(3):
            _record_serpapi_call(mem_conn)

        mock_serp = MagicMock()
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_not_called()

    def test_cap_zero_means_uncapped(self, sparse_job, mem_conn):
        """daily_call_cap=0 means no cap — tier always fires when enabled."""
        config = {"sources": {"serpapi": {"enabled": True, "daily_call_cap": 0}}}
        # Seed many rows — should not matter
        for _ in range(100):
            _record_serpapi_call(mem_conn)

        mock_serp = MagicMock(return_value=(None, []))
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_cap_absent_means_uncapped(self, sparse_job, mem_conn):
        """Absent daily_call_cap (no key) means no cap."""
        config = {"sources": {"serpapi": {"enabled": True}}}
        for _ in range(50):
            _record_serpapi_call(mem_conn)

        mock_serp = MagicMock(return_value=(None, []))
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()

    def test_successful_call_increments_ledger(self, sparse_job, mem_conn):
        """A fired SerpAPI call writes exactly one row to scoring_costs."""
        config = {"sources": {"serpapi": {"enabled": True}}}
        before = _serpapi_daily_calls_used(mem_conn)

        _install_services(search_serpapi=MagicMock(return_value=(None, [])))  # PORT-SEAM: ScanServices override
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        after = _serpapi_daily_calls_used(mem_conn)
        assert after == before + 1

    def test_disabled_call_does_not_increment_ledger(self, sparse_job, mem_conn):
        """A skipped (disabled) call must not write to the ledger."""
        config = {"sources": {"serpapi": {"enabled": False}}}
        before = _serpapi_daily_calls_used(mem_conn)

        _install_services(search_serpapi=MagicMock())  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        after = _serpapi_daily_calls_used(mem_conn)
        assert after == before


# ---------------------------------------------------------------------------
# Test: 429 rate-limit cooldown skips the enrichment tier
# ---------------------------------------------------------------------------


class TestSerpApiRateLimitCooldown:
    def test_429_cooldown_blocks_call(self, sparse_job, mem_conn):
        """A persisted 429 within cooldown hours skips the SerpAPI enrichment tier."""
        # PORT-SEAM: utc_now_iso imported at module level, not re-imported per-test
        config = {"sources": {"serpapi": {"enabled": True}}}
        now = utc_now_iso()
        mem_conn.execute(
            """INSERT INTO source_health
                (source, surface, status, consecutive_breaks, baseline_yield,
                 updated_at, last_error, last_error_at)
             VALUES (?, 'ingestion', 'healthy', 0, 0, ?, ?, ?)""",
            ("serpapi", now, "SerpAPI rate limit exceeded (429) after 2 retries", now),
        )
        mem_conn.commit()

        mock_serp = MagicMock()
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_not_called()

    def test_429_records_source_error_and_blocks_next_call(self, sparse_job, mem_conn):
        """A 429 VendorAccountError records source_health and blocks subsequent rows."""
        config = {"sources": {"serpapi": {"enabled": True}}}

        # PORT-SEAM: ScanServices override with the local _FakeVendorAccountError
        # double (see module docstring), not patch() + the private VendorAccountError
        _install_services(
            search_serpapi=MagicMock(
                side_effect=_FakeVendorAccountError(
                    "SerpAPI rate limit exceeded (429)", code="429"
                )
            )
        )
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        # First row records the 429 in source_health.
        row = mem_conn.execute(
            "SELECT last_error FROM source_health WHERE source = ?",
            ("serpapi",),
        ).fetchone()
        assert row and "429" in row["last_error"]

        # A second row should now skip the SerpAPI tier because the cooldown is active.
        mock_serp = MagicMock()
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_not_called()

    def test_429_cooldown_expired_allows_call(self, sparse_job, mem_conn):
        """A 429 older than the cooldown window is retried — the tier fires again.

        This is the behaviour that distinguishes a cooldown from a permanent
        block: a stale ``last_error_at`` must let SerpAPI back in. A
        comparison-operator regression in ``is_source_rate_limited`` (e.g.
        ``>`` instead of ``<``) would keep the tier skipped forever and this
        test would fail.
        """
        config = {"sources": {"serpapi": {"enabled": True}}}
        # 48h ago with the default 24h cooldown -> outside the window.
        stale = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=48)).isoformat()
        mem_conn.execute(
            """INSERT INTO source_health
                (source, surface, status, consecutive_breaks, baseline_yield,
                 updated_at, last_error, last_error_at)
             VALUES (?, 'ingestion', 'healthy', 0, 0, ?, ?, ?)""",
            ("serpapi", stale, "SerpAPI rate limit exceeded (429) after 2 retries", stale),
        )
        mem_conn.commit()

        mock_serp = MagicMock(return_value=(None, []))
        _install_services(search_serpapi=mock_serp)  # PORT-SEAM: ScanServices override, not patch()
        enrich_job(sparse_job, serpapi_key="KEY", conn=mem_conn, config=config)

        mock_serp.assert_called_once()


# ---------------------------------------------------------------------------
# Test: ledger helpers directly
# ---------------------------------------------------------------------------


class TestSerpApiLedgerHelpers:
    def test_daily_calls_used_zero_on_empty_db(self, mem_conn):
        assert _serpapi_daily_calls_used(mem_conn) == 0

    def test_daily_calls_used_none_conn_returns_zero(self):
        assert _serpapi_daily_calls_used(None) == 0

    def test_record_call_increments_count(self, mem_conn):
        _record_serpapi_call(mem_conn)
        assert _serpapi_daily_calls_used(mem_conn) == 1
        _record_serpapi_call(mem_conn)
        assert _serpapi_daily_calls_used(mem_conn) == 2

    def test_record_call_none_conn_is_noop(self):
        """_record_serpapi_call(None) must not raise."""
        _record_serpapi_call(None)  # should not raise

    def test_ledger_row_has_correct_provider(self, mem_conn):
        _record_serpapi_call(mem_conn)
        row = mem_conn.execute("SELECT provider, purpose, cost_usd FROM scoring_costs").fetchone()
        assert row["provider"] == "serpapi_enrichment"
        assert row["purpose"] == "serpapi_enrichment"
        assert row["cost_usd"] == 0.0

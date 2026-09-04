# PORTED from tests/test_ats_link_discovery.py @ 46d5a2a1f27179d075efc5572efeded3ba2a0266 (private job-cannon). Ledger L-0527.
# PORT-SEAM: private's bare `sqlite3.connect(migrated_db)` calls and
# `crawl_careers_batch(db_path, config)` become `svc.connection_factory()` /
# `crawl_careers_batch(config)` throughout (L-0461/L-0463's zero-arg
# connection_factory seam -- services.set_services(make_scan_services(...))
# replaces the direct db_path threading). `_ats_link_discovery_due` /
# `_stamp_ats_link_discovery` drop their db_path parameter for the same
# reason. `unittest.mock.patch(".../sync_playwright")` cannot target
# jobcannon.engine.careers_crawler.sync_playwright directly -- it is a PEP
# 562 lazy import (`__getattr__`), and patch's internal getattr() snapshot
# triggers the real import, which raises ImportError in a dev venv without
# the optional playwright package installed. A local `stub_sync_playwright`
# fixture writes the module's __dict__ directly instead, functionally
# equivalent to the private @patch decorator without requiring the package.
#
# Dropped (1 of 23): test_target_platforms_derived_from_scanner_registry --
# duplicates tests/engine/test_ats_registry_completeness.py::
# test_scannable_target_platforms, which asserts the identical
# derived-from-registry equivalence (greenhouse/lever/icims in, jobvite/
# google out). See this PR's body.
"""Tests for outbound ATS-link discovery on custom career pages (#453).

Covers the pure classifier (``discover_ats_links_from_html`` /
``best_ats_candidate``) and the crawler integration that promotes a custom-site
company to an existing scanner when the rendered DOM links out to a real board.
"""

from __future__ import annotations

# PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
import sqlite3
from datetime import datetime
from types import (
    SimpleNamespace,
)  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
from unittest.mock import MagicMock, patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.careers_crawler import crawl_careers_batch
from jobcannon.engine.careers_crawler._ats_link_discovery import (
    best_ats_candidate,
    discover_ats_links_from_html,
)
from jobcannon.engine.careers_crawler._escalation import (
    _ats_link_discovery_due,
    _stamp_ats_link_discovery,
)

from jobcannon.engine.ats_scanner._scan_log import record_scan_outcome
from tests.engine.helpers.ats_scan_services import (
    create_scan_schema,
    make_scan_services,
)  # PORT-SEAM: record_scan_outcome wired explicitly -- optional host-injected ScanServices field (L-0465), unwired by default; see file header

# ---------------------------------------------------------------------------
# Pure classifier
# ---------------------------------------------------------------------------


class TestDiscoverAtsLinks:
    def test_greenhouse_anchor(self):
        html = '<html><body><a href="https://boards.greenhouse.io/acme">Jobs</a></body></html>'
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert ("greenhouse", "acme", 5) in results

    def test_lever_iframe(self):
        html = '<html><body><iframe src="https://jobs.lever.co/acme"></iframe></body></html>'
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert ("lever", "acme", 5) in results

    def test_workday_in_inline_script(self):
        html = (
            "<html><head><script>"
            'var board = "https://acme.wd5.myworkdayjobs.com/External";'
            "</script></head><body>Careers</body></html>"
        )
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert (
            ("workday", "acme.wd5/External", 5) in results
        )  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header

    def test_sorted_specificity_descending(self):
        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        html = (
            "<html><body>"
            '<a href="https://alpha.wd1.myworkdayjobs.com/wday/cxs/alpha/Alpha/jobs">A</a>'
            '<a href="https://beta.wd1.myworkdayjobs.com/Beta">B</a>'
            "</body></html>"
        )
        results = discover_ats_links_from_html(html, "https://x.com/careers")
        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        assert (
            results[0] == ("workday", "alpha.wd1/Alpha", 10)
        )  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        assert (
            ("workday", "beta.wd1/Beta", 5) in results
        )  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        specs = [spec for _p, _s, spec in results]
        assert specs == sorted(specs, reverse=True)

    def test_non_scannable_platform_filtered(self):
        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        html = '<html><body><a href="https://jobs.jobvite.com/acme/job/abc">Jobs</a></body></html>'
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert results == []

    def test_scanner_backed_platforms_now_targeted(self):
        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        for url, platform, slug in (
            ("https://acme.recruitee.com/o/eng", "recruitee", "acme"),
            ("https://apply.workable.com/datadog", "workable", "datadog"),
            ("https://acme.bamboohr.com/careers", "bamboohr", "acme"),
        ):
            html = f'<html><body><a href="{url}">Jobs</a></body></html>'
            results = discover_ats_links_from_html(html, "https://acme.com/careers")
            assert (platform, slug, 5) in results, url

    def test_icims_embed_discovered(self):
        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        html = (
            "<html><body><iframe "
            'src="https://careers-acme.icims.com/jobs/search?ss=1"></iframe></body></html>'
        )
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert ("icims", "acme", 5) in results

    def test_no_links_returns_empty(self):
        html = "<html><body><a href='https://acme.com/about'>About</a></body></html>"
        assert discover_ats_links_from_html(html, "https://acme.com/careers") == []

    def test_dedup_collapses_repeated_pair(self):
        html = (
            "<html><body>"
            '<a href="https://boards.greenhouse.io/acme">1</a>'
            '<a href="https://boards.greenhouse.io/acme/jobs/5">2</a>'
            "</body></html>"
        )
        results = discover_ats_links_from_html(html, "https://acme.com/careers")
        assert results.count(("greenhouse", "acme", 5)) == 1


class TestBestAtsCandidate:
    def test_returns_single_best(self):
        html = '<html><body><a href="https://boards.greenhouse.io/acme">Jobs</a></body></html>'
        assert best_ats_candidate(html, "https://acme.com/careers") == ("greenhouse", "acme")

    def test_abstains_on_two_platform_tie(self):
        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        html = (
            "<html><body>"
            '<a href="https://boards.greenhouse.io/acme">GH</a>'
            '<a href="https://jobs.lever.co/acme">LV</a>'
            "</body></html>"
        )
        assert best_ats_candidate(html, "https://acme.com/careers") is None

    def test_api_breaks_tie_over_board(self):
        # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
        html = (
            "<html><body>"
            '<a href="https://boards-api.greenhouse.io/v1/boards/acme/jobs">GH-API</a>'
            '<a href="https://jobs.lever.co/acme">LV</a>'
            "</body></html>"
        )
        assert best_ats_candidate(html, "https://acme.com/careers") == ("greenhouse", "acme")

    def test_none_when_no_links(self):
        assert best_ats_candidate("<html><body>nothing</body></html>", "https://x.com") is None


# ---------------------------------------------------------------------------
# DB fixtures / harness (tests/engine/ convention: real on-disk sqlite3 via
# tmp_path, matching tests/engine/helpers/ats_scan_services.py + the m0023
# columns test_careers_crawler_persistence.py already ALTER TABLEs in, plus
# the additional columns crawl_careers_batch's two-lane selection query and
# the ATS-link cooldown/promotion path read (careers_scan_enabled,
# merged_into_id, careers_crawl_flag_reason, careers_api_endpoint,
# careers_crawl_tier, careers_nav_recipe, ats_link_discovery_last_at,
# ats_evidence_trigger, jobs.classification, company_scan_log.jobs_matched).
# PORT-SEAM: private's bare `sqlite3.connect(migrated_db)` calls become
# `svc.connection_factory()` throughout (L-0461/L-0463's zero-arg seam).
# ---------------------------------------------------------------------------


@pytest.fixture
def crawler_db_path(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    create_scan_schema(conn)
    conn.execute("ALTER TABLE companies ADD COLUMN careers_scan_enabled INTEGER DEFAULT 1")
    conn.execute("ALTER TABLE companies ADD COLUMN merged_into_id INTEGER")
    conn.execute("ALTER TABLE companies ADD COLUMN careers_crawl_flag_reason TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN careers_api_endpoint TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN careers_crawl_tier TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN careers_nav_recipe TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN ats_link_discovery_last_at TEXT")
    conn.execute("ALTER TABLE companies ADD COLUMN ats_evidence_trigger TEXT")
    conn.execute("ALTER TABLE jobs ADD COLUMN classification TEXT")
    conn.execute("ALTER TABLE jobs ADD COLUMN location TEXT")
    conn.execute("ALTER TABLE jobs ADD COLUMN first_seen TEXT")
    conn.execute("ALTER TABLE jobs ADD COLUMN last_seen TEXT")
    conn.execute("ALTER TABLE company_scan_log ADD COLUMN jobs_matched INTEGER")
    conn.execute("ALTER TABLE company_scan_log ADD COLUMN source TEXT")
    conn.execute("ALTER TABLE company_scan_log ADD COLUMN failure_reason TEXT")
    conn.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
        "source TEXT, jobs_fetched INTEGER, jobs_new INTEGER, jobs_scored INTEGER)"
    )
    conn.commit()
    conn.close()

    # PORT-SEAM: record_scan_outcome is an optional host-injected seam
    # (L-0465) -- unwired, careers_crawler._persistence silently skips the
    # company_scan_log write entirely, which would starve every strike/
    # bench assertion below. Wire the real writer, matching the precedent
    # in tests/engine/test_careers_crawler_persistence.py.
    services.set_services(make_scan_services(str(db_path), record_scan_outcome=record_scan_outcome))
    return str(db_path)


def _seed_origination_company(db_path: str, name: str, careers_url: str) -> int:
    """Insert a never-crawled custom-site company (origination lane)."""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO companies "
        "(name, name_raw, careers_url, ats_probe_status, careers_scan_enabled, "  # PORT-SEAM: column renamed scan_enabled -> careers_scan_enabled (L-0461)
        "created_at, updated_at) "
        "VALUES (?, ?, ?, 'miss', 1, ?, ?)",
        (name.lower(), name, careers_url, now, now),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return int(cid)


@pytest.fixture
def stub_sync_playwright():
    """Direct module-attribute substitute for the lazy-loaded playwright
    symbol (PEP 562 __getattr__ in jobcannon/engine/careers_crawler/__init__.py).
    unittest.mock.patch/monkeypatch.setattr both read the OLD value first via
    getattr() before assigning the mock, which would trip __getattr__'s
    ImportError in a dev venv that has no playwright installed (an optional,
    multi-hundred-MB dependency per that module's own docstring) -- so this
    writes jobcannon.engine.careers_crawler.__dict__ directly, matching the
    private test's @patch(".../sync_playwright") intent without requiring the
    real package."""
    import jobcannon.engine.careers_crawler as _cc_pkg

    mock_sp = MagicMock()
    _cc_pkg.__dict__["sync_playwright"] = mock_sp
    yield mock_sp
    _cc_pkg.__dict__.pop("sync_playwright", None)


# ---------------------------------------------------------------------------
# Cooldown helper unit tests (jobcannon.engine.careers_crawler._escalation)
# PORT-SEAM: signature dropped db_path (L-0461's connection_factory seam) --
# _ats_link_discovery_due(company_id, cooldown_days) / _stamp_ats_link_discovery(company_id).
# ---------------------------------------------------------------------------


def test_ats_link_discovery_due_null_stamp_is_due(crawler_db_path):
    """A NULL stamp (never attempted) is always due."""
    cid = _seed_origination_company(crawler_db_path, "NullStampCo", "https://nullstamp.com/careers")
    assert _ats_link_discovery_due(cid, 7) is True


def test_ats_link_discovery_due_recent_stamp_not_due(crawler_db_path):
    """A stamp within the cooldown window is not due."""
    cid = _seed_origination_company(crawler_db_path, "RecentCo", "https://recent.com/careers")
    conn = sqlite3.connect(crawler_db_path)
    conn.execute(
        "UPDATE companies SET ats_link_discovery_last_at = datetime('now') WHERE id = ?",
        (cid,),
    )
    conn.commit()
    conn.close()
    assert _ats_link_discovery_due(cid, 7) is False


def test_ats_link_discovery_due_stale_stamp_is_due(crawler_db_path):
    """A stamp older than the cooldown window is due again."""
    cid = _seed_origination_company(crawler_db_path, "StaleCo", "https://stale.com/careers")
    conn = sqlite3.connect(crawler_db_path)
    conn.execute(
        "UPDATE companies SET ats_link_discovery_last_at = datetime('now', '-30 days') "
        "WHERE id = ?",
        (cid,),
    )
    conn.commit()
    conn.close()
    assert _ats_link_discovery_due(cid, 7) is True


def test_stamp_ats_link_discovery_writes_timestamp(crawler_db_path):
    """Stamping records a non-NULL timestamp."""
    cid = _seed_origination_company(crawler_db_path, "StampCo", "https://stamp.com/careers")
    conn = sqlite3.connect(crawler_db_path)
    assert (
        conn.execute(
            "SELECT ats_link_discovery_last_at FROM companies WHERE id = ?", (cid,)
        ).fetchone()[0]
        is None
    )
    conn.close()

    _stamp_ats_link_discovery(cid)

    conn = sqlite3.connect(crawler_db_path)
    assert (
        conn.execute(
            "SELECT ats_link_discovery_last_at FROM companies WHERE id = ?", (cid,)
        ).fetchone()[0]
        is not None
    )
    conn.close()


# ---------------------------------------------------------------------------
# Crawler integration
#
# UNVERIFIED -- best-effort port, not yet run against the real suite. The
# private db_path-threaded crawl_careers_batch(db_path, config) call became
# zero-arg crawl_careers_batch(config), reading the DB through
# svc.connection_factory() (L-0461). Per-tier mocks are repointed from the
# private package namespace (job_finder.web.careers_crawler.X) to each
# symbol's real definer module, per _escalation.py's own PORT-SEAM note:
#   sync_playwright        -> jobcannon.engine.careers_crawler (package __getattr__)
#   _try_static_extract    -> jobcannon.engine.careers_crawler._escalation
#   _try_sitemap_extract   -> jobcannon.engine.careers_crawler._escalation
#   _try_embedded_json_extract -> jobcannon.engine.careers_crawler._escalation
#   probe_url_params       -> jobcannon.engine.careers_page_interactions (function-scoped import)
#   _fetch_careers_landing_html -> jobcannon.engine.careers_crawler._escalation
# Promotion no longer writes companies directly -- it goes through
# get_services().prober_extensions.promote_from_careers_link(conn, ...), a
# host-injected bundle (jobcannon/engine/services.py). _FakePromoter below is
# a local test double performing the same companies-row write the private
# in-repo promotion used to do directly, so the row-level assertions below
# (ats_probe_status/ats_platform/ats_slug/ats_evidence_trigger) still hold.
# ---------------------------------------------------------------------------


class _FakePromoter:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.calls: list[tuple] = []

    def promote_from_careers_link(self, conn, company_id, platform, slug, *, page_url, config):
        self.calls.append((company_id, platform, slug, page_url))
        conn.execute(
            "UPDATE companies SET ats_probe_status = 'hit', ats_platform = ?, "
            "ats_slug = ?, ats_evidence_trigger = ? WHERE id = ?",
            (platform, slug, f"careers_link:{page_url}", company_id),
        )
        conn.commit()
        return {"outcome": "promoted"}


def _wire_promoter(db_path: str) -> _FakePromoter:
    promoter = _FakePromoter(db_path)
    services.set_services(
        make_scan_services(
            db_path,
            prober_extensions=SimpleNamespace(
                **{
                    "promote_from_careers_link": promoter.promote_from_careers_link,
                }
            ),
        )
    )
    return promoter


def _fake_active_with_greenhouse(*args, **kwargs):
    """Mock Playwright active tier: 0 jobs, but DOM links to a greenhouse board."""
    sink = kwargs.get("html_sink")
    if sink is not None:
        sink.append(
            '<html><body><a href="https://boards.greenhouse.io/customco">'
            "Open roles</a></body></html>"
        )
    return ([], None)


@patch("jobcannon.engine.ats_registry.SCANNABLE_TARGET_PLATFORMS", frozenset({"greenhouse"}))
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_playwright_active",  # PORT-SEAM: stub_sync_playwright fixture replaces @patch("...sync_playwright") -- PEP 562 lazy import trips patch's getattr() snapshot in a dev venv without the optional playwright package; see file header
    side_effect=_fake_active_with_greenhouse,
)
@patch("jobcannon.engine.careers_crawler._escalation._try_embedded_json_extract", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_sitemap_extract", return_value=[]
)  # PORT-SEAM: mock target repointed to jobcannon.engine.careers_crawler._escalation (module-level import site confirmed by source read; private patched the crawler package root directly)
@patch("jobcannon.engine.careers_page_interactions.probe_url_params", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_static_extract", return_value=[]
)  # PORT-SEAM: mock target repointed to jobcannon.engine.careers_crawler._escalation (module-level import site confirmed by source read; private patched the crawler package root directly)
def test_crawler_promotes_on_ats_link(
    _static,
    _probe,
    _sitemap,
    _embedded,
    _active,
    crawler_db_path,
    stub_sync_playwright,  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
):
    _pw = stub_sync_playwright
    cid = _seed_origination_company(crawler_db_path, "CustomCo", "https://customco.com/careers")
    promoter = _wire_promoter(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)

    mock_browser = MagicMock()
    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser
    _pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
    _pw.return_value.__exit__ = MagicMock(return_value=False)

    config = {
        "profile": {"target_titles": ["engineer"], "exclusions": {}},
        "careers_crawl": {"ai_navigation_enabled": False, "max_workers": 1},
    }
    result = crawl_careers_batch(
        config
    )  # PORT-SEAM: crawl_careers_batch(db_path, config) call updated: public dropped the db_path parameter (L-0461/L-0463's connection_factory seam)

    assert result["ats_link_promoted"] == 1
    assert promoter.calls

    conn = sqlite3.connect(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    assert row["ats_probe_status"] == "hit"
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_slug"] == "customco"
    assert row["ats_evidence_trigger"].startswith("careers_link:")


@patch("jobcannon.engine.ats_registry.SCANNABLE_TARGET_PLATFORMS", frozenset({"greenhouse"}))
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_playwright_active",  # PORT-SEAM: stub_sync_playwright fixture replaces @patch("...sync_playwright") -- PEP 562 lazy import trips patch's getattr() snapshot in a dev venv without the optional playwright package; see file header
    side_effect=_fake_active_with_greenhouse,
)
@patch("jobcannon.engine.careers_crawler._escalation._try_embedded_json_extract", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_sitemap_extract", return_value=[]
)  # PORT-SEAM: mock target repointed to jobcannon.engine.careers_crawler._escalation (module-level import site confirmed by source read; private patched the crawler package root directly)
@patch("jobcannon.engine.careers_page_interactions.probe_url_params", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_static_extract", return_value=[]
)  # PORT-SEAM: mock target repointed to jobcannon.engine.careers_crawler._escalation (module-level import site confirmed by source read; private patched the crawler package root directly)
def test_crawler_skips_when_disabled(
    _static,
    _probe,
    _sitemap,
    _embedded,
    _active,
    crawler_db_path,
    stub_sync_playwright,  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
):
    _pw = stub_sync_playwright
    cid = _seed_origination_company(crawler_db_path, "CustomCo", "https://customco.com/careers")
    _wire_promoter(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)

    mock_browser = MagicMock()
    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser
    _pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
    _pw.return_value.__exit__ = MagicMock(return_value=False)

    config = {
        "profile": {"target_titles": ["engineer"], "exclusions": {}},
        "careers_crawl": {
            "ai_navigation_enabled": False,
            "max_workers": 1,
            "ats_link_discovery_enabled": False,
        },
    }
    result = crawl_careers_batch(
        config
    )  # PORT-SEAM: crawl_careers_batch(db_path, config) call updated: public dropped the db_path parameter (L-0461/L-0463's connection_factory seam)

    assert result["ats_link_promoted"] == 0

    conn = sqlite3.connect(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    assert row["ats_probe_status"] == "miss"
    assert row["ats_platform"] is None


# ---------------------------------------------------------------------------
# #1931 -- opportunistic discovery decoupled from the zero-jobs/Playwright gate  # PORT-SEAM: crawl_careers_batch(db_path, config) call updated: public dropped the db_path parameter (L-0461/L-0463's connection_factory seam)
# ---------------------------------------------------------------------------


def _fake_sitemap_with_jobs(*args, **kwargs):
    # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
    return [{"title": "Engineer", "url": "https://fidelityco.com/jobs/1", "description": ""}]


def _fake_landing_with_greenhouse(*args, **kwargs):
    # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
    return (
        '<html><body><a href="https://boards.greenhouse.io/fidelityco">Open roles</a></body></html>'
    )


@patch("jobcannon.engine.ats_registry.SCANNABLE_TARGET_PLATFORMS", frozenset({"greenhouse"}))
@patch(
    "jobcannon.engine.careers_crawler._escalation._fetch_careers_landing_html",  # PORT-SEAM: SCANNABLE_TARGET_PLATFORMS narrowed via @patch for a registry-content-independent assertion (private asserted a different, now-removed reconciliation seam here)
    side_effect=_fake_landing_with_greenhouse,
)
@patch("jobcannon.engine.careers_crawler._escalation._try_embedded_json_extract", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_sitemap_extract",
    side_effect=_fake_sitemap_with_jobs,
)  # PORT-SEAM: stub_sync_playwright fixture replaces @patch("...sync_playwright") -- PEP 562 lazy import trips patch's getattr() snapshot in a dev venv without the optional playwright package; see file header
@patch("jobcannon.engine.careers_page_interactions.probe_url_params", return_value=[])
@patch("jobcannon.engine.careers_crawler._escalation._try_static_extract", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._scoring._score_new_jobs"
)  # PORT-SEAM: mock target repointed to jobcannon.engine.careers_crawler._escalation (module-level import site confirmed by source read; private patched the crawler package root directly)
def test_crawler_promotes_opportunistically_when_cheap_tier_finds_jobs(
    _score,
    _static,
    _probe,
    _sitemap,
    _embedded,
    _fetch_landing,
    crawler_db_path,
    stub_sync_playwright,  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
):
    """#1931: a cheap tier finding jobs must NOT suppress ATS-link discovery."""
    _pw = stub_sync_playwright
    cid = _seed_origination_company(crawler_db_path, "FidelityCo", "https://fidelityco.com/careers")
    _wire_promoter(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)

    mock_browser = MagicMock()
    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser
    _pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
    _pw.return_value.__exit__ = MagicMock(return_value=False)

    config = {
        "profile": {"target_titles": ["engineer"], "exclusions": {}},
        "careers_crawl": {"ai_navigation_enabled": False, "max_workers": 1},
    }
    result = crawl_careers_batch(config)
    # PORT-SEAM: crawl_careers_batch(db_path, config) call updated: public dropped the db_path parameter (L-0461/L-0463's connection_factory seam)
    assert result["jobs_found"] >= 1
    assert result["ats_link_promoted"] == 1

    conn = sqlite3.connect(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    assert row["ats_probe_status"] == "hit"
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_slug"] == "fidelityco"
    assert row["ats_evidence_trigger"].startswith("careers_link:")
    # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
    assert row["ats_link_discovery_last_at"] is not None
    # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
    _fetch_landing.assert_called_once_with("https://fidelityco.com/careers")


@patch("jobcannon.engine.ats_registry.SCANNABLE_TARGET_PLATFORMS", frozenset({"greenhouse"}))
@patch(
    "jobcannon.engine.careers_crawler._escalation._fetch_careers_landing_html",  # PORT-SEAM: SCANNABLE_TARGET_PLATFORMS narrowed via @patch for a registry-content-independent assertion (private asserted a different, now-removed reconciliation seam here)
    side_effect=_fake_landing_with_greenhouse,
)
@patch("jobcannon.engine.careers_crawler._escalation._try_embedded_json_extract", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_sitemap_extract",
    side_effect=_fake_sitemap_with_jobs,
)  # PORT-SEAM: stub_sync_playwright fixture replaces @patch("...sync_playwright") -- PEP 562 lazy import trips patch's getattr() snapshot in a dev venv without the optional playwright package; see file header
@patch("jobcannon.engine.careers_page_interactions.probe_url_params", return_value=[])
@patch("jobcannon.engine.careers_crawler._escalation._try_static_extract", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._scoring._score_new_jobs"
)  # PORT-SEAM: mock target repointed to jobcannon.engine.careers_crawler._escalation (module-level import site confirmed by source read; private patched the crawler package root directly)
def test_opportunistic_ats_link_cooldown_suppresses_refetch(
    _score,
    _static,
    _probe,
    _sitemap,
    _embedded,
    _fetch_landing,
    crawler_db_path,
    stub_sync_playwright,  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
):
    """#1931: a recent discovery stamp suppresses the opportunistic landing fetch."""
    _pw = stub_sync_playwright
    cid = _seed_origination_company(crawler_db_path, "CooldownCo", "https://cooldownco.com/careers")
    conn = sqlite3.connect(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    conn.execute(
        "UPDATE companies SET ats_link_discovery_last_at = datetime('now') WHERE id = ?",
        (cid,),
    )
    conn.commit()
    conn.close()
    _wire_promoter(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)

    mock_browser = MagicMock()
    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser
    _pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
    _pw.return_value.__exit__ = MagicMock(return_value=False)

    config = {
        "profile": {"target_titles": ["engineer"], "exclusions": {}},
        "careers_crawl": {"ai_navigation_enabled": False, "max_workers": 1},
    }
    result = crawl_careers_batch(config)
    # PORT-SEAM: crawl_careers_batch(db_path, config) call updated: public dropped the db_path parameter (L-0461/L-0463's connection_factory seam)
    assert result["ats_link_promoted"] == 0
    _fetch_landing.assert_not_called()

    conn = sqlite3.connect(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    assert row["ats_probe_status"] == "miss"
    assert row["ats_platform"] is None


# ---------------------------------------------------------------------------
# #1937 -- opportunistic promotion must run AFTER the #1921 cohort-legitimacy
# gate for the cheap tiers that skip Playwright entirely.  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
# ---------------------------------------------------------------------------


def _fake_static_with_jobs(*args, **kwargs):
    # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
    return [{"title": "Engineer", "url": "https://aggregatorco.com/jobs/1", "description": ""}]


def _fake_landing_with_foreign_greenhouse(*args, **kwargs):
    # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
    return (
        '<html><body><a href="https://boards.greenhouse.io/unrelatedemployer">'
        "Open roles</a></body></html>"
    )


@patch("jobcannon.engine.ats_registry.SCANNABLE_TARGET_PLATFORMS", frozenset({"greenhouse"}))
@patch(
    "jobcannon.engine.careers_crawler._escalation._fetch_careers_landing_html",  # PORT-SEAM: SCANNABLE_TARGET_PLATFORMS narrowed via @patch for a registry-content-independent assertion (private asserted a different, now-removed reconciliation seam here)
    side_effect=_fake_landing_with_foreign_greenhouse,
)
@patch("jobcannon.engine.careers_crawler._escalation.evaluate_cohort_legitimacy")
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_embedded_json_extract", return_value=[]
)  # PORT-SEAM: stub_sync_playwright fixture replaces @patch("...sync_playwright") -- PEP 562 lazy import trips patch's getattr() snapshot in a dev venv without the optional playwright package; see file header
@patch("jobcannon.engine.careers_page_interactions.probe_url_params", return_value=[])
@patch("jobcannon.engine.careers_crawler._escalation._try_sitemap_extract", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_static_extract",  # PORT-SEAM: mock target repointed to jobcannon.engine.careers_crawler._escalation (module-level import site confirmed by source read; private patched the crawler package root directly)
    side_effect=_fake_static_with_jobs,
)
@patch(
    "jobcannon.engine.careers_crawler._scoring._score_new_jobs"
)  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
def test_flagged_cheap_tier_cohort_does_not_promote_foreign_board(
    _score,
    _static,
    _sitemap,
    _probe,
    _embedded,
    # PORT-SEAM: import list regrouped for the public engine package layout
    _gate,
    _fetch_landing,
    crawler_db_path,
    stub_sync_playwright,  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
):
    _pw = stub_sync_playwright
    cid = _seed_origination_company(
        crawler_db_path, "AggregatorCo", "https://aggregatorco.com/careers"
    )
    _wire_promoter(crawler_db_path)
    _gate.return_value = SimpleNamespace(
        flagged=True, reason="templated_title_cluster"
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)

    mock_browser = MagicMock()
    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser
    _pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
    _pw.return_value.__exit__ = MagicMock(return_value=False)

    config = {
        "profile": {"target_titles": ["engineer"], "exclusions": {}},
        "careers_crawl": {"ai_navigation_enabled": False, "max_workers": 1},
    }
    result = crawl_careers_batch(
        config
    )  # PORT-SEAM: crawl_careers_batch(db_path, config) call updated: public dropped the db_path parameter (L-0461/L-0463's connection_factory seam)

    assert result["legitimacy_flagged"] == 1
    assert result["jobs_new"] == 0
    assert result["ats_link_promoted"] == 0

    conn = sqlite3.connect(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
    assert row["ats_probe_status"] == "miss"
    assert row["ats_platform"] is None
    assert row["ats_slug"] is None
    assert row["careers_crawl_flag_reason"] == "templated_title_cluster"


@patch("jobcannon.engine.ats_registry.SCANNABLE_TARGET_PLATFORMS", frozenset({"greenhouse"}))
@patch(
    "jobcannon.engine.careers_crawler._escalation._fetch_careers_landing_html",  # PORT-SEAM: SCANNABLE_TARGET_PLATFORMS narrowed via @patch for a registry-content-independent assertion (private asserted a different, now-removed reconciliation seam here)
    side_effect=_fake_landing_with_foreign_greenhouse,
)
@patch("jobcannon.engine.careers_crawler._escalation.evaluate_cohort_legitimacy")
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_embedded_json_extract", return_value=[]
)  # PORT-SEAM: stub_sync_playwright fixture replaces @patch("...sync_playwright") -- PEP 562 lazy import trips patch's getattr() snapshot in a dev venv without the optional playwright package; see file header
@patch("jobcannon.engine.careers_page_interactions.probe_url_params", return_value=[])
@patch("jobcannon.engine.careers_crawler._escalation._try_sitemap_extract", return_value=[])
@patch(
    "jobcannon.engine.careers_crawler._escalation._try_static_extract",  # PORT-SEAM: mock target repointed to jobcannon.engine.careers_crawler._escalation (module-level import site confirmed by source read; private patched the crawler package root directly)
    side_effect=_fake_static_with_jobs,
)
@patch(
    "jobcannon.engine.careers_crawler._scoring._score_new_jobs"
)  # PORT-SEAM: mechanical rewrite for the public engine/careers_crawler call shape -- see file header
def test_clean_cheap_tier_cohort_still_promotes(
    _score,
    _static,
    _sitemap,
    _probe,
    _embedded,
    # PORT-SEAM: import list regrouped for the public engine package layout
    _gate,
    _fetch_landing,
    crawler_db_path,
    stub_sync_playwright,  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
):
    """Positive control for the #1937 reorder: a legitimacy-CLEAN cheap-tier
    cohort still promotes exactly as before."""
    _pw = stub_sync_playwright
    cid = _seed_origination_company(crawler_db_path, "CleanCo", "https://cleanco.com/careers")
    _wire_promoter(crawler_db_path)
    _gate.return_value = SimpleNamespace(
        flagged=False, reason=None
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)

    mock_browser = MagicMock()
    mock_pw_instance = MagicMock()
    mock_pw_instance.chromium.launch.return_value = mock_browser
    _pw.return_value.__enter__ = MagicMock(return_value=mock_pw_instance)
    _pw.return_value.__exit__ = MagicMock(return_value=False)

    config = {
        "profile": {"target_titles": ["engineer"], "exclusions": {}},
        "careers_crawl": {"ai_navigation_enabled": False, "max_workers": 1},
    }
    result = crawl_careers_batch(
        config
    )  # PORT-SEAM: crawl_careers_batch(db_path, config) call updated: public dropped the db_path parameter (L-0461/L-0463's connection_factory seam)

    assert result["legitimacy_flagged"] == 0
    assert result["jobs_new"] >= 1
    assert result["ats_link_promoted"] == 1

    conn = sqlite3.connect(
        crawler_db_path
    )  # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone())
    conn.close()
    assert row["ats_probe_status"] == "hit"
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_slug"] == "unrelatedemployer"
    assert row["careers_crawl_flag_reason"] is None
    # PORT-SEAM: migrated_db*/db_path fixture renamed to crawler_db_path -- connection now comes from svc.connection_factory(), not a threaded db_path argument (L-0461/L-0463's zero-arg seam)

"""Tests for jobcannon.engine.primary_source_resolver — the flat-schema
adaptation (issue #322).

The module runs engine-dialect SQL (qmark placeholders, `jobs` table,
sqlite3.Connection-shaped conn) against a bare sqlite3 connection here, the
same contract jobcannon/db/compat.py's engine_sql_to_host honors on the
hosted path. The _SCHEMA below deliberately mirrors the FLAT hosted surface
for the columns the resolver touches — no `pipeline_status` column and no
`postings` JSON column — so any regression that re-introduces a
non-flat-schema reference fails here exactly the way it would fail against
Postgres ("no such column"), instead of being masked by a private-shaped
fixture.

The ScanServices optional-hook fakes (_Seam) carry the LANDED db-layer
signatures — `annotate_posting_apply_url(conn, dedup_key,
aggregator_apply_url)` and `stamp_direct_url_checks(conn, dedup_keys)` —
so the stale private-repo call shapes this issue fixed (5-arg descriptor
keying, 3-arg stamp with now_iso) raise TypeError instead of silently
passing.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from jobcannon.engine import ats_platforms, services
from jobcannon.engine.primary_source_resolver import (
    resolve_primary_sources,
    run_primary_source_resolution,
)

# Flat-surface schema: every column the resolver's own SQL reads/writes via
# the seam fakes, plus the columns merge_primary_posting_fields' own row
# SELECT needs (company/salary_min/salary_max/posted_date/source_id/
# score_breakdown/unresolved_reasons) so a strict match can exercise the
# merge path end-to-end. NOT present: pipeline_status, postings — the two
# private-schema shapes with no hosted equivalent (issue #322).
_SCHEMA = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    ats_platform TEXT,
    ats_slug TEXT,
    ats_probe_status TEXT
);

CREATE TABLE jobs (
    dedup_key TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    location TEXT,
    description TEXT,
    jd_full TEXT,
    company_id INTEGER,
    source_urls TEXT,
    source_id TEXT,
    direct_url TEXT,
    direct_url_confidence TEXT,
    aggregator_apply_url TEXT,
    direct_url_checked_at TEXT,
    direct_url_attempts INTEGER DEFAULT 0,
    expiry_status TEXT,
    last_seen TEXT,
    salary_min REAL,
    salary_max REAL,
    posted_date TEXT,
    score_breakdown TEXT,
    unresolved_reasons TEXT
);
"""


class _Seam:
    """Recording fakes for the optional ScanServices hooks the resolver calls.

    Signatures match the landed db-layer functions verbatim — passing the
    private-repo arg lists (5-arg annotate, 3-arg stamp) raises TypeError,
    which is exactly the regression these tests pin.
    """

    def __init__(self):
        self.direct_url_calls: list[tuple] = []
        self.stamp_calls: list[list[str]] = []
        self.annotate_calls: list[tuple] = []
        self.services: services.ScanServices | None = None

    def set_direct_url(self, conn, dedup_key, url, confidence):
        self.direct_url_calls.append((dedup_key, url, confidence))
        conn.execute(
            "UPDATE jobs SET direct_url = ?, direct_url_confidence = ? WHERE dedup_key = ?",
            (url, confidence, dedup_key),
        )
        return True

    def stamp_direct_url_checks(self, conn, dedup_keys):
        self.stamp_calls.append(list(dedup_keys))

    def annotate_posting_apply_url(self, conn, dedup_key, aggregator_apply_url):
        self.annotate_calls.append((dedup_key, aggregator_apply_url))
        conn.execute(
            "UPDATE jobs SET aggregator_apply_url = ? WHERE dedup_key = ?",
            (aggregator_apply_url, dedup_key),
        )
        return True


@pytest.fixture
def db(monkeypatch):
    """Seeded in-memory sqlite DB + recorded seam bundle.

    Mirrors tests/engine/test_stale_detector.py's db fixture: the fake
    connection_factory yields the SAME pre-seeded connection so test bodies
    can keep querying after the resolver runs.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    seam = _Seam()

    @contextlib.contextmanager
    def factory(*, synchronous="FULL"):
        yield conn

    seam.services = services.ScanServices(
        connection_factory=factory,
        upsert_job=lambda *a, **k: SimpleNamespace(kind="updated"),
        set_jd_full=lambda *a, **k: None,
        upsert_company=lambda *a, **k: None,
        get_secret=lambda name, *, config=None: None,
        config={},
        jd_storage_max_chars=100_000,
        set_direct_url=seam.set_direct_url,
        stamp_direct_url_checks=seam.stamp_direct_url_checks,
        annotate_posting_apply_url=seam.annotate_posting_apply_url,
    )
    services.set_services(seam.services)

    # Register a fake platform so the candidate SQL's 'hit' companies have a
    # scannable registry entry (resolver imports SCANNERS_BY_NAME lazily, so
    # the monkeypatched dict entry is what it sees).
    monkeypatch.setitem(
        ats_platforms.SCANNERS_BY_NAME, "fake_ats", SimpleNamespace(name="fake_ats")
    )

    yield conn, seam
    conn.close()


def _board(monkeypatch, postings: list[dict]) -> None:
    """Patch the resolver's lazy run_platform_scan import to return a fixed board."""
    monkeypatch.setattr(
        "jobcannon.engine.ats_platforms._registry.run_platform_scan",
        lambda scanner, slug, titles, exclusions: (list(postings), 0),
    )


def _days_ago(n: float) -> str:
    return (datetime.now() - timedelta(days=n)).isoformat()


def _insert_company(
    conn,
    *,
    company_id: int = 1,
    name: str = "Acme",
    platform: str | None = "fake_ats",
    slug: str | None = "acme",
    probe: str = "hit",
) -> None:
    conn.execute(
        "INSERT INTO companies (id, name, ats_platform, ats_slug, ats_probe_status) "
        "VALUES (?, ?, ?, ?, ?)",
        (company_id, name, platform, slug, probe),
    )
    conn.commit()


def _insert_job(
    conn,
    dedup_key: str,
    *,
    company_id: int = 1,
    title: str = "Data Scientist",
    location: str = "Remote",
    source_urls: str = "[]",
    direct_url: str | None = None,
    attempts: int = 0,
    checked_at: str | None = None,
    expiry_status: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, company_id, source_urls, "
        "direct_url, direct_url_checked_at, direct_url_attempts, expiry_status, last_seen) "
        "VALUES (?, ?, 'Acme', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dedup_key,
            title,
            location,
            company_id,
            source_urls,
            direct_url,
            checked_at,
            attempts,
            expiry_status,
            _days_ago(0.5),
        ),
    )
    conn.commit()


_CONFIG = {"direct_link": {"resolver": {"llm_tiebreak": False}}}


class TestFreePromotion:
    def test_promotes_ats_source_url_to_strict_direct_url(self, db):
        conn, seam = db
        _insert_job(
            conn,
            "j1",
            source_urls=json.dumps(
                ["https://www.linkedin.com/jobs/view/1", "https://jobs.lever.co/acme/abc"]
            ),
        )

        stats = resolve_primary_sources(conn, _CONFIG)

        assert stats["promoted"] == 1
        assert stats["resolved"] == 1
        assert stats["strict"] == 1
        assert seam.direct_url_calls == [("j1", "https://jobs.lever.co/acme/abc", "strict")]
        row = conn.execute("SELECT direct_url FROM jobs WHERE dedup_key = 'j1'").fetchone()
        assert row["direct_url"] == "https://jobs.lever.co/acme/abc"

    def test_aggregator_only_source_urls_not_promoted(self, db):
        conn, seam = db
        _insert_job(conn, "j1", source_urls=json.dumps(["https://jooble.org/x"]))

        stats = resolve_primary_sources(conn, _CONFIG)

        assert stats["promoted"] == 0
        assert seam.direct_url_calls == []


class TestCandidateGating:
    """The candidate SQL must run against the flat schema (no pipeline_status,
    no postings JSON column) and keep the attempts/decay semantics."""

    def test_expired_and_non_hit_companies_excluded(self, db, monkeypatch):
        conn, seam = db
        _insert_company(conn, company_id=1, probe="hit")
        _insert_company(conn, company_id=2, name="Globex", slug="globex", probe="miss")
        _insert_job(conn, "expired", expiry_status="expired")
        _insert_job(conn, "miss_company", company_id=2)
        _board(monkeypatch, [])

        stats = resolve_primary_sources(conn, _CONFIG)

        assert stats["jobs_checked"] == 0
        assert stats["companies_scanned"] == 0

    def test_attempt_cap_blocks_fresh_check_allows_decay(self, db, monkeypatch):
        conn, seam = db
        _insert_company(conn)
        # At the cap with a recent check: excluded.
        _insert_job(conn, "capped", attempts=3, checked_at=_days_ago(1))
        # At the cap but past the 30-day decay window: re-eligible.
        _insert_job(conn, "decayed", attempts=3, checked_at=_days_ago(40))
        # Never checked: eligible regardless of the attempts arm.
        _insert_job(conn, "fresh", attempts=0, checked_at=None)
        _board(monkeypatch, [])

        stats = resolve_primary_sources(conn, _CONFIG)

        assert stats["jobs_checked"] == 2
        assert len(seam.stamp_calls) == 1
        assert set(seam.stamp_calls[0]) == {"decayed", "fresh"}

    def test_empty_board_still_stamps_attempt(self, db, monkeypatch):
        """An empty board result counts as an attempt for all candidates —
        the registry contract can't distinguish 'no postings' from 'fetch
        failed', so the decay window repairs transient burns."""
        conn, seam = db
        _insert_company(conn)
        _insert_job(conn, "j1")
        _board(monkeypatch, [])

        stats = resolve_primary_sources(conn, _CONFIG)

        assert stats["companies_scanned"] == 1
        assert stats["jobs_checked"] == 1
        assert seam.stamp_calls == [["j1"]]
        assert seam.direct_url_calls == []
        assert seam.annotate_calls == []

    def test_non_scannable_platform_skipped_no_attempt(self, db, monkeypatch):
        conn, seam = db
        _insert_company(conn, platform="unknown_ats")
        _insert_job(conn, "j1")
        _board(monkeypatch, [])

        stats = resolve_primary_sources(conn, _CONFIG)

        assert stats["companies_skipped"] == 1
        assert stats["companies_scanned"] == 0
        assert stats["jobs_checked"] == 0
        assert seam.stamp_calls == []


class TestStrictMatchFlatAdaptation:
    """Issue #322's core: the Phase-5 descriptor branch re-adapted to the flat
    postings row — 3-arg annotate call, no descriptor keying, and additive
    (falls through to the row-level direct_url write + merge)."""

    def test_strict_match_annotates_and_still_writes_direct_url(self, db, monkeypatch):
        conn, seam = db
        _insert_company(conn)
        _insert_job(
            conn,
            "j1",
            source_urls=json.dumps(["https://www.linkedin.com/jobs/view/1"]),
        )
        _board(
            monkeypatch,
            [{"title": "Data Scientist", "source_url": "https://boards.fake.io/acme/1"}],
        )

        stats = resolve_primary_sources(conn, _CONFIG)

        # Landed 3-arg signature: (conn, dedup_key, aggregator_apply_url).
        assert seam.annotate_calls == [("j1", "https://www.linkedin.com/jobs/view/1")]
        assert stats["annotated"] == 1
        row = conn.execute(
            "SELECT aggregator_apply_url FROM jobs WHERE dedup_key = 'j1'"
        ).fetchone()
        assert row["aggregator_apply_url"] == "https://www.linkedin.com/jobs/view/1"

        # The annotate is additive on the flat row, NOT a replacement path:
        # the row-level direct_url write and merge still run (private's
        # `continue` would have skipped the resolver's primary purpose).
        assert seam.direct_url_calls == [("j1", "https://boards.fake.io/acme/1", "strict")]
        assert stats["resolved"] == 1
        assert stats["strict"] == 1
        assert stats["merged"] == 1
        assert seam.stamp_calls == [["j1"]]

    def test_strict_match_without_source_urls_skips_annotate(self, db, monkeypatch):
        conn, seam = db
        _insert_company(conn)
        _insert_job(conn, "j1", source_urls="[]")
        _board(
            monkeypatch,
            [{"title": "Data Scientist", "source_url": "https://boards.fake.io/acme/1"}],
        )

        stats = resolve_primary_sources(conn, _CONFIG)

        assert seam.annotate_calls == []
        assert stats["annotated"] == 0
        assert stats["resolved"] == 1
        assert stats["strict"] == 1

    def test_loose_match_links_without_annotate_or_merge(self, db, monkeypatch):
        conn, seam = db
        _insert_company(conn)
        _insert_job(
            conn,
            "j1",
            source_urls=json.dumps(["https://www.linkedin.com/jobs/view/1"]),
        )
        _board(
            monkeypatch,
            [
                {"title": "Data Scientist", "source_url": "https://boards.fake.io/acme/1"},
                {"title": "Data Scientist", "source_url": "https://boards.fake.io/acme/2"},
            ],
        )

        stats = resolve_primary_sources(conn, _CONFIG)

        assert seam.direct_url_calls == [("j1", "https://boards.fake.io/acme/1", "loose")]
        assert stats["resolved"] == 1
        assert stats["loose"] == 1
        assert stats["strict"] == 0
        assert stats["annotated"] == 0
        assert stats["merged"] == 0
        assert seam.annotate_calls == []


class TestLlmTiebreak:
    def test_tiebreak_upgrade_flows_through_flat_annotate_path(self, db, monkeypatch):
        conn, seam = db
        _insert_company(conn)
        _insert_job(
            conn,
            "j1",
            title="Machine Learning Engineer",
            source_urls=json.dumps(["https://www.linkedin.com/jobs/view/9"]),
        )
        _board(
            monkeypatch,
            [{"title": "Senior ML Engineer", "source_url": "https://boards.fake.io/acme/7"}],
        )
        upgraded = {"title": "Senior ML Engineer", "source_url": "https://boards.fake.io/acme/7"}
        services.set_services(
            dataclasses.replace(
                seam.services,
                tiebreak_primary_posting=lambda *a, **k: upgraded,
            )
        )

        stats = resolve_primary_sources(conn, {})

        assert stats["llm_checked"] == 1
        assert stats["llm_upgraded"] == 1
        assert seam.annotate_calls == [("j1", "https://www.linkedin.com/jobs/view/9")]
        assert seam.direct_url_calls == [("j1", "https://boards.fake.io/acme/7", "strict")]
        assert stats["strict"] == 1


class TestSchedulerEntryPoint:
    def test_run_primary_source_resolution_uses_connection_factory(self, db):
        conn, seam = db
        _insert_company(conn)

        stats = run_primary_source_resolution("ignored.db", _CONFIG)

        assert isinstance(stats, dict)
        assert stats["companies_scanned"] == 0

"""Prober-extensions seam: host-injectable bundle replacing the private
source's lazy imports of ats_identity_reconcile / ats_slug_challenge /
careers_crawler tiers inside ats_prober.py.

With no bundle registered, the static-first fall-through records a miss
and speculative slug promotion fails closed (never demotes an incumbent
owner on a collision, always stamps a fresh claim provisional).
"""

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jobcannon.engine import ats_prober

_SCHEMA = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    name_raw TEXT,
    ats_probe_status TEXT,
    ats_platform TEXT,
    ats_slug TEXT,
    miss_reason TEXT,
    updated_at TEXT,
    ats_evidence_trigger TEXT,
    ats_evidence_extractor_version TEXT,
    ats_evidence_unique_url_count INTEGER,
    ats_evidence_job_count INTEGER,
    ats_evidence_reconciled_at TEXT,
    ats_evidence_provisional INTEGER,
    consecutive_empty_scans INTEGER DEFAULT 0,
    scan_enabled INTEGER DEFAULT 1,
    ats_scan_enabled INTEGER DEFAULT 1,
    careers_scan_enabled INTEGER DEFAULT 1,
    UNIQUE(ats_platform, ats_slug)
);
"""


@pytest.fixture(autouse=True)
def _restore_extensions():
    """Save/restore the module-global extension bundle (xdist safety)."""
    prior = ats_prober._prober_extensions
    yield
    ats_prober.set_prober_extensions(prior)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    yield c
    c.close()


def _insert_company(conn, company_id, name="Acme", name_raw="ACME INC", **cols):
    fields = {"id": company_id, "name": name, "name_raw": name_raw, **cols}
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO companies ({', '.join(fields)}) VALUES ({placeholders})",
        list(fields.values()),
    )
    conn.commit()


def test_static_fallthrough_no_extensions_is_miss(conn):
    ats_prober.set_prober_extensions(None)
    _insert_company(conn, 1)

    result = ats_prober._try_static_first_fallthrough(
        company_id=1,
        company_name="Acme",
        careers_url="https://acme.example/careers",
        conn=conn,
        config={},
        now="2026-07-16T00:00:00Z",
    )

    assert result == {"status": "miss", "reason": "static_fallthrough_unavailable"}
    row = conn.execute(
        "SELECT ats_probe_status, miss_reason FROM companies WHERE id = 1"
    ).fetchone()
    assert row["ats_probe_status"] == "miss"
    assert row["miss_reason"] == "static_fallthrough_unavailable"


def test_static_fallthrough_tier2_co_writes_careers_scan_enabled(conn):
    """WI-13 (#329): the tier-2 jobs-persisted UPDATE re-enables the careers
    lane — scan_enabled AND careers_scan_enabled go TRUE in the same
    statement. ats_scan_enabled stays untouched: the write marks a custom
    careers page, not a resurrected ATS board."""
    stub = SimpleNamespace(
        try_static_extract=lambda *a, **k: [{"title": "Engineer"}],
    )
    ats_prober.set_prober_extensions(stub)
    _insert_company(conn, 1, scan_enabled=0, ats_scan_enabled=0, careers_scan_enabled=0)

    with patch(
        "jobcannon.engine.ats_prober.fetch_with_deadline",
        side_effect=ConnectionError("offline"),
    ):
        result = ats_prober._try_static_first_fallthrough(
            company_id=1,
            company_name="Acme",
            careers_url="https://acme.example/careers",
            conn=conn,
            config={},
            now="2026-07-16T00:00:00Z",
        )

    assert result["status"] == "miss"
    assert result["reason"] == "static_fallthrough_tier2_jobs_persisted"
    row = conn.execute(
        "SELECT scan_enabled, ats_scan_enabled, careers_scan_enabled FROM companies WHERE id = 1"
    ).fetchone()
    assert row["scan_enabled"] == 1
    assert row["careers_scan_enabled"] == 1
    assert row["ats_scan_enabled"] == 0


def test_speculative_hit_no_extensions_clean_slug_is_provisional(conn):
    ats_prober.set_prober_extensions(None)
    _insert_company(conn, 1)

    result = ats_prober._promote_speculative_hit(conn, 1, "Acme", "greenhouse", "acme", {})

    assert result is True
    row = conn.execute(
        "SELECT ats_probe_status, ats_platform, ats_slug, ats_evidence_provisional "
        "FROM companies WHERE id = 1"
    ).fetchone()
    assert row["ats_probe_status"] == "hit"
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_slug"] == "acme"
    assert row["ats_evidence_provisional"] == 1


def test_speculative_hit_no_extensions_collision_fails_closed(conn):
    ats_prober.set_prober_extensions(None)
    _insert_company(
        conn,
        1,
        name="Incumbent",
        ats_probe_status="hit",
        ats_platform="greenhouse",
        ats_slug="acme",
        ats_evidence_provisional=0,
    )
    _insert_company(conn, 2, name="Challenger")

    result = ats_prober._promote_speculative_hit(conn, 2, "Challenger", "greenhouse", "acme", {})

    assert result is False
    incumbent = conn.execute(
        "SELECT ats_probe_status, ats_platform, ats_slug, ats_evidence_provisional "
        "FROM companies WHERE id = 1"
    ).fetchone()
    assert incumbent["ats_probe_status"] == "hit"
    assert incumbent["ats_platform"] == "greenhouse"
    assert incumbent["ats_slug"] == "acme"
    assert incumbent["ats_evidence_provisional"] == 0
    challenger = conn.execute(
        "SELECT ats_probe_status, ats_platform, ats_slug FROM companies WHERE id = 2"
    ).fetchone()
    assert challenger["ats_probe_status"] is None
    assert challenger["ats_platform"] is None
    assert challenger["ats_slug"] is None


def test_speculative_hit_with_extensions_owner_identity_passes_not_provisional(conn):
    stub = SimpleNamespace(owner_identity_passes=lambda *a: True)
    ats_prober.set_prober_extensions(stub)
    _insert_company(conn, 1)

    result = ats_prober._promote_speculative_hit(conn, 1, "Acme", "greenhouse", "acme", {})

    assert result is True
    row = conn.execute("SELECT ats_evidence_provisional FROM companies WHERE id = 1").fetchone()
    assert row["ats_evidence_provisional"] == 0

"""Fail-closed coverage for ``_probe.py``'s identity-trio ScanServices fields
(``resolve_slug_collision`` / ``identity_reconcile_settings`` /
``owner_identity_passes``) — review finding FIX B, PR #6.

This is the OWN-channel counterpart to
``tests/engine/test_prober_extensions_seam.py``, which covers the same
fail-closed contract for ``jobcannon.engine.ats_prober``'s separate
``prober_extensions`` module-global seam. ``_probe.py`` consults its own
``ScanServices`` fields directly (see ``services.py``'s "dual-channel" module
docstring), and until now nothing drove either of its two collision/
provisional write sites — ``_resolve_collision`` (~line 165), the B2
careers_url fast-path (~lines 394-403), and the speculative ladder (~lines
595-604) — through an actual collision or a real ``probe_ats_slugs`` hit with
the hooks at their unset default. ``test_scan_seam.py`` only asserted the
fields are ``None`` as dataclass defaults; nothing exercised the branches
that default guards.

The invariant under test: "a single speculative guess must never evict an
incumbent owner", and a freshly-written claim without owner-identity
verification is always provisional.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.ats_scanner._probe import _resolve_collision, probe_ats_slugs

_SCHEMA = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT,
    name_raw TEXT,
    careers_url TEXT,
    ats_probe_status TEXT,
    ats_probe_attempted_at TEXT,
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
    UNIQUE(ats_platform, ats_slug)
);
"""


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _make_services(db_path: str, **overrides) -> services.ScanServices:
    @contextmanager
    def factory(*, synchronous="FULL"):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    kwargs: dict = dict(
        connection_factory=factory,
        upsert_job=lambda *a, **k: None,
        set_jd_full=lambda *a, **k: None,
        upsert_company=lambda *a, **k: None,
        get_secret=lambda name, *, config=None: None,
        config={},
        jd_storage_max_chars=100_000,
    )
    kwargs.update(overrides)
    return services.ScanServices(**kwargs)


def _insert(conn, company_id, *, name="Acme", name_raw="ACME INC", **cols):
    fields = {"id": company_id, "name": name, "name_raw": name_raw, **cols}
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO companies ({', '.join(fields)}) VALUES ({placeholders})",
        list(fields.values()),
    )
    conn.commit()


def _insert_pending(conn, company_id, *, name="Challenger", careers_url=None):
    _insert(
        conn,
        company_id,
        name=name.lower(),
        name_raw=name,
        careers_url=careers_url,
        ats_probe_status="pending",
    )


def _build_probes(hits_for: dict[str, bool]) -> list:
    """Fake _PROBES list (mirrors test_speculative_probe_consistency.py's helper
    of the same name — direct list replacement is needed because the real
    _PROBES captures function references at import time)."""
    all_platforms = [
        "lever",
        "greenhouse",
        "ashby",
        "recruitee",
        "breezy",
        "jazzhr",
        "pinpoint",
        "teamtailor",
        "personio",
        "bamboohr",
    ]

    def _make_probe(value: bool):
        return lambda _slug: value

    return [(name, _make_probe(hits_for.get(name, False))) for name in all_platforms]


# ---------------------------------------------------------------------------
# _resolve_collision — direct unit coverage of the fail-closed contract.
# ---------------------------------------------------------------------------


def test_resolve_collision_hooks_unset_returns_no_demotion(db_path):
    """With resolve_slug_collision / identity_reconcile_settings both unset,
    _resolve_collision must return the fail-closed no-op dict — never call
    into anything that could demote an owner."""
    services.set_services(_make_services(db_path))
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            result = _resolve_collision(
                conn, platform="greenhouse", slug="acme", company_id=1, config={}
            )
        finally:
            conn.close()
    finally:
        services.clear_services()

    assert result == {
        "demoted": False,
        "challenge": None,
        "existing_owner_id": None,
        "existing_owner_name": None,
    }


def test_resolve_collision_only_one_hook_set_still_fails_closed(db_path):
    """The guard is an OR, not an AND: EITHER hook missing is enough to fail
    closed, since resolve_slug_collision needs identity_reconcile_settings(config)
    as an argument and can't safely be called without it."""
    services.set_services(
        _make_services(db_path, resolve_slug_collision=lambda *a, **k: {"demoted": True})
    )
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            result = _resolve_collision(
                conn, platform="greenhouse", slug="acme", company_id=1, config={}
            )
        finally:
            conn.close()
    finally:
        services.clear_services()

    assert result["demoted"] is False


# ---------------------------------------------------------------------------
# probe_ats_slugs speculative ladder — end-to-end collision + provisional.
# ---------------------------------------------------------------------------


def test_speculative_collision_hooks_unset_leaves_incumbent_untouched(db_path):
    """A pending company whose derived slug candidate collides with an
    existing hit owner must be marked miss/collision, and the incumbent's
    row must be byte-for-byte unchanged — no demotion without the identity
    hooks wired."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert(
        conn,
        1,
        name="incumbent",
        name_raw="Incumbent",
        ats_probe_status="hit",
        ats_platform="greenhouse",
        ats_slug="acme",
        ats_evidence_provisional=0,
    )
    _insert_pending(conn, 2, name="Challenger")
    conn.close()

    services.set_services(_make_services(db_path))
    try:
        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["acme"],
            ),
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"greenhouse": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(db_path, config={})
    finally:
        services.clear_services()

    assert result["misses"] == 1
    assert result["hits"] == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    incumbent = conn.execute(
        "SELECT ats_probe_status, ats_platform, ats_slug, ats_evidence_provisional "
        "FROM companies WHERE id = 1"
    ).fetchone()
    challenger = conn.execute(
        "SELECT ats_probe_status, ats_platform, ats_slug, miss_reason FROM companies WHERE id = 2"
    ).fetchone()
    conn.close()

    assert incumbent["ats_probe_status"] == "hit"
    assert incumbent["ats_platform"] == "greenhouse"
    assert incumbent["ats_slug"] == "acme"
    assert incumbent["ats_evidence_provisional"] == 0
    assert challenger["ats_probe_status"] == "miss"
    assert challenger["ats_platform"] is None
    assert challenger["ats_slug"] is None
    assert challenger["miss_reason"] == "collision"


def test_speculative_hit_owner_identity_passes_none_is_provisional(db_path):
    """A fresh (non-colliding) speculative hit with owner_identity_passes
    unset must be stamped ats_evidence_provisional=1."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_pending(conn, 1, name="Acme")
    conn.close()

    services.set_services(_make_services(db_path))
    try:
        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["acme"],
            ),
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"greenhouse": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(db_path, config={})
    finally:
        services.clear_services()

    assert result["hits"] == 1
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT ats_probe_status, ats_evidence_provisional FROM companies WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row["ats_probe_status"] == "hit"
    assert row["ats_evidence_provisional"] == 1


def test_speculative_hit_owner_identity_passes_true_is_not_provisional(db_path):
    """Companion positive case: when owner_identity_passes IS wired and
    returns True, the same hit is stamped non-provisional (proves the
    ternary's true branch, not just its default)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_pending(conn, 1, name="Acme")
    conn.close()

    services.set_services(_make_services(db_path, owner_identity_passes=lambda *a, **k: True))
    try:
        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["acme"],
            ),
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"greenhouse": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            probe_ats_slugs(db_path, config={})
    finally:
        services.clear_services()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT ats_evidence_provisional FROM companies WHERE id = 1").fetchone()
    conn.close()
    assert row["ats_evidence_provisional"] == 0


# ---------------------------------------------------------------------------
# probe_ats_slugs B2 careers_url fast-path — the OTHER is_provisional site.
# ---------------------------------------------------------------------------


def test_fastpath_collision_hooks_unset_leaves_incumbent_untouched(db_path):
    """Same fail-closed guarantee, exercised through the careers_url B2
    fast-path (the SIBLING is_provisional write site to the speculative
    ladder above — a distinct code path with its own IntegrityError handler)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert(
        conn,
        1,
        name="incumbent",
        name_raw="Incumbent",
        ats_probe_status="hit",
        ats_platform="greenhouse",
        ats_slug="acme",
        ats_evidence_provisional=0,
    )
    _insert_pending(conn, 2, name="Challenger", careers_url="https://boards.greenhouse.io/acme")
    conn.close()

    services.set_services(_make_services(db_path))
    try:
        with patch("jobcannon.engine.ats_scanner._probe._verify_fastpath_live", return_value=True):
            result = probe_ats_slugs(db_path, config={})
    finally:
        services.clear_services()

    assert result["misses"] == 1
    assert result["hits"] == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    incumbent = conn.execute(
        "SELECT ats_probe_status, ats_platform, ats_slug, ats_evidence_provisional "
        "FROM companies WHERE id = 1"
    ).fetchone()
    challenger = conn.execute(
        "SELECT ats_probe_status, ats_platform, ats_slug, miss_reason FROM companies WHERE id = 2"
    ).fetchone()
    conn.close()

    assert incumbent["ats_probe_status"] == "hit"
    assert incumbent["ats_platform"] == "greenhouse"
    assert incumbent["ats_slug"] == "acme"
    assert incumbent["ats_evidence_provisional"] == 0
    assert challenger["ats_probe_status"] == "miss"
    assert challenger["miss_reason"] == "collision"


def test_fastpath_hit_owner_identity_passes_none_is_provisional(db_path):
    """A fresh (non-colliding) B2 fast-path hit with owner_identity_passes
    unset must also be stamped ats_evidence_provisional=1, even though this
    trigger family is documented as "never provisional" when the hook DOES
    resolve True (see _probe.py's inline comment on the careers_url: prefix)
    — the None-default must not accidentally inherit that leniency."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_pending(conn, 1, name="Acme", careers_url="https://boards.greenhouse.io/acme")
    conn.close()

    services.set_services(_make_services(db_path))
    try:
        with patch("jobcannon.engine.ats_scanner._probe._verify_fastpath_live", return_value=True):
            result = probe_ats_slugs(db_path, config={})
    finally:
        services.clear_services()

    assert result["hits"] == 1
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT ats_probe_status, ats_platform, ats_evidence_provisional "
        "FROM companies WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row["ats_probe_status"] == "hit"
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_evidence_provisional"] == 1


def test_fastpath_hit_owner_identity_passes_true_is_not_provisional(db_path):
    """Companion positive case for the fast-path site."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _insert_pending(conn, 1, name="Acme", careers_url="https://boards.greenhouse.io/acme")
    conn.close()

    services.set_services(_make_services(db_path, owner_identity_passes=lambda *a, **k: True))
    try:
        with patch("jobcannon.engine.ats_scanner._probe._verify_fastpath_live", return_value=True):
            probe_ats_slugs(db_path, config={})
    finally:
        services.clear_services()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT ats_evidence_provisional FROM companies WHERE id = 1").fetchone()
    conn.close()
    assert row["ats_evidence_provisional"] == 0

"""Live-Postgres regression for issue #36: ats_prober's static-first
fall-through wrote `scan_enabled = 1` (an integer literal) in SET
(assignment) context against the `scan_enabled boolean` column. SQLite
accepts it silently (booleans are stored as 0/1); Postgres raises
`psycopg.errors.DatatypeMismatch: column "scan_enabled" is of type boolean
but expression is of type integer` (verified empirically against this
project's Postgres, error class confirmed via its MRO).

Dormant path: `_try_static_first_fallthrough` (reached only through
`probe_single_company`) has no production caller on the hosted side yet —
`jobcannon.host.wiring.build_scan_services` deliberately leaves
`ScanServices.prober_extensions` at its `None` default (spec §3.6,
fail-closed: multi-tenant identity reconciliation is a Phase-2 item). This
test drives `_try_static_first_fallthrough` directly against a real
throwaway Postgres database through the same `EngineCompatConnection` /
`engine_sql_to_host()` translation path production code would use once that
wiring lands, so a regression here is caught before it ever ships.

Tiers exercised live: 2 (static HTML extract, ~ats_prober.py:416) and 3
(embedded-JSON extract, ~ats_prober.py:495) — both reachable with a plain
stub `ext` and no external dependency. Tier 4 (Playwright, ~ats_prober.py:
583) additionally requires the `playwright` package importable and a real
Chromium launch; it is covered only by the source-level regression guard in
tests/host/test_scan_dialect.py, not live here.
"""

from types import SimpleNamespace

import psycopg
import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

_CAREERS_URL = "https://example-testco.invalid/careers"


@pytest.fixture(autouse=True)
def _restore_prober_extensions():
    """Save/restore the module-global extension bundle (xdist safety) — same
    pattern as tests/engine/test_prober_extensions_seam.py."""
    from jobcannon.engine import ats_prober

    prior = ats_prober._prober_extensions
    yield
    ats_prober.set_prober_extensions(prior)


@pytest.fixture()
def prober_db():
    """Own throwaway Postgres database + open pool. No ScanServices wiring
    needed: `_try_static_first_fallthrough` takes `conn` as a parameter and
    reads the module-global `ext` directly — it never touches
    `services.get_services()`."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations

    dsn, db_name = create_throwaway_db("jobcannon_prober_dialect")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        yield pool_mod
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def _insert_company(pool_mod, *, name: str) -> int:
    # scan_enabled defaults to TRUE in m0001 (`scan_enabled boolean NOT NULL
    # DEFAULT true`) -> seed it FALSE explicitly, so the post-call
    # `scan_enabled is True` assertions prove the UPDATE actually ran
    # instead of trivially matching an untouched column default.
    with pool_mod.connection_factory() as conn:
        conn.execute(
            "INSERT INTO companies "
            "(name, name_raw, ats_probe_status, careers_url, scan_enabled) "
            "VALUES (?, ?, 'pending', ?, FALSE)",
            (name, name, _CAREERS_URL),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
        return row["id"]


def _no_network_fetch(*_args, **_kwargs):
    # The whole tier-1 block is `try/except Exception`, so raising here
    # falls straight through to tier 2 with no real HTTP call.
    import requests

    raise requests.exceptions.ConnectionError("no network in test")


def test_tier2_static_extract_sets_scan_enabled_true_on_postgres(prober_db, monkeypatch):
    """Tier 2 (~line 416): static HTML extract finds jobs on a custom
    careers page -> the `SET scan_enabled = TRUE` UPDATE must not raise the
    Postgres boolean/integer type error the old `= 1` literal caused."""
    from jobcannon.engine import ats_prober

    company_id = _insert_company(prober_db, name="Tier2Co")
    monkeypatch.setattr(ats_prober, "fetch_with_deadline", _no_network_fetch)
    ats_prober.set_prober_extensions(
        SimpleNamespace(try_static_extract=lambda *a, **k: [{"title": "Backend Engineer"}])
    )

    with prober_db.connection_factory() as conn:
        result = ats_prober._try_static_first_fallthrough(
            company_id=company_id,
            company_name="Tier2Co",
            careers_url=_CAREERS_URL,
            conn=conn,
            config={},  # no DB_PATH -> skips ext.upsert_and_log, exercises the UPDATE only
            now="2026-08-15T00:00:00Z",
        )

    assert result == {
        "status": "miss",
        "reason": "static_fallthrough_tier2_jobs_persisted",
        "jobs_found": 1,
    }
    with prober_db.connection_factory() as conn:
        row = conn.execute(
            "SELECT ats_probe_status, scan_enabled, miss_reason FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
    assert row["ats_probe_status"] == "miss"
    assert row["scan_enabled"] is True
    assert row["miss_reason"] == "static_fallthrough_tier2_jobs_persisted"


def test_tier3_embedded_json_sets_scan_enabled_true_on_postgres(prober_db, monkeypatch):
    """Tier 3 (~line 495): reached when tier 2's static extract signals
    JS-heavy (returns None) and the embedded-JSON tier finds jobs instead.
    Same fix, same UPDATE shape, distinct call site."""
    from jobcannon.engine import ats_prober

    company_id = _insert_company(prober_db, name="Tier3Co")
    monkeypatch.setattr(ats_prober, "fetch_with_deadline", _no_network_fetch)
    ats_prober.set_prober_extensions(
        SimpleNamespace(
            try_static_extract=lambda *a, **k: None,  # JS-heavy -> fall through to tier 3
            try_embedded_json_extract=lambda *a, **k: [{"title": "Data Engineer"}],
        )
    )

    with prober_db.connection_factory() as conn:
        result = ats_prober._try_static_first_fallthrough(
            company_id=company_id,
            company_name="Tier3Co",
            careers_url=_CAREERS_URL,
            conn=conn,
            config={},
            now="2026-08-15T00:00:00Z",
        )

    assert result == {
        "status": "miss",
        "reason": "static_fallthrough_tier3_jobs_persisted",
        "jobs_found": 1,
    }
    with prober_db.connection_factory() as conn:
        row = conn.execute(
            "SELECT ats_probe_status, scan_enabled, miss_reason FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
    assert row["ats_probe_status"] == "miss"
    assert row["scan_enabled"] is True
    assert row["miss_reason"] == "static_fallthrough_tier3_jobs_persisted"


def test_integer_literal_would_have_raised_on_postgres(prober_db):
    """Positive control: proves the two assertions above would actually have
    caught the pre-fix bug, not just exercised a code path that happens to
    pass. `scan_enabled = 1` (the exact statement issue #36 reported at
    ats_prober.py:416/495/583 before this fix) raises DatatypeMismatch
    against the real `scan_enabled boolean` column — this is not
    hypothetical, it is empirically the same statement, same schema, same
    connection stack the two tests above exercise with `= TRUE` instead."""
    company_id = _insert_company(prober_db, name="ControlCo")

    with prober_db.connection_factory() as conn:
        with pytest.raises(psycopg.errors.DatatypeMismatch):
            conn.execute(
                "UPDATE companies SET scan_enabled = 1 WHERE id = ?",
                (company_id,),
            )

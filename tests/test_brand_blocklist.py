"""F8 — brand blocklist unit + invariant tests.

The blocklist gate (`jobcannon.engine.brand_blocklist.is_blocked_brand`) is the
sole defense against the brand-collision FPs that F4-resume exposed (Shopify,
Atos, Circle, Canva, LHH, Wal-Mart, Atrium). These tests pin the empirical
behavior so future blocklist edits surface regressions.

Why no fuzzy/domain-mismatch tests in this file: the F8 design history
explored fetching the tenant's claimed company-name from each ATS API and
comparing to `name_raw`. Empirical recon (HANDOFF.md F8 section) showed all
7 FP tenants self-identify with the SAME name as our DB record, so
name-comparison is a no-op for the cohort. The blocklist is the only signal
that empirically catches these. See `brand_blocklist.py` module docstring.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from jobcannon.engine.brand_blocklist import (
    _BLOCKED_NORMALIZED,
    _normalize_brand,
    is_blocked_brand,
)

# ---------------------------------------------------------------------------
# Tier 1: 7 F4-resume confirmed FPs — the entire reason this gate exists.
# Pin each as a separate test so a regression names the specific brand.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name_raw,reason",
    [
        ("Shopify", "pinpoint/shopify is a different small co"),
        ("Atos", "bamboohr/atos is a different small co"),
        ("Circle", "recruitee/circle is a different small co"),
        ("Canva", "bamboohr/canva is a different small co"),
        ("LHH", "pinpoint/lhh is a different small co"),
        ("Wal-Mart", "recruitee/walmart is a different small co"),
        ("Atrium", "bamboohr/atrium is a different small co"),
    ],
)
def test_known_f4_reverted_fps_are_blocked(name_raw: str, reason: str) -> None:
    assert is_blocked_brand(name_raw), f"{name_raw!r} should be blocked: {reason}"


# ---------------------------------------------------------------------------
# Normalization: case, punctuation, suffixes, hyphens, whitespace.
# These cover the variants we actually see in `companies.name_raw`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant,expected_norm",
    [
        # Hyphen collapse — Wal-Mart was the regression that motivated
        # collapsing all non-alnum (not just spaces).
        ("Wal-Mart", "walmart"),
        ("WAL-MART", "walmart"),
        ("Walmart Inc.", "walmart"),
        ("Walmart, Inc.", "walmart"),
        ("walmart inc", "walmart"),
        # Case insensitivity
        ("LHH", "lhh"),
        ("lhh", "lhh"),
        ("Lhh", "lhh"),
        # Suffix strip
        ("Shopify Inc.", "shopify"),
        ("Atos LLC", "atos"),
        ("Atrium Corp", "atrium"),
        ("Atrium Corporation", "atrium"),
        # Trailing punctuation
        ("Canva.", "canva"),
        ("Circle,", "circle"),
        # Multi-word names: whitespace collapses
        ("Bank of America", "bankofamerica"),
        ("Bristol-Myers Squibb", "bristolmyerssquibb"),
        ("Johnson & Johnson", "johnsonjohnson"),
        # Empty / None
        ("", ""),
    ],
)
def test_normalize_brand(variant: str, expected_norm: str) -> None:
    assert _normalize_brand(variant) == expected_norm


def test_normalize_brand_handles_none() -> None:
    # is_blocked_brand should accept None gracefully; normalize is only
    # called via is_blocked_brand.
    assert is_blocked_brand(None) is False


def test_normalize_brand_handles_empty_string() -> None:
    assert is_blocked_brand("") is False


# ---------------------------------------------------------------------------
# Negative cases: legitimate small-company names we MUST NOT block.
# Sample drawn from the prior 906 hits that the F6 audit verified.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "legit_name",
    [
        # Verified-real F4-resume retentions
        "Scribd, Inc.",
        "Onto Innovation",
        "Enverus",
        # Random sample of prior-906 hits (these are real Greenhouse/Ashby/Lever
        # tenants, NOT brand collisions). If any of these start blocking,
        # the blocklist has been over-extended.
        "Ursus, Inc.",
        "Specright",
        "Auxia",
        "Innodata",
        "Plaid",  # NOTE: would be a candidate to add later if we see collisions
        "1mind",
        "AKASA",
        "Cribl",
        # Sub-brands of famous co (DeepMind is Google-owned but has its own slug)
        "DeepMind",
        "Acme Corp",  # Synthetic placeholder
    ],
)
def test_legit_names_not_blocked(legit_name: str) -> None:
    assert not is_blocked_brand(legit_name), f"{legit_name!r} should NOT be blocked"


# ---------------------------------------------------------------------------
# Seed-list integrity invariants.
# ---------------------------------------------------------------------------


def test_blocklist_is_non_empty() -> None:
    assert len(_BLOCKED_NORMALIZED) > 7  # at least the 7 must-haves + extras


def test_all_seed_entries_normalize_uniquely() -> None:
    # If a seed entry normalizes to the same key as another, the list is
    # cluttered — surface as a warning so curation stays clean.
    from jobcannon.engine.brand_blocklist import _SEED

    normalized = [_normalize_brand(s) for s in _SEED]
    # Allow duplicates in the seed (e.g. 'Walmart' and 'Wal-Mart' both
    # normalize to 'walmart') as long as the frozenset shrinks accordingly.
    assert len(_BLOCKED_NORMALIZED) == len(set(normalized))


def test_seed_entries_self_block() -> None:
    """Every seed entry, fed through is_blocked_brand, must return True."""
    from jobcannon.engine.brand_blocklist import _SEED

    for entry in _SEED:
        assert is_blocked_brand(entry), f"Seed entry {entry!r} fails self-block"


# ---------------------------------------------------------------------------
# Invariant: existing 'hit' rows are NOT re-probed by the speculative loop,
# so blocking famous brand names has no retroactive effect on legitimate
# hits that have already been recorded.
#
# This is enforced at the SQL level in `probe_ats_slugs` and the F4 driver
# (both filter on ats_probe_status). The test below uses a real migrated DB
# to assert the invariant holds and the gate behaves correctly at that
# boundary.
# ---------------------------------------------------------------------------


def _insert_company(
    conn: sqlite3.Connection,
    *,
    name_raw: str,
    status: str,
    platform: str | None = None,
    slug: str | None = None,
) -> None:
    """Insert a minimal companies row. Both `name` (normalized) and `name_raw`
    are NOT NULL — production uses lowercased name_raw for the `name` column.
    """
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_platform, ats_slug,
                                  ats_probe_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name_raw.lower(), name_raw, platform, slug, status, now, now),
    )
    conn.commit()


def test_existing_famous_hits_not_re_probed(migrated_db_path: str) -> None:
    """A 'hit' row for a blocked brand name is preserved as-is.

    The speculative-probe loop only acts on `ats_probe_status IN ('pending',
    'miss')`. Adding 'Walmart' to the blocklist does NOT invalidate the
    existing Walmart→Workday hit (id 207 in production).
    """
    from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

    conn = sqlite3.connect(migrated_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _insert_company(
            conn,
            name_raw="Walmart",
            status="hit",
            platform="workday",
            slug="walmart.wd5/WalmartExternal",
        )

        summary = probe_ats_slugs(migrated_db_path, {"TESTING": False})

        row = conn.execute("SELECT * FROM companies WHERE name_raw = ?", ("Walmart",)).fetchone()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "workday"
        assert row["ats_slug"] == "walmart.wd5/WalmartExternal"
        # No probes attempted (the row wasn't in 'pending')
        assert summary["probed"] == 0
    finally:
        conn.close()


def test_blocked_pending_row_marked_miss_with_reason(migrated_db_path: str, monkeypatch) -> None:
    """A 'pending' row for a blocked brand → marked miss with reason, no HTTP."""
    from jobcannon.engine.ats_scanner import _probe as probe_mod

    # Patch _PROBES to a sentinel that would explode — so if the blocklist
    # DOES NOT short-circuit, the test fails with a clear assertion.
    def exploding_probe(slug: str) -> bool:  # pragma: no cover — should never run
        raise AssertionError(f"probe was called for a blocked brand (slug={slug!r})")

    monkeypatch.setattr(
        probe_mod,
        "_PROBES",
        [("lever", exploding_probe), ("greenhouse", exploding_probe)],
    )

    conn = sqlite3.connect(migrated_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _insert_company(conn, name_raw="Shopify", status="pending")

        summary = probe_mod.probe_ats_slugs(migrated_db_path, {"TESTING": False})

        row = conn.execute("SELECT * FROM companies WHERE name_raw = ?", ("Shopify",)).fetchone()
        assert row["ats_probe_status"] == "miss"
        assert row["miss_reason"] == "blocked_brand"
        assert row["ats_platform"] is None
        assert row["ats_slug"] is None
        assert summary["probed"] == 1
        assert summary["hits"] == 0
        assert summary["misses"] == 1
    finally:
        conn.close()


def test_non_blocked_pending_row_probes_normally(migrated_db_path: str, monkeypatch) -> None:
    """Sanity: blocking does not interfere with normal probe path."""
    from jobcannon.engine.ats_scanner import _probe as probe_mod

    calls: list[str] = []

    def lever_hit(slug: str) -> bool:
        calls.append(f"lever:{slug}")
        return True

    monkeypatch.setattr(probe_mod, "_PROBES", [("lever", lever_hit)])

    conn = sqlite3.connect(migrated_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _insert_company(conn, name_raw="SomeSmallStartup", status="pending")

        summary = probe_mod.probe_ats_slugs(migrated_db_path, {"TESTING": False})
        assert summary["hits"] == 1
        assert len(calls) >= 1  # at least one probe attempted
        row = conn.execute(
            "SELECT * FROM companies WHERE name_raw = ?", ("SomeSmallStartup",)
        ).fetchone()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "lever"
    finally:
        conn.close()

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

Ported from the private repo's tests/test_brand_blocklist.py. The DB/scanner
integration tests (tmp_db fixture + ats_scanner._probe interplay) are NOT
ported — ats_scanner is Task 3 (PR-3) scope, not this task's manifest.
"""

from __future__ import annotations

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

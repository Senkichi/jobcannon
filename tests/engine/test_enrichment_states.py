"""Invariants for the unified enrichment-tier vocabulary (F1 root cause).

`jobcannon/engine/enrichment_states.py` is the single source of truth for tier
names and the predicates over them. These tests pin:

- `TERMINAL` and `LOW_SIGNAL_TERMINAL` stay two distinct sets (collapsing them would
  silently re-label live rows).
- `resume_index` matches the historical `_start_tier_index` fail-closed semantics.
- `backfill_skip_sql` lists exactly the TERMINAL tiers.
- Grep gate: every `enrichment_tier = '<literal>'` SQL write in `jobcannon/` uses a
  value that is a member of the `EnrichmentTier` enum (no off-vocabulary literals).
- The classification.py consumer that aliases its own definition agrees with this
  module.

Ported from the private repo's tests/test_enrichment_states.py. The
data_enricher TIER_ORDER cross-check is NOT ported — data_enricher is
unported (Flask-side, out of this task's manifest); everything else is pure.
The grep-gate's scan root is repointed from job_finder/ to jobcannon/ (the
equivalent ported-package root) — the only non-trivial adaptation here.

F7 delta (private #1766): the `expired` tier and its membership in both
predicate sets are ported (pure vocabulary). The state machine that *writes*
`expired` — a cooldown-gated bounded retry against a hosted agentic
enricher, plus its supporting columns/migration — is WAIVED: no hosted LLM
caller or schema-migration authority exists in this engine yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jobcannon.engine.enrichment_states import (
    LOW_SIGNAL_TERMINAL,
    PIPELINE_ORDER,
    TERMINAL,
    EnrichmentTier,
    backfill_skip_sql,
    resume_index,
)

_JOB_FINDER_ROOT = Path(__file__).resolve().parents[2] / "jobcannon"


# ---------------------------------------------------------------------------
# Set distinctness — the load-bearing design decision
# ---------------------------------------------------------------------------


def test_low_signal_terminal_is_strict_subset_of_terminal():
    """Every low-signal tier stops the backfill, but not every terminal tier is
    low_signal. The two predicates are different and must stay different."""
    assert LOW_SIGNAL_TERMINAL < TERMINAL


def test_expired_is_terminal_and_low_signal():
    """'expired' (bounded agentic_exhausted retry budget spent with no jd_full
    recovered, in the private original) is the terminal end-state — the cascade
    gave up exactly like agentic_exhausted, so it belongs in both predicate
    sets. The writer for this transition is unported (see the module
    docstring); this pins the vocabulary membership only."""
    assert EnrichmentTier.EXPIRED in TERMINAL
    assert EnrichmentTier.EXPIRED in LOW_SIGNAL_TERMINAL


def test_terminal_has_members_that_are_not_low_signal():
    """serpapi / mid stop re-enrichment but are NOT low_signal — preserves the
    pre-refactor split exactly."""
    non_low_signal = TERMINAL - LOW_SIGNAL_TERMINAL
    assert EnrichmentTier.SERPAPI in non_low_signal
    assert EnrichmentTier.MID in non_low_signal


def test_low_signal_terminal_matches_legacy_frozenset():
    """LOW_SIGNAL_TERMINAL == the historical _TERMINAL_ENRICHMENT_TIERS literal,
    plus 'expired' (a gave-up terminal exactly like agentic_exhausted)."""
    assert {t.value for t in LOW_SIGNAL_TERMINAL} == {
        "exhausted",
        "agentic",
        "agentic_exhausted",
        "expired",
    }


def test_terminal_matches_canonical_skip_set():
    """TERMINAL == the canonical data_enricher backfill skip-set (the complete one)."""
    assert {t.value for t in TERMINAL} == {
        "exhausted",
        "serpapi",
        "agentic",
        "mid",
        "agentic_exhausted",
        "expired",
        "low",
        "high",
    }


# ---------------------------------------------------------------------------
# resume_index — parity with the historical _start_tier_index
# ---------------------------------------------------------------------------


def test_resume_index_none_starts_at_zero():
    assert resume_index(None) == 0


@pytest.mark.parametrize(
    "tier,expected_offset",
    [("free", 1), ("ddg", 2), ("serpapi", 3), ("agentic", 4)],
)
def test_resume_index_resumes_from_next_tier(tier, expected_offset):
    assert resume_index(tier) == expected_offset


def test_resume_index_exhausted_is_terminal():
    # 'exhausted' is the last element of the full order -> len(pipeline)+1.
    assert resume_index("exhausted") == len(PIPELINE_ORDER) + 1


@pytest.mark.parametrize(
    "tier", ["agentic_exhausted", "expired", "low", "high", "some_future_tier"]
)
def test_resume_index_unknown_or_legacy_is_fail_closed(tier):
    """Legacy/unknown tiers are terminal: index >= len(PIPELINE_ORDER) so no tier runs."""
    assert resume_index(tier) >= len(PIPELINE_ORDER)


def test_resume_index_warns_on_unknown(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        resume_index("not_a_real_tier")
    assert "not_a_real_tier" in caplog.text


# ---------------------------------------------------------------------------
# backfill_skip_sql
# ---------------------------------------------------------------------------


def test_backfill_skip_sql_lists_every_terminal_tier():
    sql = backfill_skip_sql()
    assert sql.startswith("enrichment_tier NOT IN (")
    for tier in TERMINAL:
        assert f"'{tier.value}'" in sql


def test_backfill_skip_sql_omits_resumable_tiers():
    sql = backfill_skip_sql()
    # free / ddg are resumable — they must NOT be in the skip-set.
    assert "'free'" not in sql
    assert "'ddg'" not in sql


def test_backfill_skip_sql_custom_column():
    assert backfill_skip_sql("t.enrichment_tier").startswith("t.enrichment_tier NOT IN (")


# ---------------------------------------------------------------------------
# Grep gate — every enrichment_tier SQL literal is a member of the enum
# ---------------------------------------------------------------------------

# Matches `enrichment_tier = 'literal'` / `enrichment_tier='literal'` writes in SQL.
_TIER_LITERAL_RE = re.compile(r"enrichment_tier\s*=\s*'([a-z_]+)'", re.IGNORECASE)

_VALID_TIER_VALUES = {t.value for t in EnrichmentTier}


def test_all_enrichment_tier_sql_literals_are_enum_members():
    """No off-vocabulary enrichment_tier literal may be written anywhere in the
    package — the F1 divergence class. Migrations are exempt: they legitimately
    reference historical pre-rename values (e.g. m050's 'haiku'/'sonnet')."""
    offenders: list[str] = []
    for py in _JOB_FINDER_ROOT.rglob("*.py"):
        if "__pycache__" in py.parts or "migrations" in py.parts:
            continue
        text = py.read_text(encoding="utf-8")
        for match in _TIER_LITERAL_RE.finditer(text):
            value = match.group(1)
            if value not in _VALID_TIER_VALUES:
                rel = py.relative_to(_JOB_FINDER_ROOT)
                offenders.append(f"{rel}: enrichment_tier = '{value}'")
    assert not offenders, (
        "Off-vocabulary enrichment_tier literal(s) found — every tier string written "
        f"in jobcannon/ must be an EnrichmentTier member: {offenders}"
    )


# ---------------------------------------------------------------------------
# Consumers agree with the single source of truth
# ---------------------------------------------------------------------------


def test_classification_terminal_set_aliases_low_signal_terminal():
    from jobcannon.engine.classification import _TERMINAL_ENRICHMENT_TIERS

    assert set(_TERMINAL_ENRICHMENT_TIERS) == set(LOW_SIGNAL_TERMINAL)

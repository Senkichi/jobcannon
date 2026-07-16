"""Single source of truth for enrichment-tier vocabulary (F1 root-cause fix).

The ``jobs.enrichment_tier`` column records the highest enrichment tier attempted
for a job so future backfill passes resume from the next tier up. Before this
module, the tier names and the predicates over them ("which tiers are terminal",
"which tiers stop the backfill", "where does a given tier resume") were maintained
by hand in three places that had to agree but drifted apart — the F1 infinite-loop
bug was exactly such a divergence (legacy/agentic terminal tiers missing from one
backfill skip-set). This module owns the vocabulary; every caller imports from here.

Two distinct predicates over tiers — they MUST stay distinct (see issue #260):

  ``TERMINAL`` — "stop re-enriching." A row at one of these tiers is fully drained
      of the enrichment pipeline and is excluded from backfill selection. Includes
      pipeline terminals (``serpapi``, ``agentic``), the explicit ``exhausted`` /
      ``agentic_exhausted`` end-states, and the legacy ``mid`` / ``low`` / ``high``
      tiers (m050). NOTE: a short JD at ``serpapi`` / ``mid`` is *not* low_signal —
      those tiers stop the backfill but do not mark the JD unobtainable.

  ``LOW_SIGNAL_TERMINAL`` — "JD genuinely unobtainable -> low_signal." A subset of
      TERMINAL where the *enrichment cascade itself* gave up trying to fetch a real
      JD (``exhausted``, ``agentic``, ``agentic_exhausted``). Only these tiers feed
      the ``derive_classification`` low_signal rule. Collapsing this into TERMINAL
      would silently re-label live rows, so the two sets are kept separate.

Legacy tiers (``low`` / ``mid`` / ``high``) are enumerated as terminal members.
They were written by the deleted haiku/sonnet synthesis tiers and renamed by m050;
no normalization migration is planned. Strings stay in the DB — this enum is a
Python-side vocabulary only, so existing string values in ``jobs.enrichment_tier``
need no migration.
"""

from __future__ import annotations

import logging
from enum import StrEnum

logger = logging.getLogger(__name__)


class EnrichmentTier(StrEnum):
    """Canonical enrichment-tier names. ``.value`` is the string stored in the DB.

    Active pipeline tiers (cost-ordered): FREE -> DDG -> SERPAPI -> AGENTIC.
    End-states: EXHAUSTED (all pipeline tiers tried), AGENTIC_EXHAUSTED (written by
    agentic_enricher after the agentic tier fails to fetch a JD).
    Legacy (m050, terminal): LOW / MID / HIGH — left by the deleted synthesis tiers.
    """

    # Active cost-ordered pipeline tiers
    FREE = "free"
    DDG = "ddg"
    SERPAPI = "serpapi"
    AGENTIC = "agentic"

    # Pipeline end-states
    EXHAUSTED = "exhausted"
    AGENTIC_EXHAUSTED = "agentic_exhausted"

    # Legacy migration tiers (m050) — terminal, no normalization planned
    LOW = "low"
    MID = "mid"
    HIGH = "high"


# Cost-ordered resumable pipeline. ``enrich_job`` walks these in order; a job whose
# recorded tier is one of these resumes from the NEXT index. ``EXHAUSTED`` is the
# sentinel appended after the pipeline (index len(PIPELINE_ORDER)) so a resume past
# AGENTIC lands on it.
PIPELINE_ORDER: tuple[EnrichmentTier, ...] = (
    EnrichmentTier.FREE,
    EnrichmentTier.DDG,
    EnrichmentTier.SERPAPI,
    EnrichmentTier.AGENTIC,
)

# Full ordered tier list including the EXHAUSTED sentinel. Mirrors the historical
# ``TIER_ORDER`` list in data_enricher (free, ddg, serpapi, agentic, exhausted) so
# ``resume_index`` indexing is identical.
_TIER_ORDER: tuple[EnrichmentTier, ...] = (*PIPELINE_ORDER, EnrichmentTier.EXHAUSTED)

# "Stop re-enriching" — excluded from backfill selection. Pipeline terminals plus
# explicit end-states plus the legacy m050 tiers. This is the canonical skip-set
# both backfill queries build their WHERE clause from.
TERMINAL: frozenset[EnrichmentTier] = frozenset(
    {
        EnrichmentTier.SERPAPI,
        EnrichmentTier.AGENTIC,
        EnrichmentTier.EXHAUSTED,
        EnrichmentTier.AGENTIC_EXHAUSTED,
        EnrichmentTier.MID,
        EnrichmentTier.LOW,
        EnrichmentTier.HIGH,
    }
)

# "JD genuinely unobtainable -> low_signal" — the subset of TERMINAL where the
# enrichment cascade itself exhausted its attempts to fetch a real JD. ONLY these
# tiers participate in the derive_classification low_signal rule. Kept distinct from
# TERMINAL on purpose (issue #260): serpapi/mid stop the backfill but are not
# low_signal. Identical to the historical ``_TERMINAL_ENRICHMENT_TIERS`` frozenset.
LOW_SIGNAL_TERMINAL: frozenset[EnrichmentTier] = frozenset(
    {
        EnrichmentTier.EXHAUSTED,
        EnrichmentTier.AGENTIC,
        EnrichmentTier.AGENTIC_EXHAUSTED,
    }
)


def resume_index(tier: str | None) -> int:
    """Return the index in the pipeline to resume enrichment from.

    ``None`` (brand-new job) starts at 0. A known tier resumes from the NEXT tier
    (its index + 1). An unknown / unrecognised tier is treated as terminal
    (fail-closed: returns ``len(PIPELINE_ORDER) + 1`` so no pipeline tier runs) and
    logs a warning — this is the F1 hotfix's fail-closed ``_start_tier_index``,
    folded into the shared module.

    Args:
        tier: The ``jobs.enrichment_tier`` value (raw DB string) or None.

    Returns:
        Index into the pipeline; a value >= len(PIPELINE_ORDER) means "no tier runs".
    """
    if tier is None:
        return 0
    try:
        idx = _TIER_ORDER.index(EnrichmentTier(tier))
        return idx + 1  # Resume from NEXT tier
    except ValueError:
        logger.warning(
            "Unknown enrichment_tier %r — treating as terminal (fail-closed); no tiers will run",
            tier,
        )
        return len(_TIER_ORDER)


def backfill_skip_sql(column: str = "enrichment_tier") -> str:
    """Return a ``NOT IN (...)`` SQL fragment listing every TERMINAL tier value.

    Single source for the backfill skip-set, replacing the hand-maintained
    ``NOT IN (...)`` literals that drifted apart (the F1 root cause). Tier values
    are enum members (controlled vocabulary, not user input) so direct interpolation
    is safe; ``column`` defaults to the canonical column name.

    Args:
        column: Column name to test against the skip-set.

    Returns:
        A SQL fragment, e.g. ``enrichment_tier NOT IN ('agentic', 'exhausted', ...)``.
    """
    values = ", ".join(f"'{tier.value}'" for tier in sorted(TERMINAL, key=lambda t: t.value))
    return f"{column} NOT IN ({values})"

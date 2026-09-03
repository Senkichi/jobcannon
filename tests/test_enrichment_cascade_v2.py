# PORTED from tests/test_enrichment_cascade_v2.py @ 8432ac343ae2412fcd2bb0051e3244e202496b09 (private job-cannon). Ledger L-0523.
"""Tests for the synthesis-free enrichment cascade.

After Phase 2b sub-fix (RC4), the cascade no longer contains the LLM
synthesis steps that fabricated short pseudo-JDs from fragments and blocked
escalation to true fetch tiers. The new cascade is strictly fetch-based:
free -> ddg -> serpapi -> agentic -> exhausted.
"""

from jobcannon.engine.data_enricher import FIELD_TIER_CEILINGS, TIER_ORDER


def test_tier_order_excludes_low_and_mid():
    assert "low" not in TIER_ORDER
    assert "mid" not in TIER_ORDER
    assert TIER_ORDER == ["free", "ddg", "serpapi", "agentic", "exhausted"]


def test_field_tier_ceiling_for_jd_full_caps_at_agentic():
    assert FIELD_TIER_CEILINGS["jd_full"] == "agentic"


def test_field_tier_ceilings_no_low_or_mid_references():
    for field, ceiling in FIELD_TIER_CEILINGS.items():
        assert ceiling not in ("low", "mid"), (
            f"Field {field} still references tier {ceiling} outside fetch cascade"
        )

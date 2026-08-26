"""Direct tests for job_scorer.scoring_precheck's own gating logic.

scoring_precheck is exercised indirectly through score_job in
test_job_scorer.py (TestSkipPrecondition — the jd_full gate) and
test_location_gate.py (the location gate, terminal-tier passthrough, and
location_missing passthrough) — those tests pin score_job's end-to-end skip
behavior via its ScoringResult envelope. This file calls scoring_precheck
directly to pin its pure-function contract in isolation, including the
malformed-JSON fail-safe paths (locations_structured / unresolved_reasons
parsing is wrapped in try/except(json.JSONDecodeError, TypeError)) that
neither the private repo's own test suite for this module nor the ported
score_job-level tests exercise (added per PR #3 review finding 2).
"""

from __future__ import annotations

from jobcannon.engine.jd_content_contract import JD_CONTENT_VERSION, JdVerdict
from jobcannon.engine.job_scorer import scoring_precheck


def _base_job(**overrides) -> dict:
    """Job with all fields present so the gates all pass by default."""
    base = {
        "jd_full": "Build ML systems. Python, SQL, AWS required.",
        "location": "Remote US",
        "locations_structured": "[]",
        "enrichment_tier": "free",
        "unresolved_reasons": "[]",
    }
    base.update(overrides)
    return base


class TestJdGate:
    """SCORER-05: jd_full absent/empty/whitespace-only -> awaiting_jd."""

    def test_empty_jd_full_gates(self):
        assert scoring_precheck(_base_job(jd_full="")) == "awaiting_jd"

    def test_none_jd_full_gates(self):
        assert scoring_precheck(_base_job(jd_full=None)) == "awaiting_jd"

    def test_whitespace_only_jd_full_gates(self):
        assert scoring_precheck(_base_job(jd_full="   ")) == "awaiting_jd"

    def test_missing_jd_full_key_gates(self):
        job = _base_job()
        del job["jd_full"]
        assert scoring_precheck(job) == "awaiting_jd"


class TestLocationGate:
    """P3.2: no location signal + still-enrichable -> awaiting_location."""

    def test_complete_location_passes(self):
        assert scoring_precheck(_base_job()) is None

    def test_empty_location_non_terminal_tier_gates(self):
        job = _base_job(location="", locations_structured="[]", enrichment_tier="free")
        assert scoring_precheck(job) == "awaiting_location"

    def test_flat_location_alone_passes(self):
        job = _base_job(location="Remote", locations_structured="[]")
        assert scoring_precheck(job) is None

    def test_structured_location_alone_passes(self):
        job = _base_job(location="", locations_structured='[{"city": "Austin"}]')
        assert scoring_precheck(job) is None

    def test_terminal_tier_passes_despite_empty_location(self):
        job = _base_job(location="", locations_structured="[]", enrichment_tier="exhausted")
        assert scoring_precheck(job) is None

    def test_location_missing_reason_passes_despite_empty_location(self):
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier="free",
            unresolved_reasons='["location_missing"]',
        )
        assert scoring_precheck(job) is None


class TestJdContentGate:
    """D5/#1742 (F7 port of private #1794): scoring_precheck defers a row whose
    PERSISTED jd_content_verdict is not CLEAN and the adjudicator has not
    vouched for it. The gate reads the column only — it never recomputes
    classify_jd_content here (that cost belongs at a host's set_jd_full write
    chokepoint). A NULL verdict (no host has stamped this row) fails OPEN."""

    def test_clean_verdict_passes(self):
        job = _base_job(jd_content_verdict=JdVerdict.CLEAN.value)
        assert scoring_precheck(job) is None

    def test_null_verdict_fails_open(self):
        """No jd_content_verdict key at all (the common case on this engine
        today, since no public jd_full writer stamps it yet) is NOT gated."""
        job = _base_job()
        assert "jd_content_verdict" not in job
        assert scoring_precheck(job) is None

    def test_ambiguous_verdict_non_adjudicated_gates(self):
        job = _base_job(jd_content_verdict=JdVerdict.AMBIGUOUS.value)
        assert scoring_precheck(job) == "awaiting_jd_adjudication"

    def test_reject_verdict_non_adjudicated_gates(self):
        job = _base_job(jd_content_verdict=JdVerdict.REJECT.value)
        assert scoring_precheck(job) == "awaiting_jd_adjudication"

    def test_adjudicated_non_clean_verdict_passes(self):
        """jd_adjudicated_version stamped at-or-above the current contract
        version means the adjudicator already vouched for this row — the
        gate must not re-block a row it already resolved."""
        job = _base_job(
            jd_content_verdict=JdVerdict.AMBIGUOUS.value,
            jd_adjudicated_version=JD_CONTENT_VERSION,
        )
        assert scoring_precheck(job) is None

    def test_adjudicated_at_stale_version_still_gates(self):
        """A jd_adjudicated_version below the CURRENT contract version does
        not count as adjudicated — a contract-version bump re-arms the gate
        for rows only an older contract version vouched for."""
        job = _base_job(
            jd_content_verdict=JdVerdict.REJECT.value,
            jd_adjudicated_version=JD_CONTENT_VERSION - 1,
        )
        assert scoring_precheck(job) == "awaiting_jd_adjudication"

    def test_non_int_adjudicated_version_treated_as_not_adjudicated(self):
        """A malformed jd_adjudicated_version (e.g. a stringly-typed value
        from a bad write path) fails safe: not treated as adjudicated."""
        job = _base_job(
            jd_content_verdict=JdVerdict.AMBIGUOUS.value,
            jd_adjudicated_version="5",
        )
        assert scoring_precheck(job) == "awaiting_jd_adjudication"


class TestMalformedJsonFailsSafe:
    """locations_structured / unresolved_reasons parsing never raises on bad
    input — malformed or non-list JSON is treated as empty/absent."""

    def test_malformed_locations_structured_treated_as_empty(self):
        """Unparseable locations_structured falls through to the location
        gate as if no structured data were present."""
        job = _base_job(
            location="",
            locations_structured="{not valid json",
            enrichment_tier="free",
        )
        assert scoring_precheck(job) == "awaiting_location"

    def test_non_list_locations_structured_treated_as_empty(self):
        """Valid JSON that parses to a non-list (e.g. a bare object) is not
        treated as structured location data."""
        job = _base_job(
            location="",
            locations_structured='{"city": "Austin"}',
            enrichment_tier="free",
        )
        assert scoring_precheck(job) == "awaiting_location"

    def test_malformed_unresolved_reasons_treated_as_empty(self):
        """Unparseable unresolved_reasons falls through as if no reasons
        were recorded — location_missing cannot be honored from garbage."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier="free",
            unresolved_reasons="not-json-at-all",
        )
        assert scoring_precheck(job) == "awaiting_location"

    def test_non_list_unresolved_reasons_treated_as_empty(self):
        """Valid JSON that parses to a non-list is not scanned for
        location_missing."""
        job = _base_job(
            location="",
            locations_structured="[]",
            enrichment_tier="free",
            unresolved_reasons='{"location_missing": true}',
        )
        assert scoring_precheck(job) == "awaiting_location"

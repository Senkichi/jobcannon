"""Tests for jobcannon.engine.constants, ported from the private repo's
tests/test_constants.py.

Verifies that pipeline status constants are internally consistent and conform
to the conventions relied upon by validation logic throughout the codebase.
"""

from jobcannon.engine.constants import PIPELINE_STATUSES, VALID_PIPELINE_STATUSES


class TestPipelineStatuses:
    """Invariants for PIPELINE_STATUSES and VALID_PIPELINE_STATUSES."""

    def test_all_statuses_are_lowercase(self):
        """Every status string must be lowercase (DB values are lowercase)."""
        for status in PIPELINE_STATUSES:
            assert status == status.lower(), f"Status {status!r} is not lowercase"

    def test_valid_pipeline_statuses_matches_pipeline_statuses(self):
        """VALID_PIPELINE_STATUSES must be the frozenset of PIPELINE_STATUSES."""
        assert frozenset(PIPELINE_STATUSES) == VALID_PIPELINE_STATUSES

    def test_no_duplicates_in_pipeline_statuses(self):
        """PIPELINE_STATUSES tuple must not contain duplicate values."""
        assert len(PIPELINE_STATUSES) == len(set(PIPELINE_STATUSES))

    def test_expected_core_statuses_present(self):
        """Core statuses required by pipeline logic must be present."""
        required = {
            "discovered",
            "reviewing",
            "applied",
            "archived",
            "rejected",
            "dismissed",
        }
        missing = required - VALID_PIPELINE_STATUSES
        assert not missing, f"Missing required statuses: {missing}"

    def test_valid_pipeline_statuses_is_frozenset(self):
        """VALID_PIPELINE_STATUSES must be a frozenset for O(1) membership tests."""
        assert isinstance(VALID_PIPELINE_STATUSES, frozenset)

    def test_pipeline_statuses_is_non_empty(self):
        """PIPELINE_STATUSES must define at least one status."""
        assert len(PIPELINE_STATUSES) > 0

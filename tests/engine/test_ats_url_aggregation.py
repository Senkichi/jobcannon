"""Tests for ats_detection's URL-ranking and multi-source aggregation logic.

Trimmed port of the private repo's test_ats_identity_reconcile.py: that
file's TestExtractAtsFromUrlBest and TestAggregateAtsCandidates classes
exercise only jobcannon.engine.ats_detection (Task 2 scope, ported here).
The rest of that file — TestReconcileCompanyAts, TestVerifyFailedNegativeCache,
TestPromoteFromCareersLinkReenableScan — tests
job_finder.web.ats_identity_reconcile against a migrated sqlite DB (Task 3
scope, not ported by this PR) and is deliberately left behind.
"""

from jobcannon.engine.ats_detection import (
    aggregate_ats_candidates_from_job_bundles,
    extract_ats_from_url_best,
)


class TestExtractAtsFromUrlBest:
    def test_api_greenhouse_outranks_boards_for_same_slug(self):
        api = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        board = "https://boards.greenhouse.io/acme/jobs/1"
        assert extract_ats_from_url_best(api)[2] > extract_ats_from_url_best(board)[2]

    def test_lever_api_pattern(self):
        hit = extract_ats_from_url_best("https://api.lever.co/v0/postings/acme")
        assert hit == ("lever", "acme", 10)


class TestAggregateAtsCandidates:
    def test_majority_picks_greenhouse(self):
        bundles = [
            {
                "dedup_key": "a",
                "last_seen": "2026-05-01T00:00:00",
                "urls": ["https://boards.greenhouse.io/winner/jobs/1"],
            },
            {
                "dedup_key": "b",
                "last_seen": "2026-05-02T00:00:00",
                "urls": ["https://boards.greenhouse.io/winner/jobs/2"],
            },
            {
                "dedup_key": "c",
                "last_seen": "2026-05-01T00:00:00",
                "urls": ["https://jobs.lever.co/loser/x"],
            },
        ]
        winner, abstain = aggregate_ats_candidates_from_job_bundles(bundles)
        assert abstain is None
        assert winner == ("greenhouse", "winner")

    def test_abstains_on_perfect_two_way_tie(self):
        bundles = [
            {
                "dedup_key": "a",
                "last_seen": "2026-05-01T12:00:00",
                "urls": ["https://jobs.lever.co/foo/x"],
            },
            {
                "dedup_key": "b",
                "last_seen": "2026-05-01T12:00:00",
                "urls": ["https://boards.greenhouse.io/bar/x"],
            },
        ]
        winner, abstain = aggregate_ats_candidates_from_job_bundles(bundles)
        assert winner is None
        assert abstain == "ambiguous_tie"

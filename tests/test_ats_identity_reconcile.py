"""Unit and integration tests for ATS URL→verify identity reconciliation (Phase A+B)."""

import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from jobcannon.engine.ats_detection import (
    ATS_EXTRACTOR_VERSION,
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


@pytest.fixture()
def seeded_pending_company(migrated_db_path):
    conn = sqlite3.connect(migrated_db_path)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO companies (name, name_raw, ats_probe_status, scan_enabled, created_at, updated_at)
           VALUES ('acme', 'Acme', 'pending', 1, ?, ?)""",
        (now, now),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
           first_seen, last_seen, company_id, pipeline_status)
           VALUES ('k1', 'T', 'Acme', 'Remote', '[]',
           ?, ?, ?, ?, 'discovered')""",
        (
            '["https://boards.greenhouse.io/acmecorp/jobs/1"]',
            now,
            now,
            cid,
        ),
    )
    conn.commit()
    conn.close()
    return migrated_db_path, int(cid)






def _seed_scan_disabled_miss(db_path: str) -> int:
    """Insert a frozen custom-miss company (scan_enabled=0) with a careers_url.

    ``db_path`` must already be a fully-migrated DB file (e.g. migrated_db_path).
    """
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        # WI-13 (D16): promote_from_careers_link now gates on ats_scan_enabled;
        # mirror the legacy scan_enabled=0 into it so the frozen row stays skipped.
        """INSERT INTO companies
              (name, name_raw, careers_url, ats_probe_status, miss_reason,
               scan_enabled, ats_scan_enabled, created_at, updated_at)
           VALUES ('frozenco', 'FrozenCo', 'https://frozenco.com/careers',
                   'miss', 'speculative_exhausted', 0, 0, ?, ?)""",
        (now, now),
    )
    cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return int(cid)




def _seed_promote_batch_companies(
    db_path: str,
    *,
    n_never_attempted: int,
    n_previously_attempted: int,
) -> tuple[list[int], list[int]]:
    """Seed eligible companies (status in miss/error/pending, scan_enabled=1) for
    ``promote_ats_scheduler_batch`` rotation tests.

    ``n_never_attempted`` companies get ``ats_promote_attempted_at = NULL``.
    ``n_previously_attempted`` companies get staggered old timestamps (lower id =
    older timestamp), so ordering within the group is deterministic.

    The promote batch orders by ``ats_promote_attempted_at`` (WI-12, D14), so
    that is the column these tests must key on. ``ats_probe_attempted_at`` is
    seeded to the same values for realism (the two cursors move together on a
    freshly-seeded DB) but does not govern selection.

    Returns (never_attempted_ids, previously_attempted_ids) each in insertion order.
    """
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    never_ids: list[int] = []
    for i in range(n_never_attempted):
        conn.execute(
            """INSERT INTO companies
                  (name, name_raw, ats_probe_status, scan_enabled,
                   ats_probe_attempted_at, ats_promote_attempted_at,
                   created_at, updated_at)
               VALUES (?, ?, 'miss', 1, NULL, NULL, ?, ?)""",
            (f"never-{i}", f"Never{i}", now, now),
        )
        never_ids.append(int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]))

    prior_ids: list[int] = []
    for i in range(n_previously_attempted):
        # Stagger so lower `i` (and thus lower id) is older / least-recently-attempted.
        ts = f"2026-01-{(i % 27) + 1:02d}T00:00:00"
        conn.execute(
            """INSERT INTO companies
                  (name, name_raw, ats_probe_status, scan_enabled,
                   ats_probe_attempted_at, ats_promote_attempted_at,
                   created_at, updated_at)
               VALUES (?, ?, 'miss', 1, ?, ?, ?, ?)""",
            (f"prior-{i}", f"Prior{i}", ts, ts, now, now),
        )
        prior_ids.append(int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]))

    conn.commit()
    conn.close()
    return never_ids, prior_ids








def _seed_company_with_job(
    db_path,
    *,
    name,
    name_raw,
    job_url,
    homepage_url=None,
    careers_url=None,
):
    """Seed a pending company plus one job whose source_urls carry ATS evidence.

    Returns the new company_id. Mirrors ``seeded_pending_company`` but lets the
    identity-gate tests vary name / host columns / evidence slug.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO companies
              (name, name_raw, homepage_url, careers_url,
               ats_probe_status, scan_enabled, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'pending', 1, ?, ?)""",
        (name, name_raw, homepage_url, careers_url, now, now),
    )
    cid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """INSERT INTO jobs (dedup_key, title, company, location, sources, source_urls,
               first_seen, last_seen, company_id, pipeline_status)
           VALUES ('kg1', 'T', ?, 'Remote', '[]', ?, ?, ?, ?, 'discovered')""",
        (name_raw, f'["{job_url}"]', now, now, cid),
    )
    conn.commit()
    conn.close()
    return cid


_GATE_CONFIG = {
    "ats": {
        "identity_reconcile": {
            "enabled": True,
            "shadow": False,
            "identity_gate_enabled": True,
        }
    }
}







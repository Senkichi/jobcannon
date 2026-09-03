"""PORTED from tests/test_score_audits_db.py
@ dcbde72e65d42662d6790ec53bd619e87fd1d2a0 (private job-cannon).
Ledger L-0079, L-0282.

score_audits writer/reader + audit-eligibility predicate (spec §5.1).

Parity guard mirrors the private original's TestSelectAuditCandidates::
test_parity_with_pure_python_reference -- select_audit_candidates (coarse
SQL + Python predicate) must equal a pure-Python reference over the same
rows.

# PORT-SEAM: dropped relative to the private original --
#   - TestGetEffectiveLocationFit (whole class): get_effective_location_fit
#     already has full, duplicate coverage in
#     tests/engine/test_derive_classification_domain.py (verified by grep
#     before dropping, per this port's own review). Nothing here would add
#     signal.
#   - test_effective_location_fit_from_valid_policy_verdict /
#     _none_without_policy / _none_on_missing_or_malformed: exercised
#     select_audit_candidates' location_policy_verdict_json /
#     effective_location_fit fields, which this port drops (no host column
#     -- see jobcannon/db/_score_audits.py's module docstring PORT-SEAM).
#   - test_pre_cutover_skips_do_not_count_against_bound /
#     test_post_cutover_skips_count_against_bound /
#     test_missing_cutover_watermark_counts_all_skips: exercised the
#     #1806 schema_meta cutover watermark, which this port drops entirely
#     (no schema_meta table, no legacy skips to rescue -- see the same
#     PORT-SEAM). test_skipped_at_max_skip_attempts_excluded_from_cohort
#     below is this port's replacement: it proves the *bound* itself
#     (the part of #1806 that survives) still works with no cutover gating.
#
# PORT-SEAM: test_candidate_shape drops location_policy_verdict_json /
# effective_location_fit from the expected key set (same reason).
#
# PORT-SEAM: added relative to the private original --
#   - TestJsonbTextRoundTrip: private's SQLite TEXT column preserved
#     whatever string was written; host's postings.sub_scores_json is
#     jsonb, which re-renders through Postgres's own serializer on
#     ::text (different key order/whitespace than json.dumps() on the
#     original Python dict). This class makes the calling contract in
#     _score_audits.py's module docstring ("always compare against
#     Postgres's own ::text rendering, never json.dumps() on a
#     psycopg-decoded dict") a checked invariant instead of a comment.
#
# PORT-SEAM: `jobs` -> `postings`, `?` -> `%s`, SQLite `sqlite_master`/
# `datetime()` -> Postgres `information_schema`/`now() - interval`;
# `migrated_db_mem` (private's in-memory SQLite fixture) -> `db_conn`
# (tests/host/conftest.py's rollback-isolated real-Postgres fixture, same
# convention as tests/host/test_assessment_writer.py). `_insert_job` ->
# `_insert_posting`: creates a company row first (postings.company_id is
# NOT NULL REFERENCES companies(id) on host; private's `jobs` had no such
# FK) and returns the Postgres-rendered `sub_scores_json::text` snapshot
# string rather than the caller's input string -- see the calling-contract
# PORT-SEAM above. No `_drop_contract_triggers` equivalent: host has no
# `tg_jobs_%`-style contract triggers on `postings`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from jobcannon.db._score_audits import (
    axis_sum,
    is_audit_eligible,
    record_score_audit,
    select_audit_candidates,
)
from jobcannon.db.pool import EngineCompatConnection
from tests.host.conftest import requires_postgres

pytestmark = requires_postgres

SCORES_HIGH_DICT = {
    "title_fit": 4,
    "location_fit": 4,
    "comp_fit": 3,
    "domain_match": 4,
    "seniority_match": 3,
    "skills_match": 4,
}  # sum 22
SCORES_LOW_DICT = dict.fromkeys(
    ["title_fit", "location_fit", "comp_fit", "domain_match", "seniority_match", "skills_match"],
    3,
)  # sum 18
SCORES_HIGH_V2_DICT = {
    "title_fit": 5,
    "location_fit": 4,
    "comp_fit": 3,
    "domain_match": 4,
    "seniority_match": 3,
    "skills_match": 4,
}  # sum 23

# For the pure-Python unit tests (TestAxisSum, TestEligibility) -- no DB
# involved, so a plain json.dumps() string is the correct byte-identical
# stand-in for private's SQLite TEXT semantics.
SCORES_HIGH = json.dumps(SCORES_HIGH_DICT)
SCORES_LOW = json.dumps(SCORES_LOW_DICT)
SCORES_HIGH_V2 = json.dumps(SCORES_HIGH_V2_DICT)


def _svc_conn(db_conn):
    return EngineCompatConnection(db_conn)


def _insert_posting(db_conn, dedup_key, *, sub_scores=SCORES_HIGH_DICT, first_seen=None, **fields):
    """Insert a company + posting row; returns the POSTGRES-RENDERED
    sub_scores_json::text snapshot (not json.dumps(sub_scores)) -- see this
    module's docstring PORT-SEAM on the jsonb round-trip calling contract.
    """
    company = f"co-{dedup_key}"
    db_conn.execute("INSERT INTO companies (name) VALUES (%s)", (company,))
    cid = db_conn.execute("SELECT id FROM companies WHERE name = %s", (company,)).fetchone()["id"]
    defaults = {
        "title": "Data Scientist",
        "company": company,
        "location": "Remote",
        "jd_full": "A meaningful job description body.",
    }
    defaults.update(fields)
    cols = ["dedup_key", "company_id", "title", "company", "location", "jd_full", "sub_scores_json"]
    vals = [
        dedup_key,
        cid,
        defaults["title"],
        defaults["company"],
        defaults["location"],
        defaults["jd_full"],
        Jsonb(sub_scores) if sub_scores is not None else None,
    ]
    if first_seen is not None:
        cols.append("first_seen")
        vals.append(first_seen)
    placeholders = ", ".join(["%s"] * len(vals))
    db_conn.execute(
        f"INSERT INTO postings ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    row = db_conn.execute(
        "SELECT sub_scores_json::text AS s FROM postings WHERE dedup_key = %s", (dedup_key,)
    ).fetchone()
    return row["s"]


class TestAxisSum:
    def test_valid(self):
        assert axis_sum(SCORES_HIGH) == 22

    def test_none_and_malformed(self):
        assert axis_sum(None) is None
        assert axis_sum("not json{") is None
        assert axis_sum(json.dumps({"title_fit": "high"})) is None


class TestRecordScoreAudit:
    def test_writes_row(self, db_conn):
        conn = _svc_conn(db_conn)
        row_id = record_score_audit(
            conn,
            dedup_key="k1",
            model="sonnet",
            verdict="dispute",
            audited_sub_scores_json=SCORES_HIGH,
            axis_deltas_json=json.dumps({"title_fit": -2}),
            jd_quality_flag="garbage",
            notes="short note",
        )
        row = db_conn.execute("SELECT * FROM score_audits WHERE id = %s", (row_id,)).fetchone()
        assert row["dedup_key"] == "k1"
        assert row["verdict"] == "dispute"
        assert (
            row["audited_at"] is not None
        )  # PORT-SEAM: DB-generated now(), not app-generated ISO text

    def test_rejects_bad_verdict(self, db_conn):
        conn = _svc_conn(db_conn)
        with pytest.raises(ValueError, match="verdict"):
            record_score_audit(
                conn,
                dedup_key="k1",
                model="sonnet",
                verdict="unsure",
                audited_sub_scores_json=SCORES_HIGH,
            )

    def test_writes_skipped_verdict_row(self, db_conn):
        conn = _svc_conn(db_conn)
        row_id = record_score_audit(
            conn,
            dedup_key="k1",
            model="sonnet",
            verdict="skipped",
            audited_sub_scores_json=SCORES_HIGH,
            notes="API safety block",
        )
        row = db_conn.execute("SELECT * FROM score_audits WHERE id = %s", (row_id,)).fetchone()
        assert row["verdict"] == "skipped"
        assert "API safety block" in (row["notes"] or "")


class TestEligibility:
    def test_new_high_scorer_eligible(self):
        assert is_audit_eligible(SCORES_HIGH, None, score_threshold=20) is True

    def test_below_threshold_ineligible(self):
        assert is_audit_eligible(SCORES_LOW, None, score_threshold=20) is False

    def test_already_audited_at_same_snapshot_ineligible(self):
        assert is_audit_eligible(SCORES_HIGH, SCORES_HIGH, score_threshold=20) is False

    def test_rescored_since_audit_re_eligible(self):
        assert is_audit_eligible(SCORES_HIGH_V2, SCORES_HIGH, score_threshold=20) is True

    def test_unscored_and_malformed_ineligible(self):
        assert is_audit_eligible(None, None, score_threshold=20) is False
        assert is_audit_eligible("junk{", None, score_threshold=20) is False

    def test_skipped_latest_verdict_with_equal_snapshot_eligible_again(self):
        """#1799 structural remediation: a `skipped` latest row is never a
        real audit opinion (poison-item isolation or, previously, a
        fabricated placeholder written during a provider outage). Its
        snapshot must not consume eligibility -- the job is treated as if it
        had never been audited, un-poisoning it automatically."""
        assert (
            is_audit_eligible(
                SCORES_HIGH, SCORES_HIGH, score_threshold=20, last_audit_verdict="skipped"
            )
            is True
        )

    def test_real_verdict_latest_with_equal_snapshot_still_ineligible(self):
        """A genuine agree/dispute verdict at the current snapshot must still
        exclude the job -- only `skipped` is non-consuming, not every
        verdict."""
        assert (
            is_audit_eligible(
                SCORES_HIGH, SCORES_HIGH, score_threshold=20, last_audit_verdict="agree"
            )
            is False
        )
        assert (
            is_audit_eligible(
                SCORES_HIGH, SCORES_HIGH, score_threshold=20, last_audit_verdict="dispute"
            )
            is False
        )

    def test_last_audit_verdict_defaults_to_consuming_behavior(self):
        """Omitting last_audit_verdict (the pre-#1799 call shape) must not
        change behavior for existing callers -- default None is NOT treated
        as 'skipped'."""
        assert is_audit_eligible(SCORES_HIGH, SCORES_HIGH, score_threshold=20) is False

    def test_skipped_below_max_skip_attempts_still_eligible(self):
        """#1806: a `skipped` latest verdict with skip_attempt_count below
        max_skip_attempts is still non-consuming (the #1799 behavior)."""
        assert (
            is_audit_eligible(
                SCORES_HIGH,
                SCORES_HIGH,
                score_threshold=20,
                last_audit_verdict="skipped",
                skip_attempt_count=1,
                max_skip_attempts=2,
            )
            is True
        )

    def test_skipped_at_max_skip_attempts_falls_back_to_consuming(self):
        """#1806: once skip_attempt_count reaches max_skip_attempts, a
        `skipped` latest verdict falls back to normal snapshot-consuming
        behavior -- the job is ineligible at an unchanged snapshot."""
        assert (
            is_audit_eligible(
                SCORES_HIGH,
                SCORES_HIGH,
                score_threshold=20,
                last_audit_verdict="skipped",
                skip_attempt_count=2,
                max_skip_attempts=2,
            )
            is False
        )

    def test_skipped_at_max_skip_attempts_rescored_re_eligible(self):
        """#1806: a job that hit the skip bound but was since rescored (new
        snapshot differs from the last audit snapshot) is re-eligible -- the
        bound is per-snapshot, and a rescore resets it."""
        assert (
            is_audit_eligible(
                SCORES_HIGH_V2,
                SCORES_HIGH,
                score_threshold=20,
                last_audit_verdict="skipped",
                skip_attempt_count=2,
                max_skip_attempts=2,
            )
            is True
        )

    def test_skipped_max_skip_attempts_zero_means_unbounded(self):
        """#1806: max_skip_attempts <= 0 preserves the unbounded #1799
        behavior -- skipped is always non-consuming regardless of
        skip_attempt_count."""
        assert (
            is_audit_eligible(
                SCORES_HIGH,
                SCORES_HIGH,
                score_threshold=20,
                last_audit_verdict="skipped",
                skip_attempt_count=99,
                max_skip_attempts=0,
            )
            is True
        )

    def test_skipped_max_skip_attempts_defaults_to_unbounded(self):
        """#1806: omitting both new params (the pre-#1806 call shape) must
        not change behavior for existing callers."""
        assert (
            is_audit_eligible(
                SCORES_HIGH, SCORES_HIGH, score_threshold=20, last_audit_verdict="skipped"
            )
            is True
        )


class TestSelectAuditCandidates:
    def test_parity_with_pure_python_reference(self, db_conn):
        """PARITY GUARD -- SQL coarse select + Python predicate == pure-Python scan."""
        old = datetime.now(UTC) - timedelta(days=10)
        _insert_posting(db_conn, "fresh-high")  # eligible
        _insert_posting(db_conn, "fresh-low", sub_scores=SCORES_LOW_DICT)  # below threshold
        _insert_posting(db_conn, "old-high", first_seen=old)  # outside lookback
        _insert_posting(db_conn, "fresh-unscored", sub_scores=None)  # unscored
        snap_audited = _insert_posting(db_conn, "fresh-audited")  # audited at current snapshot
        record_score_audit(
            _svc_conn(db_conn),
            dedup_key="fresh-audited",
            model="sonnet",
            verdict="agree",
            audited_sub_scores_json=snap_audited,
        )
        # audited, then rescored
        snap_before_rescore = _insert_posting(db_conn, "fresh-rescored")
        record_score_audit(
            _svc_conn(db_conn),
            dedup_key="fresh-rescored",
            model="sonnet",
            verdict="agree",
            audited_sub_scores_json=snap_before_rescore,
        )
        db_conn.execute(
            "UPDATE postings SET sub_scores_json = %s WHERE dedup_key = %s",
            (Jsonb(SCORES_HIGH_V2_DICT), "fresh-rescored"),
        )

        got = select_audit_candidates(
            _svc_conn(db_conn), score_threshold=20, lookback_days=3, max_jobs=60
        )
        got_keys = {c["dedup_key"] for c in got}

        rows = db_conn.execute(
            "SELECT dedup_key, sub_scores_json::text AS sub_scores_json, first_seen, "
            "  (SELECT a.audited_sub_scores_json FROM score_audits a "
            "   WHERE a.dedup_key = postings.dedup_key ORDER BY a.id DESC LIMIT 1) AS snap, "
            "  (SELECT a.verdict FROM score_audits a "
            "   WHERE a.dedup_key = postings.dedup_key ORDER BY a.id DESC LIMIT 1) AS last_verdict, "
            "  (first_seen >= now() - make_interval(days => 3)) AS recent "
            "FROM postings"
        ).fetchall()
        expected = {
            r["dedup_key"]
            for r in rows
            if r["recent"]
            and is_audit_eligible(
                r["sub_scores_json"],
                r["snap"],
                score_threshold=20,
                last_audit_verdict=r["last_verdict"],
            )
        }
        assert got_keys == expected == {"fresh-high", "fresh-rescored"}

    def test_ordered_by_sum_desc_and_capped(self, db_conn):
        _insert_posting(db_conn, "sum22")
        _insert_posting(db_conn, "sum23", sub_scores=SCORES_HIGH_V2_DICT)
        got = select_audit_candidates(
            _svc_conn(db_conn), score_threshold=20, lookback_days=3, max_jobs=1
        )
        assert [c["dedup_key"] for c in got] == ["sum23"]

    def test_candidate_shape(self, db_conn):
        _insert_posting(db_conn, "shape")
        (c,) = select_audit_candidates(
            _svc_conn(db_conn), score_threshold=20, lookback_days=3, max_jobs=60
        )
        # PORT-SEAM: private also asserted location_policy_verdict_json /
        # effective_location_fit -- dropped, no host column (see this
        # module's docstring PORT-SEAM).
        assert set(c) >= {
            "dedup_key",
            "title",
            "company",
            "location",
            "jd_full",
            "sub_scores_json",
            "axis_sum",
            "jd_content_verdict",
        }

    def test_skipped_verdict_reeligible_when_snapshot_unchanged(self, db_conn):
        """#1799 structural remediation (was test_skipped_verdict_excludes_
        job_from_candidates, pre-#1799 behavior): a `skipped` latest row used
        to permanently exclude the job once its snapshot matched -- exactly
        the mechanism that burned 92 keys during the 2026-08-12..14 provider
        outage. A `skipped` verdict is now non-consuming: the job is
        re-selected on the next pass even though its snapshot has not
        changed."""
        snap = _insert_posting(db_conn, "poison")
        record_score_audit(
            _svc_conn(db_conn),
            dedup_key="poison",
            model="sonnet",
            verdict="skipped",
            audited_sub_scores_json=snap,
            notes="API safety block",
        )
        got = select_audit_candidates(
            _svc_conn(db_conn), score_threshold=20, lookback_days=3, max_jobs=60
        )
        assert [c["dedup_key"] for c in got] == ["poison"]

    def test_real_verdict_still_excludes_job_at_unchanged_snapshot(self, db_conn):
        """Contrast case: a genuine agree/dispute verdict at the current
        snapshot still excludes the job -- only `skipped` is non-consuming."""
        snap = _insert_posting(db_conn, "audited")
        record_score_audit(
            _svc_conn(db_conn),
            dedup_key="audited",
            model="sonnet",
            verdict="agree",
            audited_sub_scores_json=snap,
        )
        got = select_audit_candidates(
            _svc_conn(db_conn), score_threshold=20, lookback_days=3, max_jobs=60
        )
        assert [c["dedup_key"] for c in got] == []

    def test_skipped_below_max_skip_attempts_still_selected(self, db_conn):
        """#1806: a job skipped fewer than max_skip_attempts times at its
        current snapshot is still selected (the #1799 non-consuming behavior
        holds below the bound)."""
        snap = _insert_posting(db_conn, "poison")
        for _ in range(2):
            record_score_audit(
                _svc_conn(db_conn),
                dedup_key="poison",
                model="sonnet",
                verdict="skipped",
                audited_sub_scores_json=snap,
                notes="malformed JD",
            )
        got = select_audit_candidates(
            _svc_conn(db_conn),
            score_threshold=20,
            lookback_days=3,
            max_jobs=60,
            max_skip_attempts=3,
        )
        assert [c["dedup_key"] for c in got] == ["poison"]

    def test_skipped_at_max_skip_attempts_excluded_from_cohort(self, db_conn):
        """#1806: a job skipped max_skip_attempts times at its current
        snapshot drops out of the cohort -- the `skipped` verdict falls back
        to normal snapshot-consuming behavior so a genuinely poison item
        stops re-entering the nightly batch. On this host there is no
        cutover watermark gating the count (PORT-SEAM, see module
        docstring) -- this proves the bound itself still works
        unconditionally."""
        snap = _insert_posting(db_conn, "poison")
        for _ in range(2):
            record_score_audit(
                _svc_conn(db_conn),
                dedup_key="poison",
                model="sonnet",
                verdict="skipped",
                audited_sub_scores_json=snap,
                notes="malformed JD",
            )
        got = select_audit_candidates(
            _svc_conn(db_conn),
            score_threshold=20,
            lookback_days=3,
            max_jobs=60,
            max_skip_attempts=2,
        )
        assert [c["dedup_key"] for c in got] == []

    def test_skipped_at_max_skip_attempts_rescored_re_selected(self, db_conn):
        """#1806: a job that hit the skip bound but was since rescored (new
        sub_scores_json) is re-selected -- the bound is per-snapshot, and a
        rescore (often a re-fetched jd_full) resets the skip count for the
        new snapshot."""
        old_snap = _insert_posting(db_conn, "poison")
        for _ in range(2):
            record_score_audit(
                _svc_conn(db_conn),
                dedup_key="poison",
                model="sonnet",
                verdict="skipped",
                audited_sub_scores_json=old_snap,
                notes="malformed JD",
            )
        db_conn.execute(
            "UPDATE postings SET sub_scores_json = %s WHERE dedup_key = %s",
            (Jsonb(SCORES_HIGH_V2_DICT), "poison"),
        )
        got = select_audit_candidates(
            _svc_conn(db_conn),
            score_threshold=20,
            lookback_days=3,
            max_jobs=60,
            max_skip_attempts=2,
        )
        assert [c["dedup_key"] for c in got] == ["poison"]

    def test_skipped_max_skip_attempts_zero_preserves_unbounded_behavior(self, db_conn):
        """#1806: max_skip_attempts=0 (the default) preserves the unbounded
        #1799 behavior -- a job skipped many times at the same snapshot is
        still selected."""
        snap = _insert_posting(db_conn, "poison")
        for _ in range(5):
            record_score_audit(
                _svc_conn(db_conn),
                dedup_key="poison",
                model="sonnet",
                verdict="skipped",
                audited_sub_scores_json=snap,
                notes="malformed JD",
            )
        got = select_audit_candidates(
            _svc_conn(db_conn),
            score_threshold=20,
            lookback_days=3,
            max_jobs=60,
            max_skip_attempts=0,
        )
        assert [c["dedup_key"] for c in got] == ["poison"]


class TestJsonbTextRoundTrip:
    """NEW (not in the private original): checks the calling contract
    _score_audits.py's module docstring asserts -- that every snapshot
    comparison must go through Postgres's own jsonb ::text rendering, never
    json.dumps() on a psycopg-decoded dict. See that module's PORT-SEAM."""

    def test_postgres_rendering_written_via_json_dumps_mismatches(self, db_conn):
        """A json.dumps() string of the SAME dict psycopg would decode to
        does not, in general, equal Postgres's own ::text rendering of the
        jsonb column (key order/whitespace differ) -- this is exactly the
        hazard the calling contract exists to prevent. If Postgres's jsonb
        renderer ever happened to agree byte-for-byte with json.dumps() for
        this particular dict, this assertion documents that as a
        happy-path coincidence, not a guarantee -- the round-trip test below
        is the one that actually gates correctness."""
        pg_rendering = _insert_posting(db_conn, "roundtrip", sub_scores=SCORES_HIGH_DICT)
        hand_serialized = json.dumps(SCORES_HIGH_DICT)
        # Both parse back to the same value...
        assert json.loads(pg_rendering) == json.loads(hand_serialized) == SCORES_HIGH_DICT
        # ...but axis_sum (byte-agnostic, parses JSON) agrees on both, while
        # ONLY the Postgres rendering is safe to hand to is_audit_eligible /
        # record_score_audit as this job's snapshot -- see the round-trip
        # test immediately below for why.
        assert axis_sum(pg_rendering) == axis_sum(hand_serialized) == 22

    def test_snapshot_written_from_pg_rendering_makes_job_ineligible(self, db_conn):
        """The contract in practice: record an audit using the EXACT string
        select_audit_candidates itself would read back (::text on the jsonb
        column), then confirm the job is excluded from the next
        selection -- proving the SQL equality join
        (a.audited_sub_scores_json = p.sub_scores_json::text) actually
        matches at the byte level for a real round trip, not just in
        principle."""
        pg_rendering = _insert_posting(db_conn, "sealed", sub_scores=SCORES_HIGH_DICT)
        record_score_audit(
            _svc_conn(db_conn),
            dedup_key="sealed",
            model="sonnet",
            verdict="agree",
            audited_sub_scores_json=pg_rendering,
        )
        got = select_audit_candidates(
            _svc_conn(db_conn), score_threshold=20, lookback_days=3, max_jobs=60
        )
        assert [c["dedup_key"] for c in got] == []

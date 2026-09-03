"""Tests for slug-ownership challenge wiring in the promotion write paths that
bypass ``ats_identity_reconcile._verify_and_write_promotion``: ats_prober's
speculative-ladder retry route, ats_scanner._probe's B2 careers_url fast-path
and its own speculative ladder, and company_dedup.heal_mispromoted_ats_slugs.

Before this fix all four wrote ats_platform/ats_slug/ats_probe_status='hit'
via bare UPDATEs guarded only by the m076 UNIQUE(ats_platform, ats_slug)
IntegrityError — a collision there produced no ats_slug_challenges bookkeeping
and no demotion pressure, so a poisoned owner blocked the rightful company
forever (see PR #1017, which fixed the same class of bug at the reconcile
chokepoint but left these four write sites unwired).
"""

import sqlite3
from datetime import UTC, datetime
from unittest.mock import patch

from jobcannon.engine.ats_prober import ProbeHttpResult

THRESHOLD_ONE = {"ats": {"identity_reconcile": {"challenge_demotion_threshold": 1}}}


def _insert_company(
    conn,
    name_raw,
    *,
    platform=None,
    slug=None,
    status="pending",
    careers_url=None,
) -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status,
                careers_url, scan_enabled, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (name_raw.lower(), name_raw, platform, slug, status, careers_url, now, now),
    )
    return cur.lastrowid


def _challenge_row(conn, owner_id):
    row = conn.execute(
        "SELECT * FROM ats_slug_challenges WHERE owner_company_id = ?", (owner_id,)
    ).fetchone()
    return dict(row) if row is not None else None


class _FailingConnection(sqlite3.Connection):
    """A sqlite3.Connection that raises IntegrityError on the Nth occurrence
    of a matching SQL statement — used to simulate a concurrent writer
    grabbing a (platform, slug) pair between a successful demotion and the
    challenger's retry UPDATE (the "unreachable by construction" defensive
    branches in ats_prober._promote_speculative_hit / ats_scanner._probe's
    collision handlers). Matches on SQL substring rather than call ordinal
    so the fault injection survives unrelated internal call-count changes.
    """

    _fail_sql_substring: str | None = None
    _fail_on_occurrence: int = 0
    _seen_count: int = 0

    def execute(self, sql, parameters=()):
        if self._fail_sql_substring and self._fail_sql_substring in sql:
            self._seen_count += 1
            if self._seen_count == self._fail_on_occurrence:
                raise sqlite3.IntegrityError("simulated concurrent writer")
        return super().execute(sql, parameters)


def _connect_failing(path, *, fail_sql_substring, fail_on_occurrence):
    conn = sqlite3.connect(path, factory=_FailingConnection)
    conn._fail_sql_substring = fail_sql_substring
    conn._fail_on_occurrence = fail_on_occurrence
    conn._seen_count = 0
    conn.row_factory = sqlite3.Row
    return conn


class _CountingConnection(sqlite3.Connection):
    """A sqlite3.Connection subclass that records every commit() call.

    Plain ``sqlite3.Connection`` instances have no settable ``__dict__`` for
    base-class instances, so a method can't be monkeypatched directly on one
    (``conn.commit = fn`` raises ``AttributeError: ... read-only``) — a real
    subclass is required to intercept ``commit()``.
    """

    commit_count: int = 0

    def commit(self):
        self.commit_count += 1
        return super().commit()


def _connect_counting(path):
    conn = sqlite3.connect(path, factory=_CountingConnection)
    conn.commit_count = 0
    conn.row_factory = sqlite3.Row
    return conn


class TestAtsProberSpeculativeChallenge:
    """probe_single_company's speculative branch (no ats_platform/ats_slug set)."""

    def _seed(self, migrated_db_path):
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            conn, "Software Technology", platform="lever", slug="freshworks", status="hit"
        )
        challenger_id = _insert_company(conn, "Freshworks", status="pending")
        conn.commit()
        conn.close()
        return owner_id, challenger_id

    def test_single_collision_does_not_evict_incumbent(self, migrated_db_path):
        from jobcannon.engine.ats_prober import probe_single_company

        owner_id, challenger_id = self._seed(migrated_db_path)
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row

        with (
            patch("jobcannon.engine.ats_prober.derive_slug_candidates", return_value=["freshworks"]),
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=True, status_code=200),
            ),
        ):
            result = probe_single_company(challenger_id, conn, {})

        assert result["status"] == "miss"
        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] == "lever"
        assert owner["ats_slug"] == "freshworks"  # untouched below threshold

        challenge = _challenge_row(conn, owner_id)
        assert challenge["challenge_count"] == 1
        assert challenge["owner_passed"] == 0
        assert challenge["challenger_passed"] == 1
        assert challenge["resolution"] is None
        conn.close()

    def test_repeated_collision_demotes_owner_and_promotes_challenger(self, migrated_db_path):
        from jobcannon.engine.ats_prober import probe_single_company

        owner_id, challenger_id = self._seed(migrated_db_path)
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row

        with (
            patch("jobcannon.engine.ats_prober.derive_slug_candidates", return_value=["freshworks"]),
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=True, status_code=200),
            ),
        ):
            for _ in range(2):
                result = probe_single_company(challenger_id, conn, {})
                assert result["status"] == "miss"
            result = probe_single_company(challenger_id, conn, {})

        assert result == {"status": "hit", "jobs_found": 0}

        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] is None
        assert owner["ats_slug"] is None
        assert owner["ats_probe_status"] == "pending"

        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_platform"] == "lever"
        assert challenger["ats_slug"] == "freshworks"
        assert challenger["ats_probe_status"] == "hit"

        challenge = _challenge_row(conn, owner_id)
        assert challenge["resolution"] == "owner_demoted"
        assert challenge["challenge_count"] == 3
        conn.close()


class TestProbeAtsSlugsFastpathChallenge:
    """ats_scanner._probe.probe_ats_slugs — B2 careers_url fast-path collisions."""

    def _seed(self, migrated_db_path):
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            conn, "Software Technology", platform="greenhouse", slug="freshworks", status="hit"
        )
        challenger_id = _insert_company(
            conn,
            "Freshworks",
            status="pending",
            careers_url="https://boards.greenhouse.io/freshworks",
        )
        conn.commit()
        conn.close()
        return owner_id, challenger_id

    def test_single_collision_records_challenge_and_stays_miss(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        owner_id, challenger_id = self._seed(migrated_db_path)

        with patch("jobcannon.engine.ats_scanner._probe._verify_fastpath_live", return_value=True):
            summary = probe_ats_slugs(migrated_db_path, config={})

        assert summary["misses"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_slug"] == "freshworks"  # untouched
        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_probe_status"] == "miss"
        assert challenger["miss_reason"] == "collision"

        challenge = _challenge_row(conn, owner_id)
        assert challenge is not None
        assert challenge["challenge_count"] == 1
        conn.close()

    def test_collision_demotes_owner_when_threshold_met(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        owner_id, challenger_id = self._seed(migrated_db_path)

        with patch("jobcannon.engine.ats_scanner._probe._verify_fastpath_live", return_value=True):
            summary = probe_ats_slugs(migrated_db_path, config=THRESHOLD_ONE)

        assert summary["hits"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] is None
        assert owner["ats_slug"] is None
        assert owner["ats_probe_status"] == "pending"

        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_probe_status"] == "hit"
        assert challenger["ats_platform"] == "greenhouse"
        assert challenger["ats_slug"] == "freshworks"
        assert (
            challenger["ats_evidence_trigger"]
            == "careers_url:https://boards.greenhouse.io/freshworks"
        )
        conn.close()


class TestProbeAtsSlugsSpeculativeLadderChallenge:
    """ats_scanner._probe.probe_ats_slugs — derive_slug_candidates speculative ladder."""

    def _seed(self, migrated_db_path):
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            conn, "Software Technology", platform="lever", slug="freshworks", status="hit"
        )
        challenger_id = _insert_company(conn, "Freshworks", status="pending")
        conn.commit()
        conn.close()
        return owner_id, challenger_id

    def test_single_collision_records_challenge_and_stays_miss(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        owner_id, challenger_id = self._seed(migrated_db_path)

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["freshworks"],
            ),
            patch("jobcannon.engine.ats_scanner._probe._PROBES", [("lever", lambda slug: True)]),
        ):
            summary = probe_ats_slugs(migrated_db_path, config={})

        assert summary["misses"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_slug"] == "freshworks"
        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_probe_status"] == "miss"
        assert challenger["miss_reason"] == "collision"
        challenge = _challenge_row(conn, owner_id)
        assert challenge is not None
        conn.close()

    def test_collision_demotes_owner_when_threshold_met(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        owner_id, challenger_id = self._seed(migrated_db_path)

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["freshworks"],
            ),
            patch("jobcannon.engine.ats_scanner._probe._PROBES", [("lever", lambda slug: True)]),
        ):
            summary = probe_ats_slugs(migrated_db_path, config=THRESHOLD_ONE)

        assert summary["hits"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] is None
        assert owner["ats_slug"] is None
        assert owner["ats_probe_status"] == "pending"

        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_probe_status"] == "hit"
        assert challenger["ats_platform"] == "lever"
        assert challenger["ats_slug"] == "freshworks"
        # Speculative promotions stay evidence-NULL — the invariant that lets
        # audits (and m064-style resets) tell them apart from URL/reconcile
        # evidence hits must survive routing through the challenge mechanism.
        assert challenger["ats_evidence_trigger"] is None
        conn.close()




class TestDemotionPromotionAtomicity:
    """The demotion UPDATE (clearing the incumbent's claim) and the
    challenger's promotion UPDATE must land in exactly one commit — a
    regression that split them into two commit() calls would reopen PR
    #1017's bug class (a crash between the two commits leaves the owner's
    slug cleared with nobody holding the pair). Reading state back via the
    SAME connection (as the tests above do) can't tell one commit apart from
    two, since a connection always sees its own uncommitted writes — these
    tests instead count real commit() calls on the connection.
    """

    def test_ats_prober_speculative_demotion_is_one_commit(self, migrated_db_path):
        from jobcannon.engine.ats_prober import probe_single_company

        conn = _connect_counting(migrated_db_path)
        owner_id = _insert_company(
            conn, "Software Technology", platform="lever", slug="freshworks", status="hit"
        )
        challenger_id = _insert_company(conn, "Freshworks", status="pending")
        conn.commit()
        conn.commit_count = 0

        with (
            patch("jobcannon.engine.ats_prober.derive_slug_candidates", return_value=["freshworks"]),
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=True, status_code=200),
            ),
        ):
            result = probe_single_company(challenger_id, conn, THRESHOLD_ONE)

        assert result == {"status": "hit", "jobs_found": 0}
        assert conn.commit_count == 1

        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] is None
        assert owner["ats_slug"] is None
        conn.close()

    def test_probe_ats_slugs_fastpath_demotion_is_one_commit(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        seed_conn = _connect_counting(migrated_db_path)
        owner_id = _insert_company(
            seed_conn,
            "Software Technology",
            platform="greenhouse",
            slug="freshworks",
            status="hit",
        )
        _insert_company(
            seed_conn,
            "Freshworks",
            status="pending",
            careers_url="https://boards.greenhouse.io/freshworks",
        )
        seed_conn.commit()
        seed_conn.close()

        real_conn = _connect_counting(migrated_db_path)
        with (
            patch("jobcannon.engine.ats_scanner._probe._verify_fastpath_live", return_value=True),
            patch("jobcannon.engine.ats_scanner._probe.standalone_connection") as mock_standalone,
        ):
            mock_standalone.return_value.__enter__.return_value = real_conn
            mock_standalone.return_value.__exit__.return_value = False
            summary = probe_ats_slugs(migrated_db_path, config=THRESHOLD_ONE)

        assert summary["hits"] == 1
        assert real_conn.commit_count == 1

        owner = dict(
            real_conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone()
        )
        assert owner["ats_platform"] is None
        assert owner["ats_slug"] is None
        real_conn.close()

    def test_probe_ats_slugs_speculative_ladder_demotion_is_one_commit(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        seed_conn = _connect_counting(migrated_db_path)
        owner_id = _insert_company(
            seed_conn, "Software Technology", platform="lever", slug="freshworks", status="hit"
        )
        _insert_company(seed_conn, "Freshworks", status="pending")
        seed_conn.commit()
        seed_conn.close()

        real_conn = _connect_counting(migrated_db_path)
        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["freshworks"],
            ),
            patch("jobcannon.engine.ats_scanner._probe._PROBES", [("lever", lambda slug: True)]),
            patch("jobcannon.engine.ats_scanner._probe.standalone_connection") as mock_standalone,
        ):
            mock_standalone.return_value.__enter__.return_value = real_conn
            mock_standalone.return_value.__exit__.return_value = False
            summary = probe_ats_slugs(migrated_db_path, config=THRESHOLD_ONE)

        assert summary["hits"] == 1
        assert real_conn.commit_count == 1

        owner = dict(
            real_conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone()
        )
        assert owner["ats_platform"] is None
        assert owner["ats_slug"] is None
        real_conn.close()




class TestNestedIntegrityErrorOnRetry:
    """The 'unreachable by construction' defensive branch: the retry UPDATE
    right after a successful demotion collides AGAIN (another writer raced
    in between). Both ats_prober._promote_speculative_hit and
    ats_scanner._probe's collision handlers roll back rather than committing
    a demotion with no matching promotion. Exercised here via a Connection
    subclass that injects the second collision deterministically — no
    threading or sqlite3-internals mocking required.
    """

    def test_ats_prober_rolls_back_demotion_when_retry_collides_again(self, migrated_db_path):
        from jobcannon.engine.ats_prober import _promote_speculative_hit

        seed_conn = sqlite3.connect(migrated_db_path)
        seed_conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            seed_conn, "Software Technology", platform="lever", slug="freshworks", status="hit"
        )
        challenger_id = _insert_company(seed_conn, "Freshworks", status="pending")
        seed_conn.commit()
        seed_conn.close()

        conn = _connect_failing(
            migrated_db_path,
            fail_sql_substring="ats_probe_status = 'hit'",
            fail_on_occurrence=2,  # 1st = the real collision, 2nd = the post-demotion retry
        )

        result = _promote_speculative_hit(
            conn, challenger_id, "Freshworks", "lever", "freshworks", THRESHOLD_ONE
        )

        assert result is False

        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] == "lever"
        assert owner["ats_slug"] == "freshworks"
        # The rollback discards the demotion AND the challenge bookkeeping
        # from the same uncommitted transaction — no orphaned audit row.
        assert _challenge_row(conn, owner_id) is None
        conn.close()


class TestPromoteSpeculativeHitResetsConsecutiveEmptyScans:
    """Issue #1044: the speculative-ladder promotion write
    (_promote_speculative_hit — one of the "probe hit" bypass writers this
    file's module docstring covers) resets consecutive_empty_scans to 0 on
    every successful promotion, the same as the evidence-based
    _verify_and_write_promotion chokepoint.
    """

    def test_successful_promotion_resets_counter(self, migrated_db_path):
        from jobcannon.engine.ats_prober import _promote_speculative_hit

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        challenger_id = _insert_company(conn, "Freshworks", status="pending")
        conn.execute(
            "UPDATE companies SET consecutive_empty_scans = 6 WHERE id = ?", (challenger_id,)
        )
        conn.commit()

        result = _promote_speculative_hit(
            conn, challenger_id, "Freshworks", "lever", "freshworks", THRESHOLD_ONE
        )

        assert result is True
        row = dict(
            conn.execute("SELECT * FROM companies WHERE id = ?", (challenger_id,)).fetchone()
        )
        assert row["ats_platform"] == "lever"
        assert row["ats_slug"] == "freshworks"
        assert row["consecutive_empty_scans"] == 0
        conn.close()


class TestBypassWriterProvisionalStamping:
    """The three bypass write sites now stamp ats_evidence_provisional the
    same way _verify_and_write_promotion does (see PR #1019's
    owner_identity_passes / ats_evidence_provisional and PR #1020's follow-up
    that closed this specific gap): a first-acquisition promotion whose own
    name has no affinity with the slug it just claimed is marked provisional
    so a FUTURE collision against it demotes after the shorter
    challenge_demotion_threshold_provisional instead of the standard one.
    """

    def test_ats_prober_speculative_affine_name_is_not_provisional(self, migrated_db_path):
        from jobcannon.engine.ats_prober import probe_single_company

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        challenger_id = _insert_company(conn, "Freshworks", status="pending")
        conn.commit()

        with (
            patch("jobcannon.engine.ats_prober.derive_slug_candidates", return_value=["freshworks"]),
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=True, status_code=200),
            ),
        ):
            result = probe_single_company(challenger_id, conn, {})

        assert result == {"status": "hit", "jobs_found": 0}
        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_evidence_provisional"] == 0
        conn.close()

    def test_ats_prober_speculative_non_affine_name_is_provisional(self, migrated_db_path):
        from jobcannon.engine.ats_prober import probe_single_company

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        challenger_id = _insert_company(conn, "Totally Unrelated Corp", status="pending")
        conn.commit()

        with (
            patch("jobcannon.engine.ats_prober.derive_slug_candidates", return_value=["xyzzy123"]),
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=True, status_code=200),
            ),
        ):
            result = probe_single_company(challenger_id, conn, {})

        assert result == {"status": "hit", "jobs_found": 0}
        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_evidence_provisional"] == 1
        conn.close()

    def test_fastpath_careers_url_evidence_is_never_provisional(self, migrated_db_path):
        """The B2 fast-path's trigger is owner-anchored (careers_url:), so
        owner_identity_passes short-circuits to True regardless of name
        affinity — proven here with a deliberately non-affine company name."""
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        challenger_id = _insert_company(
            conn,
            "Totally Unrelated Corp",
            status="pending",
            careers_url="https://boards.greenhouse.io/freshworks",
        )
        conn.commit()
        conn.close()

        with patch("jobcannon.engine.ats_scanner._probe._verify_fastpath_live", return_value=True):
            summary = probe_ats_slugs(migrated_db_path, config={})

        assert summary["hits"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_evidence_provisional"] == 0
        conn.close()

    def test_probe_ats_slugs_speculative_affine_name_is_not_provisional(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        challenger_id = _insert_company(conn, "Freshworks", status="pending")
        conn.commit()
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["freshworks"],
            ),
            patch("jobcannon.engine.ats_scanner._probe._PROBES", [("lever", lambda slug: True)]),
        ):
            summary = probe_ats_slugs(migrated_db_path, config={})

        assert summary["hits"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_evidence_provisional"] == 0
        conn.close()

    def test_probe_ats_slugs_speculative_non_affine_name_is_provisional(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        challenger_id = _insert_company(conn, "Totally Unrelated Corp", status="pending")
        conn.commit()
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["xyzzy123"],
            ),
            patch("jobcannon.engine.ats_scanner._probe._PROBES", [("lever", lambda slug: True)]),
        ):
            summary = probe_ats_slugs(migrated_db_path, config={})

        assert summary["hits"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_evidence_provisional"] == 1
        conn.close()

    def test_demoted_owner_provisional_flag_speeds_up_next_demotion(self, migrated_db_path):
        """A non-affine first acquisition is stamped provisional; a SUBSEQUENT
        name-affine challenger then only needs
        challenge_demotion_threshold_provisional (1) agreeing challenges to
        demote it, instead of the standard threshold (3) — proving the
        provisional flag written by the bypass writer actually shortens the
        fuse for a later collision, not just that the column gets set."""
        from jobcannon.engine.ats_prober import probe_single_company

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        phantom_id = _insert_company(conn, "Totally Unrelated Corp", status="pending")
        conn.commit()

        with (
            patch("jobcannon.engine.ats_prober.derive_slug_candidates", return_value=["freshworks"]),
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=True, status_code=200),
            ),
        ):
            result = probe_single_company(phantom_id, conn, {})
        assert result == {"status": "hit", "jobs_found": 0}
        phantom = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (phantom_id,)).fetchone()
        )
        assert phantom["ats_evidence_provisional"] == 1

        challenger_id = _insert_company(conn, "Freshworks", status="pending")
        conn.commit()

        # Standard threshold (3) is NOT met by a single challenge — but the
        # provisional owner should demote at threshold_provisional (1)
        # instead, on this first collision.
        with (
            patch("jobcannon.engine.ats_prober.derive_slug_candidates", return_value=["freshworks"]),
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=True, status_code=200),
            ),
        ):
            result = probe_single_company(challenger_id, conn, {})

        assert result == {"status": "hit", "jobs_found": 0}
        phantom = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (phantom_id,)).fetchone()
        )
        assert phantom["ats_platform"] is None
        assert phantom["ats_probe_status"] == "pending"
        challenger = dict(
            conn.execute("SELECT * FROM companies WHERE id=?", (challenger_id,)).fetchone()
        )
        assert challenger["ats_platform"] == "lever"
        assert challenger["ats_slug"] == "freshworks"

        challenge = _challenge_row(conn, phantom_id)
        assert challenge["resolution"] == "owner_demoted"
        assert challenge["challenge_count"] == 1
        conn.close()


class TestKillSwitchAndDenylistThreading:
    """The three new call sites each independently re-derive settings via
    identity_reconcile_settings(config) and thread config through to
    _challenger_passes. A wiring bug in any of them (config not threaded,
    typo'd key) would silently leave the kill switch or denylist inert at
    that specific site while the pre-existing reconcile-path tests
    (test_ats_slug_challenge.py) stay green."""

    KILL_SWITCH_OFF = {"ats": {"identity_reconcile": {"challenge_demotion_enabled": False}}}
    DENYLIST_FRESHWORKS = {"filters": {"company_denylist": ["Freshworks"]}}

    def test_ats_prober_speculative_kill_switch(self, migrated_db_path):
        from jobcannon.engine.ats_prober import probe_single_company

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            conn, "Software Technology", platform="lever", slug="freshworks", status="hit"
        )
        challenger_id = _insert_company(conn, "Freshworks", status="pending")
        conn.commit()

        with (
            patch("jobcannon.engine.ats_prober.derive_slug_candidates", return_value=["freshworks"]),
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=True, status_code=200),
            ),
        ):
            for _ in range(5):
                result = probe_single_company(challenger_id, conn, self.KILL_SWITCH_OFF)
                assert result["status"] == "miss"

        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] == "lever"
        assert owner["ats_slug"] == "freshworks"
        conn.close()

    def test_ats_prober_speculative_denylisted_challenger(self, migrated_db_path):
        from jobcannon.engine.ats_prober import probe_single_company

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            conn, "Software Technology", platform="lever", slug="freshworks", status="hit"
        )
        challenger_id = _insert_company(conn, "Freshworks", status="pending")
        conn.commit()

        with (
            patch("jobcannon.engine.ats_prober.derive_slug_candidates", return_value=["freshworks"]),
            patch(
                "jobcannon.engine.ats_prober._probe_lever_with_result",
                return_value=ProbeHttpResult(hit=True, status_code=200),
            ),
        ):
            for _ in range(5):
                result = probe_single_company(challenger_id, conn, self.DENYLIST_FRESHWORKS)
                assert result["status"] == "miss"

        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] == "lever"
        assert owner["ats_slug"] == "freshworks"
        conn.close()

    def test_probe_ats_slugs_fastpath_kill_switch(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            conn, "Software Technology", platform="greenhouse", slug="freshworks", status="hit"
        )
        _insert_company(
            conn,
            "Freshworks",
            status="pending",
            careers_url="https://boards.greenhouse.io/freshworks",
        )
        conn.commit()
        conn.close()

        with patch("jobcannon.engine.ats_scanner._probe._verify_fastpath_live", return_value=True):
            summary = probe_ats_slugs(migrated_db_path, config=self.KILL_SWITCH_OFF)

        assert summary["hits"] == 0
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] == "greenhouse"
        assert owner["ats_slug"] == "freshworks"
        conn.close()

    def test_probe_ats_slugs_fastpath_denylisted_challenger(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            conn, "Software Technology", platform="greenhouse", slug="freshworks", status="hit"
        )
        _insert_company(
            conn,
            "Freshworks",
            status="pending",
            careers_url="https://boards.greenhouse.io/freshworks",
        )
        conn.commit()
        conn.close()

        with patch("jobcannon.engine.ats_scanner._probe._verify_fastpath_live", return_value=True):
            summary = probe_ats_slugs(migrated_db_path, config=self.DENYLIST_FRESHWORKS)

        assert summary["hits"] == 0
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] == "greenhouse"
        assert owner["ats_slug"] == "freshworks"
        conn.close()

    def test_probe_ats_slugs_speculative_ladder_kill_switch(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            conn, "Software Technology", platform="lever", slug="freshworks", status="hit"
        )
        _insert_company(conn, "Freshworks", status="pending")
        conn.commit()
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["freshworks"],
            ),
            patch("jobcannon.engine.ats_scanner._probe._PROBES", [("lever", lambda slug: True)]),
        ):
            summary = probe_ats_slugs(migrated_db_path, config=self.KILL_SWITCH_OFF)

        assert summary["hits"] == 0
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] == "lever"
        assert owner["ats_slug"] == "freshworks"
        conn.close()

    def test_probe_ats_slugs_speculative_ladder_denylisted_challenger(self, migrated_db_path):
        from jobcannon.engine.ats_scanner._probe import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner_id = _insert_company(
            conn, "Software Technology", platform="lever", slug="freshworks", status="hit"
        )
        _insert_company(conn, "Freshworks", status="pending")
        conn.commit()
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe.derive_slug_candidates",
                return_value=["freshworks"],
            ),
            patch("jobcannon.engine.ats_scanner._probe._PROBES", [("lever", lambda slug: True)]),
        ):
            summary = probe_ats_slugs(migrated_db_path, config=self.DENYLIST_FRESHWORKS)

        assert summary["hits"] == 0
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        owner = dict(conn.execute("SELECT * FROM companies WHERE id=?", (owner_id,)).fetchone())
        assert owner["ats_platform"] == "lever"
        assert owner["ats_slug"] == "freshworks"
        conn.close()

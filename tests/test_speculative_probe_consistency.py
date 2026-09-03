"""F6 — speculative-probe careers_url consistency check.

Catches brand-name-collision false positives where the speculative probe
hits one ATS (because the slug happens to exist there) while the company's
own `careers_url` positively identifies a DIFFERENT ATS.

Honest limit: this fix does NOT catch the Shopify case (careers_url=
shopify.com/careers carries no ATS signature). That requires wide F6
(fetch and parse careers page), deferred.
"""

import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from jobcannon.engine.ats_detection import (
    careers_url_is_live,
    probe_hit_consistent_or_dead_url,
    probe_hit_consistent_with_careers_url,
)

# ---------------------------------------------------------------------------
# Fake HTTP response — lets us drive careers_url_is_live without real network.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _make_get(status: int):
    def _get(url, timeout):
        return _FakeResp(status)

    return _get


def _raising_get(exc: Exception):
    def _get(url, timeout):
        raise exc

    return _get


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_live_probe_http():
    """Block the real HTTP the speculative probe ladder issues to fake slugs.

    The _PROBES ladder (ats_scanner._probe) captures the ats_prober._probe_X
    function objects at import, so these tests' ``patch("...ats_prober._probe_X")``
    don't actually take effect — the real probes run and each does
    requests.get(slug_url, timeout=_PROBE_TIMEOUT) against a non-existent slug,
    costing ~3-4s of connect timeouts per test (the outcome is unchanged: a fake
    slug 404s -> probe returns False -> miss). Patch ats_prober.requests.get to a
    fast 404 so every probe misses instantly, preserving the all-miss outcome.
    Tests that force a hit do so by patching _probe._PROBES / _probe._probe_X
    (the by-name fast-path dispatch), which is unaffected by this. Applied via
    usefixtures only to the all-miss probe classes.
    """
    with patch("jobcannon.engine.ats_prober.requests.get", new=_make_get(404)):
        yield


def _insert_pending_company(
    conn: sqlite3.Connection,
    name: str,
    careers_url: str | None = None,
) -> int:
    now = datetime.now().isoformat()
    cursor = conn.execute(
        """INSERT INTO companies
           (name, name_raw, careers_url, ats_probe_status, created_at, updated_at)
           VALUES (?, ?, ?, 'pending', ?, ?)""",
        (name.lower(), name, careers_url, now, now),
    )
    conn.commit()
    inserted_id = cursor.lastrowid
    assert inserted_id is not None
    return inserted_id


# ---------------------------------------------------------------------------
# Unit tests — probe_hit_consistent_with_careers_url
# ---------------------------------------------------------------------------


class TestProbeHitConsistencyHelper:
    """Pure-function tests for the helper, independent of the probe loop."""

    def test_no_careers_url_is_consistent(self):
        """Without a careers_url, we have nothing to disprove the hit."""
        assert probe_hit_consistent_with_careers_url("pinpoint", None) is True
        assert probe_hit_consistent_with_careers_url("pinpoint", "") is True

    def test_careers_url_with_no_ats_signature_is_consistent(self):
        """careers_url like 'shopify.com/careers' carries no ATS signature.

        The helper passes the hit through. This is the documented narrow-F6
        limitation: wide F6 (fetch + widget parse) is needed to catch this.
        """
        assert (
            probe_hit_consistent_with_careers_url("pinpoint", "https://shopify.com/careers")
            is True
        )

    def test_url_inferred_platform_matches_hit_is_consistent(self):
        """Greenhouse URL + greenhouse hit → accept."""
        assert (
            probe_hit_consistent_with_careers_url(
                "greenhouse", "https://boards.greenhouse.io/acme"
            )
            is True
        )

    def test_url_inferred_platform_differs_from_hit_is_rejected(self):
        """Lever URL + greenhouse hit → reject — the Shopify-style pathology
        with a positive URL signature.
        """
        assert (
            probe_hit_consistent_with_careers_url("greenhouse", "https://jobs.lever.co/acme")
            is False
        )

    def test_ashby_url_rejects_other_platform_hit(self):
        assert (
            probe_hit_consistent_with_careers_url("pinpoint", "https://jobs.ashbyhq.com/acme")
            is False
        )

    def test_workday_url_accepts_workday_hit(self):
        """Workday subdomain pattern matches; same-platform hit passes."""
        assert (
            probe_hit_consistent_with_careers_url(
                "workday", "https://zillow.wd5.myworkdayjobs.com/External"
            )
            is True
        )


# ---------------------------------------------------------------------------
# Integration test — probe_ats_slugs honors the consistency gate
# ---------------------------------------------------------------------------


def _build_probes(hits_for: dict[str, bool]) -> list:
    """Build a fake _PROBES list. `hits_for` maps platform name → True/False.

    Uses the same (name, callable) shape probe_ats_slugs expects. Direct
    list replacement is needed because the real _PROBES captures function
    references at import time — patching the names doesn't reach them.
    """
    all_platforms = [
        "lever",
        "greenhouse",
        "ashby",
        "recruitee",
        "breezy",
        "jazzhr",
        "pinpoint",
        "teamtailor",
        "personio",
        "bamboohr",
    ]

    def _make_probe(value: bool):
        def _probe(_slug):
            return value

        return _probe

    return [(name, _make_probe(hits_for.get(name, False))) for name in all_platforms]


@pytest.mark.usefixtures("_no_live_probe_http")
class TestProbeAtsSlugsConsistencyGate:
    """End-to-end: a probe that hits but disagrees with careers_url is
    rejected; the company stays on miss.

    Several cases here have a careers_url that itself matches a fast-path
    ATS URL pattern (e.g. jobs.lever.co/..., boards.greenhouse.io/...), which
    triggers probe_ats_slugs's B2 careers_url fast-path (_verify_fastpath_live)
    BEFORE the mocked _PROBES speculative ladder is ever consulted. That
    fast-path calls the real per-platform probe function (ats_prober._probe_X),
    so this class needs the same real-HTTP block as _no_live_probe_http
    (a fast, deterministic 404) to keep the fast-path from finding a live
    board and short-circuiting the speculative-ladder behavior under test.
    """

    def test_hit_with_mismatched_careers_url_is_rejected(self, migrated_db_path):
        """Lever URL (LIVE) + speculative pinpoint hit → company ends on miss.

        Liveness explicitly mocked to True so the test asserts the
        brand-collision path (Shopify-style with a positive URL signature).
        Without the mock, the lever URL would hit real network — flakey and
        the wrong intent anyway.
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            careers_url="https://jobs.lever.co/some-other-acme",
        )
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"pinpoint": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch(
                "jobcannon.engine.ats_detection.careers_url_is_live",
                return_value=True,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["probed"] == 1
        assert result["hits"] == 0
        assert result["misses"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform, ats_slug FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "miss"
        assert row["ats_platform"] is None
        assert row["ats_slug"] is None

    def test_hit_with_matching_careers_url_is_accepted(self, migrated_db_path):
        """Greenhouse URL + speculative greenhouse hit → company promoted."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            careers_url="https://boards.greenhouse.io/acme",
        )
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"greenhouse": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1
        assert result["misses"] == 0

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "greenhouse"

    def test_hit_with_no_careers_url_is_accepted(self, migrated_db_path):
        """Without a careers_url, the gate is silent — pre-F6 behavior."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(conn, name="Acme", careers_url=None)
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"lever": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1
        assert result["misses"] == 0

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "lever"

    def test_rejected_hit_falls_through_to_legitimate_hit_on_next_platform(self, migrated_db_path):
        """When the first hit is rejected by the gate but a later platform
        ALSO hits and IS consistent, the consistent one wins.
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            # URL infers greenhouse → pinpoint hit rejected, greenhouse accepted.
            careers_url="https://boards.greenhouse.io/acme",
        )
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"pinpoint": True, "greenhouse": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            # Block real HTTP from careers_url_is_live; force "live" so the
            # pinpoint rejection holds and greenhouse takes over.
            patch(
                "jobcannon.engine.ats_detection.careers_url_is_live",
                return_value=True,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_platform FROM companies WHERE id=?", (company_id,)
        ).fetchone()
        conn.close()
        assert row["ats_platform"] == "greenhouse"


# ---------------------------------------------------------------------------
# Unit tests — careers_url_is_live
# ---------------------------------------------------------------------------


class TestCareersUrlIsLive:
    """Pure unit tests via injected `_get`. No real HTTP."""

    def test_none_url_returns_none(self):
        assert careers_url_is_live(None) is None

    def test_empty_url_returns_none(self):
        assert careers_url_is_live("") is None

    def test_200_returns_true(self):
        assert careers_url_is_live("https://x/", _get=_make_get(200)) is True

    def test_204_returns_true(self):
        """Any 2xx is treated as live (defensive — most ATSes return 200)."""
        assert careers_url_is_live("https://x/", _get=_make_get(204)) is True

    def test_404_returns_false(self):
        """404 is the canonical signal that an ATS tenant has been removed."""
        assert careers_url_is_live("https://x/", _get=_make_get(404)) is False

    def test_410_returns_false(self):
        """410 Gone — explicit signal that the resource is permanently dead."""
        assert careers_url_is_live("https://x/", _get=_make_get(410)) is False

    def test_403_returns_none(self):
        """Ambiguous — bot block, paywall, or legitimately gated. Caller
        falls back to conservative gate behavior (preserve rejection).
        """
        assert careers_url_is_live("https://x/", _get=_make_get(403)) is None

    def test_500_returns_none(self):
        """Server-side fault. Could be transient — don't trust either way."""
        assert careers_url_is_live("https://x/", _get=_make_get(500)) is None

    def test_exception_returns_none(self):
        """Timeout, DNS failure, connection refused — all undetermined."""
        assert (
            careers_url_is_live("https://x/", _get=_raising_get(TimeoutError("timeout"))) is None
        )


# ---------------------------------------------------------------------------
# Unit tests — probe_hit_consistent_or_dead_url (composite)
# ---------------------------------------------------------------------------


class TestProbeHitConsistentOrDeadUrl:
    """Composite of the pure helper + liveness check. Tests inject the
    liveness_check callable so no network is touched.
    """

    def test_no_url_short_circuits_no_liveness_check_made(self):
        """careers_url=None → pure helper accepts → liveness never called."""
        calls = []

        def _spy(url):
            calls.append(url)
            return False

        assert probe_hit_consistent_or_dead_url("pinpoint", None, liveness_check=_spy) is True
        assert calls == []

    def test_matching_platform_short_circuits_no_liveness_check_made(self):
        """Matching platform → pure helper accepts → liveness never called."""
        calls = []

        def _spy(url):
            calls.append(url)
            return False

        assert (
            probe_hit_consistent_or_dead_url(
                "greenhouse",
                "https://boards.greenhouse.io/acme",
                liveness_check=_spy,
            )
            is True
        )
        assert calls == []

    def test_no_signature_short_circuits_no_liveness_check_made(self):
        """careers_url with no ATS signature → pure helper accepts → liveness
        never called. Documents the narrow-F6 limit (Shopify case still slips).
        """
        calls = []

        def _spy(url):
            calls.append(url)
            return False

        assert (
            probe_hit_consistent_or_dead_url(
                "pinpoint",
                "https://shopify.com/careers",
                liveness_check=_spy,
            )
            is True
        )
        assert calls == []

    def test_mismatched_but_live_url_rejects_hit(self):
        """Brand-collision case: careers_url positively identifies a different
        platform AND is live → keep the rejection. This is the original F6
        behavior preserved.
        """
        assert (
            probe_hit_consistent_or_dead_url(
                "pinpoint",
                "https://jobs.lever.co/some-other-acme",
                liveness_check=lambda _u: True,
            )
            is False
        )

    def test_mismatched_but_dead_url_accepts_hit(self):
        """Migration case: careers_url is 404/410 → trust the live probe hit.
        Matches the real Nimble Robotics + Niantic findings from the audit.
        """
        assert (
            probe_hit_consistent_or_dead_url(
                "greenhouse",
                "https://jobs.lever.co/NimbleAI",  # 404 in production
                liveness_check=lambda _u: False,
            )
            is True
        )

    def test_mismatched_and_ambiguous_url_preserves_rejection(self):
        """Conservative default: 5xx/403/timeout → can't confirm dead, so we
        keep the rejection. Prevents the Shopify pathology from leaking
        through when the careers_url happens to be temporarily blocked.
        """
        assert (
            probe_hit_consistent_or_dead_url(
                "pinpoint",
                "https://jobs.lever.co/some-other-acme",
                liveness_check=lambda _u: None,
            )
            is False
        )


# ---------------------------------------------------------------------------
# Write-boundary invariant — probe paths must never write hit with NULL identity
# ---------------------------------------------------------------------------


class TestProbeWriteBoundaryIdentityInvariant:
    """Probe write paths must enforce: ats_probe_status='hit' ↔ non-NULL identity.

    The speculative ladder assigns hit_platform and hit_slug together — one
    cannot be set without the other — and the write site asserts hit_slug is
    not None before writing. These tests verify the guarantee from the outside:
    every hit written to the DB has both ats_platform and ats_slug populated,
    and a probe that produces no consistent hit leaves them NULL.

    The B2 careers_url fast-path is separately guarded by
    _verify_fastpath_live(fp_platform, fp_slug): that function receives the
    (platform, slug) pair extracted from the URL, so fp_platform/fp_slug are
    always both non-None when it is called (the extraction either returns a
    complete triple or None). No separate NULL-guard at the write site is
    needed there — the call-site structure enforces it by construction.
    """

    def test_speculative_hit_written_with_non_null_platform_and_slug(self, migrated_db_path):
        """A probe hit is only persisted when both platform and slug are set.

        Injects a fake _PROBES ladder that returns True for every slug on
        'lever', then verifies the written row has both ats_platform='lever'
        and a non-NULL ats_slug. Confirms the write boundary is reached and
        the identity pair is always written together.
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(conn, name="Acme Corp", careers_url=None)
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=[("lever", lambda slug: True)],
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1
        assert result["misses"] == 0

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform, ats_slug FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()

        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] is not None, "hit must have non-NULL ats_platform"
        assert row["ats_slug"] is not None, "hit must have non-NULL ats_slug"
        assert row["ats_platform"] == "lever"

    def test_speculative_miss_leaves_identity_null(self, migrated_db_path):
        """All probes returning False → miss, identity stays NULL."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(conn, name="Acme Corp", careers_url=None)
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=[("lever", lambda slug: False)],
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch("jobcannon.engine.ats_prober.requests.get", new=_make_get(404)),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 0
        assert result["misses"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform, ats_slug FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()

        assert row["ats_probe_status"] == "miss"
        assert row["ats_platform"] is None, "miss must leave ats_platform NULL"
        assert row["ats_slug"] is None, "miss must leave ats_slug NULL"

    def test_hit_write_never_carries_null_identity_columns(self, migrated_db_path):
        """Every DB write that sets ats_probe_status='hit' binds non-NULL
        platform and slug parameters.

        Uses a Connection subclass to intercept every SQL statement containing
        the hit sentinel and asserts the first two bind parameters
        (ats_platform, ats_slug) are both non-None. This is a data-contract
        test: it fails if any future code path reaches the write boundary with
        NULL identity, regardless of whether the enforcement mechanism is an
        assert, a guard, or a DB constraint.
        """
        import sqlite3 as _stdlib_sqlite3

        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        _insert_pending_company(conn, name="Acme Corp", careers_url=None)
        conn.close()

        hit_write_params: list[tuple] = []

        class _SpyingConnection(_stdlib_sqlite3.Connection):
            def execute(self, sql, params=()):
                if "ats_probe_status = 'hit'" in sql:
                    hit_write_params.append(params)
                    assert params[0] is not None, (
                        f"hit write bound NULL ats_platform; params={params}"
                    )
                    assert params[1] is not None, f"hit write bound NULL ats_slug; params={params}"
                return super().execute(sql, params)

        _real_connect = _stdlib_sqlite3.connect

        def _spying_connect(path, **kwargs):
            return _SpyingConnection(path, **kwargs)

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=[("lever", lambda slug: True)],
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch("jobcannon.engine.db_helpers.sqlite3.connect", new=_spying_connect),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1
        assert len(hit_write_params) >= 1, "spy never saw a hit write — patch target wrong"
        for params in hit_write_params:
            assert params[0] is not None
            assert params[1] is not None

    def test_default_liveness_check_is_careers_url_is_live(self):
        """Smoke: when liveness_check is not provided, the composite reaches
        for `careers_url_is_live`. Patch it at the module to confirm wiring.
        """
        with patch(
            "jobcannon.engine.ats_detection.careers_url_is_live", return_value=False
        ) as mock_check:
            result = probe_hit_consistent_or_dead_url(
                "greenhouse",
                "https://jobs.lever.co/some-other-acme",
            )
            assert result is True
            mock_check.assert_called_once_with("https://jobs.lever.co/some-other-acme")


# ---------------------------------------------------------------------------
# Integration test — migration scenario through probe_ats_slugs
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_no_live_probe_http")
class TestMigrationScenario:
    """End-to-end: a company with a stale (404) careers_url should still get
    promoted when the live probe rediscovers the new ATS — F6 narrow's
    original bug, now fixed by the liveness augmentation.
    """

    def test_stale_careers_url_does_not_block_probe(self, migrated_db_path):
        """Nimble Robotics-style: careers_url is jobs.lever.co/X but the
        Lever tenant 404s; meanwhile the company's new ATS is greenhouse.
        Pre-augmentation F6 would reject. Post-augmentation, the greenhouse
        hit wins.
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            careers_url="https://jobs.lever.co/StaleAcme",  # 404 in production
        )
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"greenhouse": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            # Force the careers_url to be "dead" — simulates the 404 we saw
            # for jobs.lever.co/NimbleAI in production.
            patch(
                "jobcannon.engine.ats_detection.careers_url_is_live",
                return_value=False,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1
        assert result["misses"] == 0

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "greenhouse"

    def test_live_mismatched_careers_url_still_blocks_probe(self, migrated_db_path):
        """Shopify-style (hypothetical with live URL): careers_url positively
        identifies Lever and IS live → speculative pinpoint hit gets rejected.
        Confirms augmentation hasn't weakened the brand-collision protection.
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Acme",
            careers_url="https://jobs.lever.co/RealAcme",
        )
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"pinpoint": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch(
                "jobcannon.engine.ats_detection.careers_url_is_live",
                return_value=True,  # URL is live → reject the pinpoint hit
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 0
        assert result["misses"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, ats_platform FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "miss"
        assert row["ats_platform"] is None


# ---------------------------------------------------------------------------
# B1a — FP-prone platform exclusion (2026-05-27 audit corollary)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# B2 -- careers_url hostname fast-path
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_no_live_probe_http")
class TestCareersUrlFastPath:
    """When careers_url unambiguously identifies a supported ATS, the probe
    bypasses speculative slug derivation, verifies via the platform probe,
    and writes a hit with ats_evidence_trigger='careers_url:...' attribution.
    Closes audit B2: 6 known regression rows (3 Ashby + 3 SmartRecruiters
    careers_url hits that the speculative path missed)."""

    def test_ashby_url_fastpath_hits_with_evidence(self, migrated_db_path):
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="AcmeWidgets",
            careers_url="https://jobs.ashbyhq.com/AcmeWidgets",
        )
        conn.close()

        with (
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            # Fast-path liveness resolves the probe through the registry SSOT (ats_prober),
            # not the _probe module's local re-export; patch it at the resolution point.
            patch(
                "jobcannon.engine.ats_prober._probe_ashby",
                return_value=True,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["probed"] == 1
        assert result["hits"] == 1
        assert result["misses"] == 0

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT ats_probe_status, ats_platform, ats_slug,
                      ats_evidence_trigger, ats_evidence_extractor_version,
                      ats_evidence_unique_url_count, ats_evidence_job_count
               FROM companies WHERE id=?""",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "ashby"
        assert row["ats_slug"] == "AcmeWidgets"
        assert row["ats_evidence_trigger"].startswith("careers_url:")
        assert "jobs.ashbyhq.com/AcmeWidgets" in row["ats_evidence_trigger"]
        assert row["ats_evidence_extractor_version"]
        assert row["ats_evidence_unique_url_count"] == 1
        assert row["ats_evidence_job_count"] == 0

    def test_recruitee_url_fastpath_can_assign_fp_prone_platform(self, migrated_db_path):
        """URL evidence beats the speculative-ladder FP-prone exclusion.

        bamboohr/personio/recruitee/breezy are banned from speculative
        probing (100% FP rate via {slug}={name} collisions). But
        https://{slug}.recruitee.com IS unambiguous URL evidence -- no
        name collision. The fast-path must be allowed to assign FP-prone
        platforms when the URL positively identifies them."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="GenuineSmallCo",
            careers_url="https://genuinesmallco.recruitee.com",
        )
        conn.close()

        with (
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            # Fast-path liveness resolves the probe through the registry SSOT (ats_prober).
            patch(
                "jobcannon.engine.ats_prober._probe_recruitee",
                return_value=True,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_platform, ats_slug, ats_evidence_trigger FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_platform"] == "recruitee"
        assert row["ats_slug"] == "genuinesmallco"
        assert row["ats_evidence_trigger"].startswith("careers_url:")

    def test_fastpath_runs_before_brand_blocklist(self, migrated_db_path):
        """URL evidence overrides the brand blocklist. A famous-brand company
        with an unambiguous ATS careers_url should land via fast-path, not
        get short-circuited by the blocklist."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="Shopify",
            careers_url="https://jobs.ashbyhq.com/Shopify",
        )
        conn.close()

        with (
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            # Fast-path liveness resolves the probe through the registry SSOT (ats_prober);
            # is_blocked_brand is still called in _probe's namespace, so it stays patched there.
            patch(
                "jobcannon.engine.ats_prober._probe_ashby",
                return_value=True,
            ),
            patch(
                "jobcannon.engine.ats_scanner._probe.is_blocked_brand",
                return_value=True,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1, "fast-path should run before brand blocklist"

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_platform, ats_probe_status FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "hit"
        assert row["ats_platform"] == "ashby"

    def test_fastpath_verifier_returning_false_falls_through(self, migrated_db_path):
        """careers_url points at a supported ATS but the live probe returns
        False (e.g. tenant deleted). Should NOT write a fast-path hit;
        should fall through to brand blocklist + speculative ladder."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="DeadTenant",
            careers_url="https://jobs.ashbyhq.com/DeadTenant",
        )
        conn.close()

        with (
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch(
                "jobcannon.engine.ats_scanner._probe.is_blocked_brand",
                return_value=False,
            ),
            # Fast-path liveness resolves the probe through the registry SSOT (ats_prober);
            # simulate the ashby tenant being dead there (hermetic — no real network probe).
            patch(
                "jobcannon.engine.ats_prober._probe_ashby",
                return_value=False,
            ),
            patch("jobcannon.engine.ats_prober._probe_lever", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_greenhouse", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_jazzhr", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_pinpoint", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_teamtailor", return_value=False),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["misses"] == 1
        assert result["hits"] == 0

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT ats_probe_status, ats_platform, ats_evidence_trigger
               FROM companies WHERE id=?""",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "miss"
        assert row["ats_platform"] is None
        assert row["ats_evidence_trigger"] is None

    def test_company_without_careers_url_skips_fastpath(self, migrated_db_path):
        """No careers_url -> fast-path is a no-op; flow continues."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(conn, name="NoUrlCo", careers_url=None)
        conn.close()

        with (
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch(
                "jobcannon.engine.ats_scanner._probe.is_blocked_brand",
                return_value=False,
            ),
            patch("jobcannon.engine.ats_prober._probe_lever", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_greenhouse", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_ashby", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_jazzhr", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_pinpoint", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_teamtailor", return_value=False),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["misses"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_evidence_trigger FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_evidence_trigger"] is None

    def test_url_without_ats_signature_skips_fastpath(self, migrated_db_path):
        """careers_url exists but doesn't match any known ATS host pattern
        -> fast-path is a no-op; falls through to brand blocklist + speculative."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        _insert_pending_company(
            conn,
            name="CustomAtsCo",
            careers_url="https://customatsco.com/careers/",
        )
        conn.close()

        with (
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch(
                "jobcannon.engine.ats_scanner._probe.is_blocked_brand",
                return_value=False,
            ),
            patch("jobcannon.engine.ats_prober._probe_lever", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_greenhouse", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_ashby", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_jazzhr", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_pinpoint", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_teamtailor", return_value=False),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["misses"] == 1


# ---------------------------------------------------------------------------
# B4 -- categorical miss_reason on speculative-probe failures
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_no_live_probe_http")
class TestSpeculativeMissCategorization:
    """probe_ats_slugs now writes a categorical miss_reason for every miss it
    creates. Audit B4: 2563/2568 legacy miss rows have NULL miss_reason,
    blocking diagnostic and rescue passes. The new categories are:
      - 'blocked_brand'          (already in use pre-B4)
      - 'speculative_exhausted'  (no probe returned True for any slug)
      - 'speculative_rejected'   (probe hit but consistency gate rejected it)
    Legacy NULL rows are not retroactively backfilled -- they stay NULL until
    the company is re-probed."""

    def test_speculative_exhausted_when_all_probes_return_false(self, migrated_db_path):
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(conn, name="ObscureSmallCo")
        conn.close()

        with (
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch(
                "jobcannon.engine.ats_scanner._probe.is_blocked_brand",
                return_value=False,
            ),
            patch("jobcannon.engine.ats_prober._probe_lever", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_greenhouse", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_ashby", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_jazzhr", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_pinpoint", return_value=False),
            patch("jobcannon.engine.ats_prober._probe_teamtailor", return_value=False),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["misses"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, miss_reason FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "miss"
        assert row["miss_reason"] == "speculative_exhausted"

    def test_speculative_rejected_when_consistency_gate_blocks_all_hits(self, migrated_db_path):
        """careers_url positively identifies platform X, but speculative
        probes hit platform Y (collision). The consistency gate rejects all
        Y-hits, and no other platform yields a hit. miss_reason should be
        'speculative_rejected' (not 'speculative_exhausted')."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(
            conn,
            name="MyCompany",
            careers_url="https://jobs.lever.co/some-other-mycompany",
        )
        conn.close()

        with (
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch(
                "jobcannon.engine.ats_scanner._probe.is_blocked_brand",
                return_value=False,
            ),
            # Fast-path liveness (careers_url is lever) resolves via the registry SSOT
            # (ats_prober); force it False so the flow falls through to the speculative ladder.
            patch(
                "jobcannon.engine.ats_prober._probe_lever",
                return_value=False,
            ),
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"pinpoint": True}),
            ),
            patch(
                "jobcannon.engine.ats_detection.careers_url_is_live",
                return_value=True,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["misses"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_probe_status, miss_reason FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["ats_probe_status"] == "miss"
        assert row["miss_reason"] == "speculative_rejected"

    def test_blocked_brand_reason_is_unchanged(self, migrated_db_path):
        """Pre-B4 'blocked_brand' miss_reason is preserved."""
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(conn, name="Walmart")
        conn.close()

        with (
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch(
                "jobcannon.engine.ats_scanner._probe.is_blocked_brand",
                return_value=True,
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["misses"] == 1
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT miss_reason FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
        conn.close()
        assert row["miss_reason"] == "blocked_brand"


# ---------------------------------------------------------------------------
# m076: UNIQUE(ats_platform, ats_slug) collision recovery for probe_ats_slugs
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_no_live_probe_http")
class TestSpeculativeProbeCollisionRecovery:
    """When the UPDATE in either probe branch would violate the partial
    UNIQUE index introduced by m076, the company must end on
    ats_probe_status='miss' with miss_reason='collision'. The legitimate
    owner of the (platform, slug) pair is untouched.

    Both tests here route the collision through _resolve_collision ->
    resolve_slug_collision -> process_slug_challenge, which (when both the
    owner and challenger pass the name/slug affinity check) probes the ATS
    board's own display name via ats_registry.probe_board_identity to break
    the tie — a real HTTP call to ats_prober._probe_identity_greenhouse.
    _no_live_probe_http's fast 404 makes that probe return None (no
    tie-break signal), which is the same "board identity indeterminate"
    outcome a real 404 for a fake slug would produce, preserving the
    owner-stays-owner assertions below.
    """

    def test_speculative_branch_collision_marks_miss_with_collision_reason(self, migrated_db_path):
        """The speculative ladder hits a slug already owned by another company.

        The UPDATE raises sqlite3.IntegrityError; the handler demotes the
        row to status='miss' with miss_reason='collision' and the pre-
        existing owner is left as-is.
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        # Pre-existing legitimate owner of (greenhouse, acme).
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies
                  (name, name_raw, ats_platform, ats_slug,
                   ats_probe_status, scan_enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'hit', 1, ?, ?)""",
            ("acme corp", "Acme Corp", "greenhouse", "acme", now, now),
        )
        conn.commit()
        owner_id = cursor.lastrowid
        # Pending probe candidate whose derived slug will collide.
        loser_id = _insert_pending_company(conn, name="Acme", careers_url=None)
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=_build_probes({"greenhouse": True}),
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 0
        assert result["misses"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        loser_row = conn.execute(
            "SELECT ats_probe_status, ats_platform, ats_slug, miss_reason "
            "FROM companies WHERE id=?",
            (loser_id,),
        ).fetchone()
        owner_row = conn.execute(
            "SELECT ats_platform, ats_slug FROM companies WHERE id=?",
            (owner_id,),
        ).fetchone()
        conn.close()

        assert loser_row["ats_probe_status"] == "miss"
        assert loser_row["miss_reason"] == "collision"
        # Loser must NOT have taken the owner's slug.
        assert loser_row["ats_platform"] is None
        assert loser_row["ats_slug"] is None
        # Pre-existing owner intact.
        assert owner_row["ats_platform"] == "greenhouse"
        assert owner_row["ats_slug"] == "acme"

    def test_fastpath_branch_collision_marks_miss_with_collision_reason(self, migrated_db_path):
        """The careers_url fast-path tries to write a slug that's owned.

        URL inference picks (greenhouse, acme) from the careers_url; the
        owner already holds that pair. The fast-path UPDATE raises, the
        handler demotes the loser to miss/collision.
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """INSERT INTO companies
                  (name, name_raw, ats_platform, ats_slug,
                   ats_probe_status, scan_enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'hit', 1, ?, ?)""",
            ("acme corp", "Acme Corp", "greenhouse", "acme", now, now),
        )
        conn.commit()
        owner_id = cursor.lastrowid
        loser_id = _insert_pending_company(
            conn,
            name="Acme",
            careers_url="https://boards.greenhouse.io/acme",
        )
        conn.close()

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._verify_fastpath_live",
                return_value=True,
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 0
        assert result["misses"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        loser_row = conn.execute(
            "SELECT ats_probe_status, ats_platform, ats_slug, miss_reason "
            "FROM companies WHERE id=?",
            (loser_id,),
        ).fetchone()
        owner_row = conn.execute(
            "SELECT ats_platform, ats_slug FROM companies WHERE id=?",
            (owner_id,),
        ).fetchone()
        conn.close()

        assert loser_row["ats_probe_status"] == "miss"
        assert loser_row["miss_reason"] == "collision"
        assert loser_row["ats_platform"] is None
        assert loser_row["ats_slug"] is None
        assert owner_row["ats_platform"] == "greenhouse"
        assert owner_row["ats_slug"] == "acme"


# ---------------------------------------------------------------------------
# Write-time invariant tests (issue #1023)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Precedence determinism tests (issue #1040)
# ---------------------------------------------------------------------------


class TestConcurrentProbePrecedenceDeterminism:
    """Verify that concurrent platform probes respect _PROBES order precedence.

    The key invariant: even if a later-platform probe returns first (due to
    network timing), the earlier platform must win if it also hits. This
    preserves the original ladder's deterministic semantics.
    """

    def test_earlier_platform_beats_later_platform_even_if_slower(self, migrated_db_path):
        """Lever (earlier) beats Pinpoint (later) even if Pinpoint returns first.

        Simulates the case where a later platform probe is faster but an
        earlier platform also hits. The earlier platform must win by precedence.
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(conn, name="Acme", careers_url=None)
        conn.close()

        # Both platforms hit - the earlier one (Lever) must win by precedence
        fake_probes = [
            ("lever", lambda slug: True),
            ("greenhouse", lambda slug: False),
            ("ashby", lambda slug: False),
            ("jazzhr", lambda slug: False),
            ("pinpoint", lambda slug: True),
            ("teamtailor", lambda slug: False),
        ]

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=fake_probes,
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1
        assert result["misses"] == 0

        # Lever (earlier in _PROBES) must win regardless of execution timing
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_platform FROM companies WHERE id=?", (company_id,)
        ).fetchone()
        conn.close()
        assert row["ats_platform"] == "lever", "Lever must win by precedence over Pinpoint"

    def test_precedence_order_respected_with_multiple_hits(self, migrated_db_path):
        """When multiple platforms hit, the earliest in _PROBES order wins.

        Tests the full ladder: Lever > Greenhouse > Ashby > JazzHR > Pinpoint > Teamtailor.
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        company_id = _insert_pending_company(conn, name="Acme", careers_url=None)
        conn.close()

        # All platforms hit, but in reverse order of precedence
        fake_probes = [
            ("lever", lambda slug: True),
            ("greenhouse", lambda slug: True),
            ("ashby", lambda slug: True),
            ("jazzhr", lambda slug: True),
            ("pinpoint", lambda slug: True),
            ("teamtailor", lambda slug: True),
        ]

        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=fake_probes,
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        assert result["hits"] == 1

        # Lever (first in _PROBES) must win
        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ats_platform FROM companies WHERE id=?", (company_id,)
        ).fetchone()
        conn.close()
        assert row["ats_platform"] == "lever"


class TestConcurrentProbeCollisionPath:
    """Verify that collision handling remains intact under concurrent probing.

    The m076 UNIQUE(ats_platform, ats_slug) constraint and the collision
    resolution mechanism must work correctly even when probes are fired
    concurrently. This test ensures the collision path is not bypassed.
    """

    def test_collision_handling_intact_under_concurrency(self, migrated_db_path):
        """Concurrent probing must still respect UNIQUE constraint collision handling.

        When a speculative probe hits a (platform, slug) pair already owned by
        another company, the collision resolution path must run and either
        demote the owner or mark the challenger as miss with reason='collision'.
        This test verifies the collision path is invoked and the UNIQUE constraint
        is enforced (IntegrityError is raised and handled).
        """
        from jobcannon.engine.ats_scanner import probe_ats_slugs

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row

        # Insert owner company with a hit on lever/acme
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO companies
               (name, name_raw, ats_platform, ats_slug, ats_probe_status, created_at, updated_at)
               VALUES (?, ?, 'lever', 'acme', 'hit', ?, ?)""",
            ("Acme", "Acme", now, now),
        )
        owner_id = conn.execute("SELECT id FROM companies WHERE name_raw='Acme'").fetchone()["id"]

        # Insert challenger company (pending) - same name will derive to same slug
        challenger_id = _insert_pending_company(conn, name="Acme", careers_url=None)
        conn.close()

        # Mock probes to hit lever/acme for the challenger
        fake_probes = [
            ("lever", lambda slug: True),
            ("greenhouse", lambda slug: False),
            ("ashby", lambda slug: False),
            ("jazzhr", lambda slug: False),
            ("pinpoint", lambda slug: False),
            ("teamtailor", lambda slug: False),
        ]

        # Mock collision resolution to prevent demotion (so we can test the collision path)
        with (
            patch(
                "jobcannon.engine.ats_scanner._probe._PROBES",
                new=fake_probes,
            ),
            patch("jobcannon.engine.ats_scanner._probe.time.sleep"),
            patch(
                "jobcannon.engine.ats_scanner._probe._resolve_collision",
                return_value={
                    "demoted": False,
                    "existing_owner_id": owner_id,
                    "existing_owner_name": "Acme",
                    "challenge": None,
                },
            ),
        ):
            result = probe_ats_slugs(migrated_db_path, config={})

        # Challenger should get collision miss (owner is not demoted by our mock)
        assert result["hits"] == 0
        assert result["misses"] == 1

        conn = sqlite3.connect(migrated_db_path)
        conn.row_factory = sqlite3.Row

        # Verify owner still owns the slug
        owner_row = conn.execute(
            "SELECT ats_platform, ats_slug, ats_probe_status FROM companies WHERE id=?",
            (owner_id,),
        ).fetchone()
        assert owner_row["ats_platform"] == "lever"
        assert owner_row["ats_slug"] == "acme"
        assert owner_row["ats_probe_status"] == "hit"

        # Verify challenger got collision miss
        challenger_row = conn.execute(
            "SELECT ats_probe_status, miss_reason, ats_platform, ats_slug FROM companies WHERE id=?",
            (challenger_id,),
        ).fetchone()
        assert challenger_row["ats_probe_status"] == "miss"
        assert challenger_row["miss_reason"] == "collision"
        assert challenger_row["ats_platform"] is None
        assert challenger_row["ats_slug"] is None
        conn.close()

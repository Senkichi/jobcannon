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
            probe_hit_consistent_with_careers_url("pinpoint", "https://shopify.com/careers") is True
        )

    def test_url_inferred_platform_matches_hit_is_consistent(self):
        """Greenhouse URL + greenhouse hit → accept."""
        assert (
            probe_hit_consistent_with_careers_url("greenhouse", "https://boards.greenhouse.io/acme")
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


# DROPPED class (port L-group jobcannon/engine) [TestProbeAtsSlugsConsistencyGate]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


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
        assert careers_url_is_live("https://x/", _get=_raising_get(TimeoutError("timeout"))) is None


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

    # DROPPED test (port L-group jobcannon/engine) [TestProbeWriteBoundaryIdentityInvariant::test_speculative_hit_written_with_non_null_platform_and_slug]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap

    # DROPPED test (port L-group jobcannon/engine) [TestProbeWriteBoundaryIdentityInvariant::test_speculative_miss_leaves_identity_null]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap

    # DROPPED test (port L-group jobcannon/engine) [TestProbeWriteBoundaryIdentityInvariant::test_hit_write_never_carries_null_identity_columns]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap

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


# DROPPED class (port L-group jobcannon/engine) [TestMigrationScenario]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


# ---------------------------------------------------------------------------
# B1a — FP-prone platform exclusion (2026-05-27 audit corollary)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# B2 -- careers_url hostname fast-path
# ---------------------------------------------------------------------------


# DROPPED class (port L-group jobcannon/engine) [TestCareersUrlFastPath]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


# ---------------------------------------------------------------------------
# B4 -- categorical miss_reason on speculative-probe failures
# ---------------------------------------------------------------------------


# DROPPED class (port L-group jobcannon/engine) [TestSpeculativeMissCategorization]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


# ---------------------------------------------------------------------------
# m076: UNIQUE(ats_platform, ats_slug) collision recovery for probe_ats_slugs
# ---------------------------------------------------------------------------


# DROPPED class (port L-group jobcannon/engine) [TestSpeculativeProbeCollisionRecovery]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


# ---------------------------------------------------------------------------
# Write-time invariant tests (issue #1023)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Precedence determinism tests (issue #1040)
# ---------------------------------------------------------------------------


# DROPPED class (port L-group jobcannon/engine) [TestConcurrentProbePrecedenceDeterminism]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap


# DROPPED class (port L-group jobcannon/engine) [TestConcurrentProbeCollisionPath]: private-only migrated_db_path/app fixtures (SQLite migrated-DB clone-template / Flask app harness); jobcannon has no equivalent (Postgres tests/host harness is structurally different) -- L-group jobcannon/engine fixture-gap

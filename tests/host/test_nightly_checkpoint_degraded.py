"""PORTED from tests/test_nightly_checkpoint_degraded.py @
5221e7e6518c67e62996219e1c7c56747f10dd8f (private job-cannon). Ledger L-0607
(extends L-0471/L-0585's checkpoint_verdict.py). (5221e7e6 is the commit on
the private #2120 worker branch that never opened a PR; the orchestrator
salvaged the same diff onto private main as
1ecb8bb78bf6c9460dc1e6d7e7f90ae788b3d89e -- confirmed byte-identical for
both this file and the ported checkpoint_verdict.py branch above -- so a
fidelity run pinned to 5221e7e6 reports 1 stale commit against private main,
which is this salvage landing, not further drift.)

Regression tests for private issue #2120: disposition=degraded with a
non-null ``error`` must be a forced checkpoint verdict (ANOMALY), bypassing
the post-return reason filter exactly as disposition=failed and
disposition=orphaned bypass it today.

The private defect: the checkpoint verdicter had forced paths for
disposition=failed and (since #2107) disposition=orphaned, but not for
disposition=degraded. A degraded run's verdict therefore depended on whether
the post-return reason filter happened to leave any reason standing. On
2026-09-04 four ``health`` runs were byte-identical in their degrading
condition (``error: "Opaque-redirect candidates: example.com"``) and split
ANOMALY x3 / PASS x1 -- the PASS run's single model reason was emptied by a
guard (``rejected_reasons: 1``), downgrading ANOMALY to PASS on a packet
whose own ``error`` field says something failed.

The fix adds ``degraded`` + non-null ``error`` to the forced-verdict set with
a deterministic ANOMALY, pre-model, carrying the packet's own ``error``
string into ``reasons`` so the reason filter can never empty it and downgrade
to PASS. A degraded run with a null/empty ``error`` is NOT forced -- the
model adjudicates.

# PORT-SEAM: the private tests drove checkpoint_verdict(packet, conn, {})
# against a real sqlite connection (the migrated_db_mem fixture), built
# packets via the private build_packet() helper, and used a
# make_model_result fixture + patch(module call_model) to prove
# non-invocation. This port calls checkpoint_verdict(packet, call_model=...)
# per this file's injected call_model keyword seam (see
# checkpoint_verdict.py's own PORT-SEAM); the forced path returns before
# ever reaching the call_model(...) call site, so a raising call_model
# injected but never invoked -- proven by the test not erroring -- is the
# proof, matching this directory's sibling test_nightly_checkpoint_orphaned.py
# and test_nightly_checkpoint_verdict.py conventions. No DB connection is
# needed: the forced path never touches conn/config either, so conn/config
# and the migrated_db_mem fixture are dropped, and packets are built as
# plain dicts -- this repo's build_packet equivalent
# (checkpoint_packet.build_packet) computes band/duration derivations the
# forced branch never reads, so it is not needed either.
#
# Dropped (branch-ordering regression tests for the EXISTING forced paths,
# not the new one under test -- coverage is not lost, it already lives in
# this directory's sibling files): test_failed_still_forces_fail (see
# test_nightly_checkpoint_verdict.py::test_forced_fail_on_disposition_failed_skips_model_call)
# and test_orphaned_still_forces_anomaly (see
# test_nightly_checkpoint_orphaned.py::TestForcedOrphanedVerdict::test_orphaned_forces_anomaly_without_model_call).
"""

from __future__ import annotations

from jobcannon.host.nightly.checkpoint_packet import (
    LOG_EXCERPT_STATUS_CAPTURED_EMPTY,
    LOG_EXCERPT_STATUS_CAPTURED_NON_EMPTY,
)
from jobcannon.host.nightly.checkpoint_verdict import checkpoint_verdict

# The degrading condition from the 2026-09-04 health runs cited in private #2120.
_DEGRADING_ERROR = "Opaque-redirect candidates: example.com"


def _degraded_packet(
    *,
    run_id: str = "health:23868:1788581115",
    error: str | None = _DEGRADING_ERROR,
    log_excerpt_status: str = LOG_EXCERPT_STATUS_CAPTURED_EMPTY,
):
    """Minimal packet matching the 2026-09-04 degraded health runs: a
    disposition=degraded run whose packet carries the runner's own error
    description (or None/"" for the not-forced acceptance tests)."""
    return {
        "job": "health",
        "run_id": run_id,
        "disposition": "degraded",
        "duration_s": 18.92,
        "error": error,
        "result": {"issues": [_DEGRADING_ERROR]} if error else {"issues": []},
        "log_excerpt_status": log_excerpt_status,
    }


class _FakeResult:
    def __init__(self, data):
        self.data = data


def _model(verdict, reasons):
    def _call(**kwargs):
        return _FakeResult({"verdict": verdict, "reasons": reasons})

    return _call


def _raising_model(exc):
    def _call(**kwargs):
        raise exc

    return _call


class TestForcedDegradedVerdict:
    """disposition=degraded with a non-null error forces ANOMALY pre-model,
    independent of the reason filter and of model output."""

    def test_degraded_with_error_forces_anomaly_without_model_call(self):
        # No call_model passed: the forced path must return before reaching
        # the call site (an unforced path with call_model=None resolves to
        # VERDICT_UNAVAILABLE, not ANOMALY, so a correct forced result here
        # IS the proof the model was never consulted).
        packet = _degraded_packet()
        result = checkpoint_verdict(packet)
        assert result == {
            "verdict": "ANOMALY",
            "reasons": [f"disposition=degraded (error={_DEGRADING_ERROR})"],
            "forced": True,
            "rejected_reasons": 0,
        }

    def test_forced_reasons_name_the_packet_own_error_string(self):
        """Acceptance: the forced verdict's ``reasons`` names the packet's
        own ``error`` string, and ``forced: true`` is recorded."""
        packet = _degraded_packet()
        result = checkpoint_verdict(packet)
        assert result["forced"] is True
        assert result["reasons"], "forced reasons must not be empty"
        assert any(_DEGRADING_ERROR in r for r in result["reasons"])
        assert any("degraded" in r for r in result["reasons"])

    def test_degraded_with_error_never_passes_regardless_of_model_output(self):
        """Acceptance: a degraded+error packet never receives PASS, regardless
        of model output. Injects a call_model that raises if invoked at all --
        a regression sentinel: if the forced path is ever removed, this test
        fails with the injected error instead of quietly drifting to PASS."""
        packet = _degraded_packet()
        result = checkpoint_verdict(
            packet,
            call_model=_raising_model(AssertionError("forced path did not short-circuit")),
        )
        assert result["verdict"] != "PASS"
        assert result["verdict"] == "ANOMALY"
        assert result["forced"] is True

    def test_degraded_with_error_never_fails(self):
        """A degraded run is a partial failure, not a clear breakage -- the
        forced verdict is ANOMALY, never FAIL (#1367 invariant preserved)."""
        packet = _degraded_packet()
        result = checkpoint_verdict(
            packet,
            call_model=_raising_model(AssertionError("forced path did not short-circuit")),
        )
        assert result["verdict"] != "FAIL"
        assert result["verdict"] == "ANOMALY"


class TestExcerptAvailabilityIrrelevant:
    """Acceptance: two packets identical except for ``log_excerpt_status``
    (``captured_non_empty`` vs ``captured_empty``) produce the same verdict --
    the forced branch does not read ``log_excerpt_status`` at all, so
    excerpt-availability guard behavior cannot explain the split the private
    issue refuted."""

    def test_captured_non_empty_and_captured_empty_produce_same_verdict(self):
        p_non_empty = _degraded_packet(log_excerpt_status=LOG_EXCERPT_STATUS_CAPTURED_NON_EMPTY)
        p_empty = _degraded_packet(log_excerpt_status=LOG_EXCERPT_STATUS_CAPTURED_EMPTY)
        v_non_empty = checkpoint_verdict(p_non_empty)
        v_empty = checkpoint_verdict(p_empty)
        assert v_non_empty["verdict"] == v_empty["verdict"] == "ANOMALY"
        assert v_non_empty["forced"] is True
        assert v_empty["forced"] is True
        assert v_non_empty["rejected_reasons"] == v_empty["rejected_reasons"] == 0


class TestRejectedReasonsStillAnomaly:
    """Acceptance: a ``degraded`` packet whose model reasons would be
    rejected by the guard chain still yields ANOMALY with a non-empty
    ``reasons``, not PASS with ``reasons: []``.

    Before the fix, the model was called on the degraded packet, the guards
    could empty the reason list, and zero remaining reasons resolved to PASS
    -- the 2026-09-04 PASS packet had ``rejected_reasons: 1``. With the
    forced path the model is never consulted, so the filter has no
    opportunity to downgrade -- proven here by injecting a call_model that
    raises if invoked at all: if the forced path is ever removed, the model
    would be called, its reason guard-rejected, and the verdict would fall
    to PASS with ``reasons: []``, failing this test."""

    def test_rejected_model_reasons_still_yield_anomaly_with_non_empty_reasons(self):
        packet = _degraded_packet()
        result = checkpoint_verdict(
            packet,
            call_model=_raising_model(AssertionError("forced path did not short-circuit")),
        )
        assert result["verdict"] == "ANOMALY"
        assert result["forced"] is True
        assert result["reasons"], "forced reasons must not be empty (the defect produced [])"
        assert _DEGRADING_ERROR in result["reasons"][0]


class TestDegradedNullErrorNotForced:
    """A degraded run with a null/empty error is NOT forced -- the runner
    signalled degradation but supplied no specific failure description, so
    the model adjudicates from the rest of the packet. This guards against
    over-broadening the forced branch."""

    def test_degraded_null_error_goes_to_model(self):
        packet = _degraded_packet(error=None)
        assert packet["error"] is None
        result = checkpoint_verdict(packet, call_model=_model("PASS", []))
        assert result["forced"] is False

    def test_degraded_empty_error_goes_to_model(self):
        packet = _degraded_packet(error="")
        assert packet["error"] == ""
        result = checkpoint_verdict(packet, call_model=_model("PASS", []))
        assert result["forced"] is False


class TestDegradedHealthRunRegressionFixture:
    """Regression fixture built from the 2026-09-04 degraded health run
    packets cited in private issue #2120. Before the fix, one of four
    byte-identical packets verdicted PASS (``reasons: []``,
    ``rejected_reasons: 1``, ``forced: false``) while the other three
    verdicted ANOMALY. All four must now verdict ANOMALY."""

    def test_degrading_packet_verdicts_anomaly(self):
        packet = _degraded_packet(run_id="health:23868:1788581115")
        result = checkpoint_verdict(packet)
        assert result["verdict"] == "ANOMALY"
        assert result["forced"] is True
        assert result["reasons"], "reasons must not be empty (the defect produced [])"
        assert _DEGRADING_ERROR in result["reasons"][0]
        assert result["rejected_reasons"] == 0

    def test_four_degrading_packets_verdict_identically(self):
        """The four 2026-09-04 health packets (three ANOMALY, one PASS) must
        now all verdict ANOMALY -- the within-window contradiction on
        identical inputs is resolved."""
        packets = [
            _degraded_packet(run_id="health:27860:1788526800"),
            _degraded_packet(run_id="health:46100:1788531855"),
            _degraded_packet(run_id="health:24804:1788554743"),
            _degraded_packet(run_id="health:23868:1788581115"),
        ]
        verdicts = [checkpoint_verdict(p) for p in packets]
        assert all(v["verdict"] == "ANOMALY" for v in verdicts)
        assert len({v["verdict"] for v in verdicts}) == 1
        assert all(v["forced"] is True for v in verdicts)
        assert all(v["rejected_reasons"] == 0 for v in verdicts)

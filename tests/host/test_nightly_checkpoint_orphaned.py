"""PORTED from tests/test_nightly_checkpoint_orphaned.py @ a7f0f38a (private
job-cannon). Ledger L-0585 (extends L-0471's checkpoint_verdict.py).

Regression tests for private issue #2107: disposition=orphaned must be a
forced checkpoint verdict (ANOMALY), bypassing the post-return reason filter
exactly as disposition=failed bypasses it today.

The private defect: the checkpoint verdicter had a forced path for
disposition=failed but not for disposition=orphaned. An orphan's verdict
therefore depended on whether the post-return reason filter happened to
leave any reason standing. On 2026-09-02 a process died mid-tick with two
runs in flight; both packets were disposition=orphaned with duration_s,
result, and error all null -- and they received opposite verdicts (ANOMALY
and PASS) because the filter emptied the reason list on the second. A run
that never reported an outcome was recorded as passing.

The fix adds "orphaned" to the forced-verdict set with a deterministic
ANOMALY, pre-model, so the reason filter can never downgrade it to PASS.

# PORT-SEAM: the private tests drove checkpoint_verdict(packet, conn, {})
# against a real sqlite connection (the migrated_db_mem fixture) and patched
# the module-level call_model import to assert it was never called. This
# port calls checkpoint_verdict(packet, call_model=...) per this file's
# injected call_model keyword seam (see checkpoint_verdict.py's own
# PORT-SEAM); the forced path returns before ever reaching the call_model(...)
# call site, so a raising call_model injected but never invoked -- proven by
# the test not erroring -- is the proof, matching this test module's sibling
# test_nightly_checkpoint_verdict.py conventions. No DB connection is needed:
# the forced path never touches conn/config either, so conn/config and the
# migrated_db_mem fixture are dropped -- nothing here is DB-dependent.
"""

from __future__ import annotations

from jobcannon.host.nightly.checkpoint_verdict import checkpoint_verdict

_FORCED_REASON = (
    "disposition=orphaned (run ended with no terminal event; "
    "process was reaped or wedged before reporting an outcome)"
)


def _orphan_packet(*, job: str, run_id: str):
    """Minimal packet matching an orphaned run_end: no duration, result, or
    error, since the process died or was reclaimed before reporting one."""
    return {
        "job": job,
        "run_id": run_id,
        "disposition": "orphaned",
        "duration_s": None,
        "error": None,
        "result": None,
    }


def _raising_model(exc):
    def _call(**kwargs):
        raise exc

    return _call


class TestForcedOrphanedVerdict:
    """disposition=orphaned forces ANOMALY pre-model, independent of the
    reason filter."""

    def test_orphaned_forces_anomaly_without_model_call(self):
        packet = _orphan_packet(job="ATS scan", run_id="ATS scan:71196:1")
        # No call_model passed: the forced path must return before reaching
        # the call site (an unforced path with call_model=None resolves to
        # VERDICT_UNAVAILABLE, not ANOMALY -- see
        # test_call_model_default_none_yields_verdict_unavailable), so a
        # correct forced result here IS the proof the model was never
        # consulted.
        result = checkpoint_verdict(packet)
        assert result == {
            "verdict": "ANOMALY",
            "reasons": [_FORCED_REASON],
            "forced": True,
            "rejected_reasons": 0,
        }

    def test_orphaned_never_resolves_to_pass_even_if_call_model_would_fire(self):
        """The core defect: if the forced path were ever removed, the model
        would be consulted on this bare packet and its answer could resolve
        to PASS (directly, or via the post-return guards stripping every
        reason down to zero). This test injects a call_model that raises if
        invoked at all -- a regression sentinel: if the forced path is ever
        removed, this test fails with the injected error instead of quietly
        drifting to PASS."""
        packet = _orphan_packet(job="ATS scan", run_id="ATS scan:71196:1")
        result = checkpoint_verdict(
            packet,
            call_model=_raising_model(AssertionError("forced path did not short-circuit")),
        )
        assert result["verdict"] == "ANOMALY"
        assert result["forced"] is True
        assert result["rejected_reasons"] == 0

    def test_two_orphans_same_process_death_identical_verdicts(self):
        """Two orphaned packets from the same process death (same pid) must
        receive identical verdicts -- the 2026-09-02 regression."""
        p_jd = _orphan_packet(job="JD adjudication", run_id="JD adjudication:71196:1788375600")
        p_ats = _orphan_packet(job="ATS render scan", run_id="ATS render scan:71196:1788373976")
        v_jd = checkpoint_verdict(p_jd)
        v_ats = checkpoint_verdict(p_ats)
        assert v_jd["verdict"] == v_ats["verdict"] == "ANOMALY"
        assert v_jd["forced"] is True
        assert v_ats["forced"] is True
        assert v_jd["rejected_reasons"] == v_ats["rejected_reasons"] == 0


class TestPid71196RegressionFixture:
    """Regression fixture built from the two same-pid packets cited in the
    private issue. Before the fix, one packet verdicted PASS (reasons
    emptied by the filter) while the other verdicted ANOMALY. Both must now
    verdict identically."""

    def test_both_same_pid_packets_verdict_identically(self):
        p_jd = _orphan_packet(job="JD adjudication", run_id="JD adjudication:71196:1788375600")
        p_ats = _orphan_packet(job="ATS render scan", run_id="ATS render scan:71196:1788373976")
        # Sanity: both packets carry the orphaned disposition and no duration.
        assert p_jd["disposition"] == "orphaned"
        assert p_ats["disposition"] == "orphaned"
        assert p_jd["duration_s"] is None
        assert p_ats["duration_s"] is None

        v_jd = checkpoint_verdict(p_jd)
        v_ats = checkpoint_verdict(p_ats)

        # The defect: one packet was PASS, the other ANOMALY. Both must match.
        assert v_jd["verdict"] == v_ats["verdict"] == "ANOMALY"
        assert v_jd["forced"] is True
        assert v_ats["forced"] is True
        assert v_jd["rejected_reasons"] == 0
        assert v_ats["rejected_reasons"] == 0

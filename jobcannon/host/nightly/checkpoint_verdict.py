"""ADAPTED from job_finder/web/nightly_monitor/_checkpoint.py (verdict half)
@ 5221e7e6518c67e62996219e1c7c56747f10dd8f (private job-cannon).
Ledger L-0471, L-0585, L-0607. (5221e7e6 is the commit on the private #2120
worker branch that never opened a PR; the orchestrator salvaged the same
diff onto private main as 1ecb8bb78bf6c9460dc1e6d7e7f90ae788b3d89e --
confirmed byte-identical for both this file and the carried test below -- so
a fidelity run pinned to 5221e7e6 reports 1 stale commit against private
main, which is this salvage landing, not further drift.)

Checkpoint verdict: given an evidence packet from checkpoint_packet.py,
call the injected verdict model and run the packet-falsification guard
chain over its answer. This module holds the public entry point and the
model-call path; a second file split (issue #421, module-size convention)
moved the guard chain to checkpoint_verdict_guards.py and the pure
packet/reason predicates it applies to checkpoint_verdict_checks.py. The
moved names are re-exported below so existing
``from jobcannon.host.nightly.checkpoint_verdict import <name>`` callers
are unchanged.

Three forced rules run BEFORE any model call: disposition=failed (the
authoritative per-run signal) is a FAIL regardless of model opinion,
disposition=orphaned (a run that ended with no terminal outcome -- no
duration, result, or error) is an ANOMALY regardless of model opinion, and
disposition=degraded with a non-null ``error`` (private #2120) is an ANOMALY
regardless of model opinion, so none of the three can silently resolve to
PASS. A fail-severity signature is NOT forced -- hits are matched over the
whole tick's log window and carry no run_id, so forcing FAIL would blame
every job sharing the tick with another job's failure line; the hits go into
the packet and the attribution-aware model adjudicates. These are the
private implementation's own rules, stated in its docstring (private #2107,
#2120), and this port preserves them byte-identical -- disposition=failed,
disposition=orphaned, and disposition=degraded+error only; forcing these
verdicts pre-model keeps them out of the post-return reason filter's reach,
since the filter can empty a bare packet's reason list and would otherwise
downgrade ANOMALY to PASS. Otherwise the verdict comes from the injected
``call_model`` at workload tier "quick". Unparseable model output => ANOMALY;
verdict-call failures (missing call_model, provider/transport/cascade
exhausted) => VERDICT_UNAVAILABLE -- both are fail-safe and not alarming, but
only the latter carries the infra-failure signal so morning review can
separate job anomalies from scorer outages (issue #1402).

# PORT-SEAM: call_model is an injected optional keyword parameter
# (default None), matching jobcannon.engine.job_scorer.score_job's
# call_model injection seam and jobcannon.host.model_provider.call_model's
# own docstring ("hosted scoring has no live caller wired to a tenant yet;
# when user_id is None ... the call fails closed"). No caller on this
# branch supplies a user_id-scoped call_model today, so every call here
# falls through the broad except below: call_model=None -> TypeError ->
# VERDICT_UNAVAILABLE. That is the SAME fail-safe path a live cascade
# exhaustion would take, so no special-cased branch is needed for the
# not-yet-wired case. The owner-tenant-identity resolution that a future
# caller needs (which user_id owns a given nightly job) is unscoped here
# and is listed as a follow-up, not invented.
#
# PORT-SEAM: disposition=orphaned reachability -- checkpoint_packet.py's
# module docstring records that the hosted caller (jobcannon.host.nightly.
# sampler) derives ``disposition`` from a procrastinate_jobs row via
# sampler.py's ``_STATUS_TO_DISPOSITION`` map, which today covers only
# {"succeeded": "completed", "failed": "failed"} and falls through to the
# raw procrastinate job status otherwise; procrastinate has no "orphaned"
# status, so no current host caller emits this disposition value. The forced
# branch is ported anyway for parity with the private rule and to be correct
# the moment a caller does emit it (e.g. a future host-side orphan-reclaim
# path modeled on jobcannon.host.tasks.reclaim_orphaned_jobs), matching how
# call_model above is ported unwired ahead of its caller.
#
# PORT-SEAM: disposition=degraded+error reachability (private #2120) -- the
# same sampler.py ``_STATUS_TO_DISPOSITION`` map has no "degraded" entry
# either, and procrastinate has no "degraded" job status, so no current host
# caller emits this disposition value today (baselines.py's own
# ``_TERMINAL_OK = ("completed", "degraded")`` shows "degraded" is already a
# recognized terminal-outcome token elsewhere in this module tree -- it is
# simply not yet wired to a producer that emits it as a checkpoint packet's
# ``disposition``). The forced branch is ported anyway, byte-identical to the
# private rule, dormant and safe to ship ahead of a producer -- matching how
# the orphaned branch above and the call_model seam are both ported unwired
# ahead of their callers.
#
# jd_full_loss_excess (private's fourth forced rule, ANOMALY on a
# jd_full-loss invariant violation) is DROPPED, not ported: it imports
# `job_finder.web.run_events.jd_full_loss_excess`, a module outside
# nightly_monitor/ with no host analog, and it operates on `db_delta`,
# which no hosted caller populates yet (see checkpoint_packet.py's module
# docstring). A stub that always returns 0 would look implemented when it
# is not, so the branch is absent rather than inert. Forced verdicts here
# are scoped to exactly what the private code's own docstring specifies:
# disposition=failed (FAIL), disposition=orphaned (ANOMALY), and
# disposition=degraded+error (ANOMALY) only -- no other disposition value
# is forced. A fail-severity signature is adjudicated by the model like any
# other packet evidence, matching the private implementation -- FAIL
# escalation to a recorded health-log ERROR is a sampler-level concern, not
# this function's internal forcing rule.

The packet that reaches the model is a *semantic* view of the run. Raw
``db_delta`` integers are replaced by a precomputed ``db_delta_summary`` that
labels each counter as ``improved_by_N``, ``worsened_by_N``, ``unchanged``, or
``not_attributable`` (when the run's window overlapped another run's, so the
database-wide counter diff cannot be attributed to this run alone -- issue
#1734), and ``db_delta_tracked`` marks whether this job's work is expected to
move those counters. After the model returns, a chain of deterministic Python
post-checks suppresses reasons the packet itself falsifies: ``_sanitize_verdict``
catches db_delta sign/zero misreadings, ``_guard_in_band_duration`` catches
duration reasons the deterministic band already cleared, ``_validate_reasons``
catches fabricated out_of_band/p10/p90 assertions and no-work/negative-counter
claims, ``_guard_non_attributable_db_delta`` drops reasons citing a db_delta
counter movement when the delta is not attributable to this run,
``_guard_new_row_backlog`` drops reasons citing only new-row-bounded counters
whose growth is the arithmetic consequence of ingestion, and
``_guard_ambiguous_only_evidence`` catches escalation on unattributable
concurrent-job noise alone. ``_guard_excerpt_absence`` drops reasons whose sole
content is that the log excerpt is empty/missing when the capture was
unavailable or genuinely empty (issue #2013) -- the absence of log lines is an
evidence-availability caveat, not a job anomaly. Each verdict return also
carries ``rejected_reasons``: the count of model-supplied reasons dropped as
fabricated by ``_sanitize_verdict``, ``_guard_in_band_duration``,
``_validate_reasons``, ``_guard_non_attributable_db_delta``,
``_guard_new_row_backlog``, or ``_guard_excerpt_absence`` (the note
substitution in ``_guard_ambiguous_only_evidence`` is a structural downgrade,
not a falsified-reason rejection, and does not add to the count). This whole
chain is a fidelity anchor for this port: it is a set of pure functions over
whatever the packet carries, and its logic carries over from the private
original unchanged function-for-function -- verified by a name-and-body
extraction diff against the private source at the pinned SHA (all 29
top-level functions from _checkpoint.py present by name across this file's
three checkpoint_verdict* modules and checkpoint_packet.py, zero body drift
beyond the documented seams: call_model injection, jd_full_loss_excess
removal, and the success_count_keys config source below; the issue #421
split changed which module each function lives in, not any function body).
Docstrings and comments are NOT verbatim: they are rewrapped
to this repo's line length and re-cited to its ``issue #N`` convention
(private used bare ``#N``; see e.g. jobcannon/host/model_provider.py), so a
raw text diff against the private original is expected to show prose-only
hunks beyond the split itself.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

# Re-exports: the names below moved to checkpoint_verdict_guards.py /
# checkpoint_verdict_checks.py in the issue #421 module-size split. They stay
# importable from this module so existing callers (the host test suites import
# the guards and predicates directly) do not change.
from jobcannon.host.nightly.checkpoint_verdict_checks import (  # noqa: F401
    _has_evidence_of_work,
    _has_success_excerpt,
    _is_excerpt_absence_reason,
    _is_improvement_reason,
    _is_no_work_reason,
    _reason_contradicts,
)
from jobcannon.host.nightly.checkpoint_verdict_guards import (
    _guard_ambiguous_only_evidence,
    _guard_excerpt_absence,
    _guard_in_band_duration,
    _guard_new_row_backlog,
    _guard_non_attributable_db_delta,
    _sanitize_verdict,
    _validate_reasons,
)
from jobcannon.host.nightly.checkpoint_verdict_guards import (  # noqa: F401
    _AMBIGUOUS_ONLY_NOTE,
    _CAPTURE_UNAVAILABLE_NOTE,
    _PASS_NOTE,
)

logger = logging.getLogger(__name__)

_VALID_VERDICTS = ("PASS", "ANOMALY", "FAIL")
VERDICT_UNAVAILABLE = "VERDICT_UNAVAILABLE"

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "ANOMALY", "FAIL"]},
        "reasons": {"type": "array", "items": {"type": "string", "maxLength": 300}},
    },
    "required": ["verdict", "reasons"],
}

_SYSTEM = (
    "You are a release-engineering checkpoint reviewer for overnight scheduled "
    "jobs. Given one job-run evidence packet (JSON), return STRICT JSON "
    "{verdict, reasons}. The packet includes `band_assessment` "
    "('insufficient_history' | 'in_band' | 'out_of_band') and `out_of_band` "
    "('fast' | 'slow' | null). `out_of_band` is the only sanctioned duration "
    "signal; do not re-derive duration anomalies from `baseline`, `duration_s`, "
    "or `in_band`. When `band_assessment` is 'in_band' (i.e. `out_of_band` is "
    "null and the run is within the tolerance-and-floor band), you MUST NOT "
    "issue duration-based ANOMALY reasons. When `band_assessment` is "
    "'insufficient_history', the deterministic band has not been established "
    "for this run (too few good-run samples or missing duration), so a null "
    "`out_of_band` is NOT a clearance; duration may be one ordinary signal "
    "among others. PASS = run looks healthy. ANOMALY = something is off but not "
    "clearly broken (anomaly-severity log signatures, unexplained db_delta for "
    "job-table counters). FAIL = clear breakage. `db_delta` tracks job-table "
    "counters only (total_jobs, scoring_backlog, classification_null, "
    "missing_jd_full, first_seen_today); a null or zero db_delta is not evidence "
    "of 'no work' for jobs whose output is not job-table-visible (company "
    "linkage, registry hygiene, backup). Use the precomputed "
    "`db_delta_summary` -- do not reason from raw `db_delta` integers. Each "
    "counter is labelled: `improved_by_N` means the count moved in the healthy "
    "direction, `worsened_by_N` means it moved in an unhealthy direction, "
    "`unchanged` means no change, and `pending_from_new_rows_N` means a "
    "decrease-direction counter (classification_null, missing_jd_full) rose "
    "by N solely because the run inserted N or more new rows -- each freshly "
    "inserted row is unclassified and jd_full-missing by construction, so "
    "this is the arithmetic consequence of ingestion, NOT backlog "
    "accumulation, and MUST NOT be cited as an anomaly reason. When the "
    "increase exceeds the new-row count, the label is "
    "`worsened_by_N` with N the excess only. `scoring_backlog` is NOT "
    "new-row-bounded: it only counts rows that already have jd_full, which "
    "a freshly inserted row does not, so its label is never "
    "`pending_from_new_rows_N` -- but a `scoring_backlog` increase can also "
    "be the expected result of an enrichment run filling in `jd_full` on "
    "existing rows, moving them into the scoring queue; use the rest of "
    "the evidence (disposition, log signatures, whether the run is "
    "enrichment vs. ingestion) to judge whether a rise is a genuine "
    "problem, same as any other `worsened_by_N` counter. For the jobs-table counters: `missing_jd_full` "
    "and `scoring_backlog` improve when they decrease; `classification_null` "
    "improves when it decreases; `total_jobs` and `first_seen_today` improve "
    "when they increase. A negative `missing_jd_full` or `scoring_backlog` delta "
    "is progress, not a defect. `db_delta_tracked` is true only for jobs whose "
    "work is expected to move those counters, false when history shows they do "
    "not, and null when the job has no run history. If `db_delta_tracked` is "
    "false or null, an all-zero `db_delta_summary` is NOT evidence of a no-op; "
    "use `log_excerpt` and `result` instead. A run with `disposition: "
    "'completed'`, `error: null`, and a success token in `log_excerpt`/`result` "
    "is healthy; do not call it a no-op just because db_delta is flat. "
    "`db_delta_attributable` is true only when `concurrent_run_ids` is empty -- "
    "i.e. no other run's window overlapped this run's. When it is false, "
    "`db_delta` is a database-wide counter diff that may include another "
    "concurrently-running job's writes; every counter in `db_delta_summary` is "
    "labelled `not_attributable`, and you MUST NOT issue a reason citing a "
    "db_delta counter movement (improved/worsened/changed) for such a run -- "
    "the movement is not this run's work. Use `log_excerpt`, `result`, and "
    "`signature_hits` instead. "
    "signature_hits contains only log-signature matches "
    "that fall within this run's own [start, end] time window. "
    "shared_signature_hits contains matches from windows that overlap this run "
    "or could not be uniquely attributed; do not blame this run for those lines. "
    "log_excerpt is job-scoped only when log_excerpt_is_job_scoped is true; "
    "when true, it contains only log lines whose timestamp falls exclusively in "
    "this run's own [start, end] window and in no other concurrently-running "
    "job's window. concurrent_context contains lines from the same time window "
    "that also fall inside another run's window; treat them as cross-job noise "
    "and never as evidence about this job. When log_excerpt_is_job_scoped is "
    "false, log_excerpt is a time-windowed tail of the SHARED application log, "
    "NOT a job-scoped transcript; do not treat a line as evidence about this job "
    "unless the line names this job's run_id or this job's own logger. "
    "log_excerpt_status is a three-state capture outcome: "
    "'captured_non_empty' means run-owned lines were found and are present; "
    "'captured_empty' means the capture ran against a correctly identified run "
    "window but the job emitted no matching lines (this is NOT an anomaly -- a "
    "quiet run is not a broken run); 'capture_unavailable' means no run-owned "
    "window could be established (log rotated, no scoping anchor, or a "
    "concurrent run's window could not be resolved), so the empty excerpt "
    "carries NO information about the job's activity. You MUST NOT issue an "
    "ANOMALY or FAIL reason whose sole content is that the log excerpt is "
    "empty, missing, or absent when log_excerpt_status is 'capture_unavailable' "
    "or 'captured_empty' -- the absence of log lines is an evidence-availability "
    "caveat, not a job anomaly. Absent or "
    "unrelated log content is NOT evidence of anomaly; in particular, do not "
    "cite another job's warnings (e.g. content-gating, stale_detector, "
    "expiry_checker) as a reason about this job. At most 3 short reasons."
)


def checkpoint_verdict(
    packet: dict,
    *,
    call_model: Callable[..., Any] | None = None,
    conn: Any = None,
    config: dict | None = None,
) -> dict:
    """Forced verdicts on the authoritative per-run signals: disposition=failed
    (FAIL), disposition=orphaned (ANOMALY), and disposition=degraded with a
    non-null ``error`` (ANOMALY); everything else is model-adjudicated
    (private #2107, #2120; see module docstring).

    A fail-severity signature is deliberately NOT a forced FAIL: hits are matched
    over the whole tick's log window and carry no run_id, so forcing FAIL here
    would blame every job whose run_end shares the tick with another job's
    failure line. The hits ARE in the packet; the attribution-aware model
    (system prompt) decides, and the fallbacks below floor the signal if the
    model is unavailable. Unparseable model output => ANOMALY; verdict-call
    failures (missing ``call_model``, provider/transport/cascade exhausted)
    => VERDICT_UNAVAILABLE, preserving the exception type/message in reasons
    so morning review can distinguish job anomalies from infra noise (issue
    #1402).

    ``disposition=orphaned`` is forced to ANOMALY pre-model: an orphan is a
    run that ended with no terminal outcome (no duration/result/error), so it
    carries no evidence a guard could validate. Without a forced path the
    verdict would fall to the model and the post-return reason guards, which
    can empty the reason list on such a bare packet and downgrade ANOMALY to
    PASS -- recording a run that never reported an outcome as passing.
    Forcing pre-model bypasses the reason filter entirely, exactly as
    ``failed`` does.

    ``disposition=degraded`` with a non-null ``error`` is forced to ANOMALY
    pre-model (private #2120): ``degraded`` means the runner detected a
    partial failure and the ``error`` string is the runner's own description
    of what went wrong, so a run whose packet carries such an error is by
    definition not clean and must never resolve to PASS. Without a forced
    path the verdict fell to the model and the same post-return reason
    guards, which can empty the reason list on such a packet and downgrade
    ANOMALY to PASS -- recording a run whose own packet says something failed
    as clean. Forcing pre-model and carrying the packet's own ``error`` into
    ``reasons`` bypasses the reason filter entirely, exactly as ``failed`` and
    ``orphaned`` do. A degraded run with a null/empty ``error`` is NOT forced:
    the runner signalled degradation but supplied no specific failure
    description, so the model adjudicates from the rest of the packet.

    Every return path carries ``rejected_reasons``: the count of model-supplied
    reasons dropped by the deterministic post-verdict guards as fabricated
    (falsified against the packet), defaulting to 0 where no model reasons
    reached validation (forced FAIL/ANOMALY, unparseable verdict, verdict-call
    failure).

    ``call_model`` defaults to None: on this host, no caller has a live
    user_id-scoped model dispatcher wired to a nightly-monitor tick yet (see
    module docstring). Calling ``None(...)`` raises ``TypeError``, caught by
    the same broad ``except Exception`` below that catches a real cascade
    exhaustion -- both resolve to the identical VERDICT_UNAVAILABLE fail-safe.
    """
    if packet.get("disposition") == "failed":
        return {
            "verdict": "FAIL",
            "reasons": [f"disposition=failed (error={packet.get('error')})"],
            "forced": True,
            "rejected_reasons": 0,
        }
    if packet.get("disposition") == "orphaned":
        # PORT-SEAM: private #2107 -- an orphan never reported an outcome (no
        # duration/result/error). Force ANOMALY pre-model so the reason
        # filter cannot empty the reason list and downgrade to PASS, exactly
        # as disposition=failed forces FAIL above.
        return {
            "verdict": "ANOMALY",
            "reasons": [
                "disposition=orphaned (run ended with no terminal event; "
                "process was reaped or wedged before reporting an outcome)"
            ],
            "forced": True,
            "rejected_reasons": 0,
        }
    if packet.get("disposition") == "degraded" and packet.get("error"):
        # PORT-SEAM: private #2120 -- a degraded run whose packet carries a
        # non-null error cannot be adjudicated PASS. Force ANOMALY pre-model
        # so the reason filter cannot empty the reason list and downgrade to
        # PASS, exactly as disposition=failed and disposition=orphaned force
        # their verdicts above.
        return {
            "verdict": "ANOMALY",
            "reasons": [f"disposition=degraded (error={packet.get('error')})"],
            "forced": True,
            "rejected_reasons": 0,
        }
    try:
        # The model sees the semantic summary and the deterministic band, not
        # the raw signed db_delta integers.
        model_packet = {k: v for k, v in packet.items() if k != "db_delta"}
        result = call_model(
            tier="quick",
            system=_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(model_packet)}],
            conn=conn,
            config=config or {},
            output_schema=VERDICT_SCHEMA,
            purpose="nightly_checkpoint",
            max_tokens=512,
        )
        data = result.data
        if data.get("verdict") in _VALID_VERDICTS:
            raw_verdict = data["verdict"]
            raw_reasons = [str(r)[:300] for r in list(data.get("reasons", []))[:3]]
            verdict, reasons = _sanitize_verdict(packet, raw_verdict, raw_reasons)
            sanitized_rejected = len(raw_reasons) - len(reasons)
            verdict, reasons, dropped = _guard_in_band_duration(packet, verdict, reasons)
            verdict, reasons, rejected = _validate_reasons(packet, verdict, reasons)
            verdict, reasons, non_attr_rejected = _guard_non_attributable_db_delta(
                packet, verdict, reasons
            )
            verdict, reasons, new_row_rejected = _guard_new_row_backlog(packet, verdict, reasons)
            verdict, reasons, excerpt_rejected = _guard_excerpt_absence(packet, verdict, reasons)
            verdict, reasons = _guard_ambiguous_only_evidence(packet, verdict, reasons)
            rejected_reasons = (
                sanitized_rejected
                + dropped
                + rejected
                + non_attr_rejected
                + new_row_rejected
                + excerpt_rejected
            )
            return {
                "verdict": verdict,
                "reasons": [str(r)[:300] for r in list(reasons)[:3]],
                "forced": False,
                "rejected_reasons": rejected_reasons,
            }
        return {
            "verdict": "ANOMALY",
            "reasons": ["unparseable model verdict"],
            "forced": False,
            "rejected_reasons": 0,
        }
    except Exception as exc:
        logger.warning("nightly checkpoint verdict call failed", exc_info=True)
        detail = f": {exc}" if str(exc) else ""
        return {
            "verdict": VERDICT_UNAVAILABLE,
            "reasons": [f"verdict call failed: {type(exc).__name__}{detail}"],
            "forced": False,
            "rejected_reasons": 0,
        }

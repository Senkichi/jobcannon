"""ADAPTED from job_finder/web/nightly_monitor/_checkpoint.py (verdict half)
@ 5221e7e6518c67e62996219e1c7c56747f10dd8f (private job-cannon).
Ledger L-0471, L-0585, L-0607.

The deterministic post-verdict falsification guard chain: each guard takes
(packet, verdict, reasons) and returns a possibly-downgraded
(verdict, reasons, dropped_count) -- pure functions over whatever the
packet carries, invoked in fixed order by checkpoint_verdict() in
checkpoint_verdict.py. Split out of checkpoint_verdict.py under the
module-size convention (issue #421); every function moved verbatim. The
predicates the guards apply live in checkpoint_verdict_checks.py; see
checkpoint_verdict.py's module docstring for this port's provenance and
why this chain is the port's fidelity anchor.
"""

from __future__ import annotations

from jobcannon.host.nightly.checkpoint_packet import (
    LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE,
    LOG_EXCERPT_STATUS_CAPTURED_EMPTY,
    _DB_DELTA_COUNTER_KEYS,
    _db_delta_summary,
)
from jobcannon.host.nightly.checkpoint_verdict_checks import (
    _DURATION_REASON_RE,
    _counter_mentioned,
    _improved_and_worsened_counters,
    _is_excerpt_absence_reason,
    _is_improvement_reason,
    _is_no_work_phrase,
    _is_no_work_reason,
    _packet_has_success,
    _reason_cites_db_delta_counter,
    _reason_contradicts,
)
from jobcannon.host.nightly.config import nightly_monitor_config


def _sanitize_verdict(packet: dict, verdict: str, reasons: list[str]) -> tuple[str, list[str]]:
    """Post-return guard: drop reasons based solely on db_delta misreadings.

    Suppresses:
      - a reason that treats a *decrease*-direction improvement as a defect;
      - a reason that treats an all-zero db_delta as "no work" on a job where
        db_delta is not tracked;
      - any db-delta no-work/improvement reason when the packet's own
        success indicators (completed, no error, success in log/result) contradict it.

    If every reason is suppressed, the verdict is downgraded to PASS (because
    the remaining evidence no longer supports ANOMALY/FAIL).

    # PORT-SEAM: private sourced success_count_keys from
    # get_nightly_monitor_config(config or {}) -- a YAML config.yaml dict.
    # There is no config.yaml on this host; nightly_monitor_config() reads
    # the same tunable from an env var (with the same default set) instead,
    # so the ``config`` parameter private threaded through here is dropped.
    """
    summary = packet.get("db_delta_summary") or _db_delta_summary(
        packet.get("db_delta"),
        packet.get("db_delta_tracked"),
        attributable=packet.get("db_delta_attributable", True),
    )
    tracked = summary.get("tracked")
    all_zero = all(c.get("raw_delta", 0) == 0 for c in summary.get("by_counter", {}).values())
    nightly_cfg = nightly_monitor_config()
    success = _packet_has_success(packet, nightly_cfg["success_count_keys"])

    kept: list[str] = []
    for reason in reasons:
        if _is_improvement_reason(reason, summary):
            continue
        if tracked is False and all_zero and _is_no_work_reason(reason):
            continue
        if success and (_is_improvement_reason(reason, summary) or _is_no_work_phrase(reason)):
            continue
        kept.append(reason)

    if not kept and verdict in ("ANOMALY", "FAIL"):
        verdict = "PASS"
    return verdict, kept


_PASS_NOTE = ["duration in band; out_of_band is null"]
_AMBIGUOUS_ONLY_NOTE = [
    "run completed in band with no run-attributed signature hits; "
    "sole anomaly evidence is ambiguous shared hits recorded as context"
]
_CAPTURE_UNAVAILABLE_NOTE = [
    "log excerpt capture unavailable (no run-owned window established); "
    "excerpt absence is an evidence caveat, not a job anomaly"
]


def _guard_excerpt_absence(
    packet: dict, verdict: str, reasons: list[str]
) -> tuple[str, list[str], int]:
    """Post-verdict boundary: drop reasons solely about an absent log excerpt.

    When ``log_excerpt_status`` is ``capture_unavailable`` or
    ``captured_empty``, the empty/missing excerpt is an evidence-availability
    caveat, not a job anomaly (issue #2013). A model reason whose sole content
    is that the excerpt is empty/missing/absent is a false escalation and is
    dropped. If stripping those reasons empties an ANOMALY or FAIL verdict,
    downgrade to PASS with a caveat note so the evidence gap is surfaced
    without manufacturing an anomaly.

    Returns ``(verdict, reasons, dropped_count)``. A no-op when the status
    is ``captured_non_empty`` (a non-empty excerpt is real evidence the model
    may reason about).
    """
    status = packet.get("log_excerpt_status")
    if status not in (
        LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE,
        LOG_EXCERPT_STATUS_CAPTURED_EMPTY,
    ):
        return verdict, reasons, 0
    kept: list[str] = []
    dropped = 0
    for reason in reasons:
        if _is_excerpt_absence_reason(reason):
            dropped += 1
            continue
        kept.append(reason)
    if not kept and verdict in ("ANOMALY", "FAIL"):
        note = (
            _CAPTURE_UNAVAILABLE_NOTE
            if status == LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE
            else [
                "log excerpt captured but empty (job emitted no matching "
                "lines); excerpt absence is not a job anomaly"
            ]
        )
        return "PASS", list(note), dropped
    return verdict, kept, dropped


def _guard_in_band_duration(
    packet: dict, verdict: str, reasons: list[str]
) -> tuple[str, list[str], int]:
    """Post-verdict boundary: strip duration-citing reasons when the band is clear.

    The deterministic `out_of_band` result is the only sanctioned duration
    signal. When `band_assessment` is ``in_band`` the run is within the
    tolerance-and-floor band, so any duration-citing model reason is a false
    positive. If stripping those reasons empties an ANOMALY or FAIL verdict,
    downgrade to PASS with a sanitized note. A PASS verdict whose remaining
    reasons all cite duration is also sanitized so it no longer cites an
    anomaly the deterministic band already cleared.

    Returns ``(verdict, reasons, dropped_count)``.
    """
    if packet.get("band_assessment") != "in_band":
        return verdict, reasons, 0

    original = list(reasons or [])
    filtered = [r for r in original if not _DURATION_REASON_RE.search(r)]
    if not filtered:
        if not original:
            return verdict, original, 0
        return "PASS", _PASS_NOTE, len(original)
    if len(filtered) == len(original):
        return verdict, original, 0
    return verdict, filtered, len(original) - len(filtered)


def _validate_reasons(packet: dict, verdict: str, reasons: list) -> tuple[str, list[str], int]:
    """Drop reasons contradicted by the packet; downgrade if none survive.

    Both ANOMALY and FAIL are downgraded to PASS when every model reason is
    rejected. FAIL triggers a critical-severity alert in sampler.py, so
    leaving a fabricated FAIL un-downgraded is more consequential than a
    fabricated ANOMALY -- consistent with _guard_in_band_duration, which
    already downgrades both for its narrower duration check.
    """
    kept: list[str] = []
    rejected = 0
    for r in reasons:
        reason = str(r)[:300]
        if _reason_contradicts(packet, reason):
            rejected += 1
            continue
        kept.append(reason)
    if verdict in ("ANOMALY", "FAIL") and not kept:
        return "PASS", kept, rejected
    return verdict, kept, rejected


def _guard_non_attributable_db_delta(
    packet: dict, verdict: str, reasons: list[str]
) -> tuple[str, list[str], int]:
    """Post-verdict boundary: drop db_delta reasons when the delta is not attributable.

    When ``db_delta_attributable`` is false, the run's ``db_delta`` is a
    database-wide counter diff that overlapped at least one other
    concurrently-running job, so any counter movement may belong to a
    sibling run. Verdict reasons citing a db_delta counter (or the db_delta
    field itself) are fabricated against this run and are dropped. If
    stripping those reasons empties an ANOMALY or FAIL verdict, downgrade
    to PASS -- the remaining evidence no longer supports escalation.

    Returns ``(verdict, reasons, dropped_count)``. A no-op (returns the
    inputs unchanged with 0 dropped) when the delta is attributable. On this
    host every caller today leaves ``concurrent_run_ids`` empty (see
    checkpoint_packet.py's module docstring), so this guard is a no-op in
    practice until a caller populates it -- its branch logic is unchanged so
    that fact stays a caller property, not a hand-edited-out code path.
    """
    if packet.get("db_delta_attributable", True):
        return verdict, reasons, 0
    kept: list[str] = []
    dropped = 0
    for reason in reasons:
        if _reason_cites_db_delta_counter(reason):
            dropped += 1
            continue
        kept.append(reason)
    if not kept and verdict in ("ANOMALY", "FAIL"):
        return "PASS", kept, dropped
    return verdict, kept, dropped


def _guard_new_row_backlog(
    packet: dict, verdict: str, reasons: list[str]
) -> tuple[str, list[str], int]:
    """Post-verdict boundary: drop reasons citing only new-row-bounded counters.

    A decrease-direction counter (``classification_null``, ``missing_jd_full``)
    whose increase is fully bounded by the run's new-row growth is labelled
    ``pending_from_new_rows_N`` in ``db_delta_summary``: the
    movement is the arithmetic consequence of newly inserted rows, not backlog
    accumulation (issue #1893). A verdict reason that cites only such counters
    (and no counter labelled ``worsened_by_*`` / ``improved_by_*``) is a false
    escalation against ingestion work the run is supposed to perform. If
    stripping those reasons empties an ANOMALY or FAIL verdict, downgrade to
    PASS -- the remaining evidence no longer supports escalation.

    A reason that also cites a genuinely-worsened counter (partial-excess
    backlog) or a non-db_delta signal is kept; only reasons whose db_delta
    counter citations are all fully-bounded are dropped.

    Returns ``(verdict, reasons, dropped_count)``. A no-op (returns the inputs
    unchanged with 0 dropped) when no counter is ``pending_from_new_rows_N``.
    """
    summary = packet.get("db_delta_summary") or _db_delta_summary(
        packet.get("db_delta"),
        packet.get("db_delta_tracked"),
        attributable=packet.get("db_delta_attributable", True),
    )
    by_counter = summary.get("by_counter", {})
    pending = {
        key
        for key, info in by_counter.items()
        if (info.get("label") or "").startswith("pending_from_new_rows_")
    }
    if not pending:
        return verdict, reasons, 0
    kept: list[str] = []
    dropped = 0
    for reason in reasons:
        cited = {key for key in _DB_DELTA_COUNTER_KEYS if _counter_mentioned(reason, key)}
        # Drop only when the reason cites at least one bounded counter and every
        # db_delta counter it cites is fully bounded (no genuine worsened/improved
        # counter alongside). A reason citing no db_delta counter at all is left
        # for the other guards / the model.
        if cited and cited <= pending:
            dropped += 1
            continue
        kept.append(reason)
    if not kept and verdict in ("ANOMALY", "FAIL"):
        return "PASS", kept, dropped
    return verdict, kept, dropped


def _guard_ambiguous_only_evidence(
    packet: dict, verdict: str, reasons: list[str]
) -> tuple[str, list[str]]:
    """Post-verdict boundary: an ambiguous-only evidence set is non-escalating.

    When the packet carries no run-attributed anomaly signal -- empty
    ``signature_hits``, ``disposition: completed``, ``band_assessment:
    in_band``, no error, no worsened db_delta counters -- and the only
    anomaly-adjacent evidence is ``shared_signature_hits`` entries marked
    ``attribution: "ambiguous"``, an ANOMALY verdict is a false escalation.
    The ambiguous hits belong to a concurrently-running job or to an
    unresolvable overlap; they are recorded as context, not as a verdict
    basis. Downgrade to PASS with a note (issue #1618).

    If any structural anomaly evidence is present (own signature hits, a
    non-null error, a worsened db_delta counter, or a non-ambiguous shared
    hit), the guard does not fire -- the model may have a legitimate basis.
    """
    if verdict != "ANOMALY":
        return verdict, reasons
    if packet.get("disposition") != "completed":
        return verdict, reasons
    if packet.get("band_assessment") != "in_band":
        return verdict, reasons
    if packet.get("signature_hits"):
        return verdict, reasons
    if packet.get("error"):
        return verdict, reasons
    shared = packet.get("shared_signature_hits") or []
    ambiguous = [h for h in shared if h.get("attribution") == "ambiguous"]
    if not ambiguous:
        return verdict, reasons
    summary = packet.get("db_delta_summary") or _db_delta_summary(
        packet.get("db_delta"),
        packet.get("db_delta_tracked"),
        attributable=packet.get("db_delta_attributable", True),
    )
    _improved, worsened = _improved_and_worsened_counters(summary)
    if worsened:
        return verdict, reasons
    return "PASS", list(_AMBIGUOUS_ONLY_NOTE)

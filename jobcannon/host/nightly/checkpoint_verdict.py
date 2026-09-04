"""ADAPTED from job_finder/web/nightly_monitor/_checkpoint.py (verdict half)
@ e1f47695b07f928e6c91cc64767c97a99645d68f (private job-cannon).
Ledger L-0471.

Checkpoint verdict: given an evidence packet from checkpoint_packet.py
(the other half of this port's file split), call the injected verdict
model and run the packet-falsification guard chain over its answer.

One forced-FAIL rule runs BEFORE any model call: disposition=failed (the
authoritative per-run signal) is a FAIL regardless of model opinion. A
fail-severity signature is NOT forced -- hits are matched over the whole
tick's log window and carry no run_id, so forcing FAIL would blame every
job sharing the tick with another job's failure line; the hits go into the
packet and the attribution-aware model adjudicates. This is the private
implementation's own rule, stated in its docstring, and it is what this
port preserves byte-identical -- disposition=failed only. Otherwise the
verdict comes from the injected ``call_model`` at workload tier "quick".
Unparseable model output => ANOMALY; verdict-call failures (missing
call_model, provider/transport/cascade exhausted) => VERDICT_UNAVAILABLE --
both are fail-safe and not alarming, but only the latter carries the
infra-failure signal so morning review can separate job anomalies from
scorer outages (issue #1402).

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
# jd_full_loss_excess (private's second forced rule, ANOMALY on a
# jd_full-loss invariant violation) is DROPPED, not ported: it imports
# `job_finder.web.run_events.jd_full_loss_excess`, a module outside
# nightly_monitor/ with no host analog, and it operates on `db_delta`,
# which no hosted caller populates yet (see checkpoint_packet.py's module
# docstring). A stub that always returns 0 would look implemented when it
# is not, so the branch is absent rather than inert. Forced FAIL here is
# scoped to exactly what the private code's own docstring specifies: only
# disposition=failed is forced. A fail-severity signature is adjudicated
# by the model like any other packet evidence, matching the private
# implementation -- FAIL escalation to a recorded health-log ERROR is a
# sampler-level concern, not this function's internal forcing rule.

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
whatever the packet carries and diffs clean against the private original
except for the two seams noted above (call_model injection, jd_full_loss_excess
removal) and the success_count_keys config source (below).
"""

from __future__ import annotations

import ast
import functools
import json
import logging
import re
from collections.abc import Callable, Collection
from typing import Any

from jobcannon.host.nightly.checkpoint_packet import (
    LOG_EXCERPT_STATUS_CAPTURE_UNAVAILABLE,
    LOG_EXCERPT_STATUS_CAPTURED_EMPTY,
    _DB_DELTA_COUNTER_KEYS,
    _db_delta_summary,
)
from jobcannon.host.nightly.config import (
    DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS,
    nightly_monitor_config,
)

logger = logging.getLogger(__name__)

_VALID_VERDICTS = ("PASS", "ANOMALY", "FAIL")
VERDICT_UNAVAILABLE = "VERDICT_UNAVAILABLE"

# Matches reasons that re-litigate duration despite the deterministic banding.
#
# ``longer`` and ``shorter`` only count when they introduce a comparison
# (``longer than`` / ``shorter than``), so non-duration phrases like
# ``no longer emitting events`` are not stripped. A bare numeric ``<n>s``
# token is gated on a real duration keyword in the same reason, so HTTP
# status plurals (``HTTP 429s``) and similar false positives survive.
_DURATION_KEYWORDS = (
    r"duration|p90|p10|percentile|baseline|band|out[_\s]of[_\s]band|"
    r"tolerance|floor|fast|slow|seconds|"
    r"(?:longer|shorter)(?:\s+|-)than"
)
_DURATION_REASON_RE = re.compile(
    r"\b(?:" + _DURATION_KEYWORDS + r")\b|"
    r"\b\d+(?:\.\d+)?s\b(?=.*\b(?:" + _DURATION_KEYWORDS + r")\b)",
    re.IGNORECASE,
)

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "ANOMALY", "FAIL"]},
        "reasons": {"type": "array", "items": {"type": "string", "maxLength": 300}},
    },
    "required": ["verdict", "reasons"],
}

# Regexes used to detect a success/work token in result/log_excerpt so that a
# "no work" db_delta reason can be contradicted by the packet's own evidence.
_STATUS_SUCCESS_RE = re.compile(r"\bstatus\s*[:=]\s*['\"]?success\b", re.IGNORECASE)
_COMPLETE_RE = re.compile(r"\bcomplete:\s*", re.IGNORECASE)


@functools.lru_cache(maxsize=128)
def _success_count_re(success_keys: tuple[str, ...]) -> re.Pattern:
    """Build a regex for ``key=N`` positive counts after a ``complete:`` marker.

    Only tokens in ``success_keys`` count; the review finding is that a bare
    positive integer after ``complete:`` (e.g. ``failed=1`` or ``skipped=8``)
    should not be treated as success.
    """
    if not success_keys:
        success_keys = tuple(sorted(DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS))
    keys = sorted(success_keys)
    pattern = r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")\s*=\s*[1-9]\d*\b"
    return re.compile(pattern, re.IGNORECASE)


# Phrases that mark a db_delta reason as a "no work performed" assertion.
# These are used only for post-return suppression; the primary guard is the
# db_delta_summary + db_delta_tracked prompt information.
_NO_WORK_PHRASES = (
    "no work",
    "all zero",
    "all-zero",
    "all zeros",
    "did no work",
    "did nothing",
    "no activity",
    "confirms no",
    "not performed",
    "no-op",
    "noop",
    "no operation",
    "zero delta",
    "zero change",
)

_NO_WORK_PHRASES_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(p) for p in sorted(_NO_WORK_PHRASES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)

_DB_DELTA_REF_RE = re.compile(r"\bdb[ _]delta\b", re.IGNORECASE)

# Negative-framing tokens that, when they appear near an improved counter,
# indicate the model is treating that improvement as a defect. Tokens like
# "anomalous" are deliberately excluded so legitimate magnitude-anomaly reasons
# are not suppressed.
_NEGATIVE_FRAMING_TOKENS = (
    "data loss",
    "data inconsistency",
    "inconsistent",
    "defect",
    "damage",
    "error",
    "errors",
    "failure",
    "failed",
    "problem",
    "corrupt",
    "corruption",
    "unhealthy",
    "stale",
    "wrong",
    "invalid",
    "loss",
    "lost",
    "negative",
    "break",
    "breakage",
    "degraded",
    "critical",
    "alarm",
    "risk",
    "concern",
)

_NEGATIVE_FRAMING_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(t) for t in sorted(_NEGATIVE_FRAMING_TOKENS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)

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


def _counter_variants(counter: str) -> list[str]:
    """Return the text variants of a counter key the model might use."""
    counter = counter.lower()
    variants = {counter, counter.replace("_", " ")}
    parts = counter.split("_")
    if len(parts) > 1:
        variants.add("_".join(parts))
        variants.add("_".join(parts[-2:]))
        variants.add(" ".join(parts))
        variants.add(" ".join(parts[-2:]))
    return [v for v in variants if v]


@functools.cache
def _counter_variant_re(counter: str) -> re.Pattern:
    """Compiled regex that matches any variant of ``counter`` as whole tokens."""
    variants = _counter_variants(counter)
    pattern = (
        r"(?<!\w)(?:"
        + "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
        + r")(?!\w)"
    )
    return re.compile(pattern, re.IGNORECASE)


def _counter_mentioned(reason: str, counter: str) -> bool:
    """True when ``reason`` text references ``counter`` (with or without underscores)."""
    return bool(_counter_variant_re(counter).search(reason))


def _improved_and_worsened_counters(summary: dict) -> tuple[set[str], set[str]]:
    """Return the sets of counter keys whose labels are improved/worsened."""
    improved: set[str] = set()
    worsened: set[str] = set()
    for key, info in summary.get("by_counter", {}).items():
        label = info.get("label") or ""
        if label.startswith("improved_"):
            improved.add(key)
        elif label.startswith("worsened_"):
            worsened.add(key)
    return improved, worsened


def _frames_as_defect(reason: str, counter: str, summary: dict) -> bool:
    """True when the reason treats a counter improvement as a defect."""
    text = reason.lower()
    pattern = _counter_variant_re(counter)
    for match in pattern.finditer(text):
        start = match.start()
        end = match.end()
        window = text[max(0, start - 50) : end + 50]
        # A literal negative number near the counter means the model is reasoning
        # from the raw signed delta instead of the precomputed summary label.
        if re.search(r"(?:^|\W)-[1-9]\d*\b", window):
            return True
        if _NEGATIVE_FRAMING_RE.search(window):
            return True
    return False


def _is_improvement_reason(reason: str, summary: dict) -> bool:
    """A reason that frames a *decrease*-direction counter improvement as a defect.

    Increase-direction counter improvements (total_jobs, first_seen_today) are
    not suppressed here; a large increase can be a legitimate magnitude anomaly
    and should not be masked as a sign misread.
    """
    improved, worsened = _improved_and_worsened_counters(summary)
    if not improved:
        return False
    # If the reason also mentions a worsened counter, it has a legitimate
    # unhealthy signal and should not be reduced to an improvement misread.
    if any(_counter_mentioned(reason, k) for k in worsened):
        return False
    for counter in improved:
        if not _counter_mentioned(reason, counter):
            continue
        direction = summary["by_counter"][counter].get("improvement_direction")
        if direction != "decrease":
            # total_jobs / first_seen_today increases are intentionally left alone.
            continue
        if _frames_as_defect(reason, counter, summary):
            return True
    return False


def _is_no_work_phrase(reason: str) -> bool:
    """A reason that contains an explicit "no work performed" phrase."""
    return bool(_NO_WORK_PHRASES_RE.search(reason.lower()))


def _is_no_work_reason(reason: str) -> bool:
    """A reason that treats an all-zero/untracked db_delta as 'no work performed'."""
    text = reason.lower()
    if _DB_DELTA_REF_RE.search(text):
        return True
    return _is_no_work_phrase(reason)


def _has_success_excerpt(text: str | None, success_keys: Collection[str]) -> bool:
    """Return True when ``text`` contains a success/work-completed token."""
    if not text:
        return False
    if _STATUS_SUCCESS_RE.search(text):
        return True
    if not success_keys:
        success_keys = DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS
    positive_success = _success_count_re(tuple(sorted(success_keys)))
    for match in _COMPLETE_RE.finditer(text):
        tail = text[match.end() :]
        # Only look at the same sentence/line after the ``complete:`` marker.
        snippet = re.split(r"[.!?]|\n", tail, maxsplit=1)[0]
        if positive_success.search(snippet):
            return True
    return False


def _packet_has_success(packet: dict, success_keys: Collection[str]) -> bool:
    """Completed, error-free run with a success token in its result or log excerpt."""
    if packet.get("disposition") != "completed":
        return False
    if packet.get("error"):
        return False
    result = packet.get("result")
    result_text = result if isinstance(result, str) else (str(result) if result else "")
    text = " ".join([result_text, packet.get("log_excerpt") or ""])
    return _has_success_excerpt(text, success_keys)


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

# Reasons whose sole content is that the log excerpt is empty/missing/absent.
# These are dropped when log_excerpt_status is capture_unavailable or
# captured_empty (issue #2013): the absence of log lines is an
# evidence-availability caveat, not a job anomaly. A reason that cites
# specific content *in* the excerpt (e.g. "database is locked in
# log_excerpt") does not match because it references a present string, not
# an absence.
_LOG_EXCERPT_REF_RE = re.compile(
    r"\blog[ _]excerpt\b|\bjob[\s-]scoped\s+log\b|\blog\s+(?:content|lines|evidence)\b",
    re.IGNORECASE,
)
_EXCERPT_ABSENCE_RE = re.compile(
    r"\b(?:"
    r"empty|emptied|missing|absent|not\s+provided|unavailable|"
    r"not\s+captured|not\s+found|no\s+matching|lack\s+of|lacking|"
    r"nothing\s+(?:in|from|to)|"
    # "no <qualifier> <noun>": the qualifier may be multi-word and
    # hyphenated (e.g. "No job-scoped log content ..."), so allow up to
    # four intervening word tokens between "no" and the absence noun
    # (issue #2013: the prior single-\w* form missed the issue's own
    # quoted manufactured-verdict example).
    r"no\s+(?:[\w-]+\s+){0,4}(?:log|excerpt|content|lines|evidence)"
    r")\b",
    re.IGNORECASE,
)


def _is_excerpt_absence_reason(reason: str) -> bool:
    """True when a reason's sole content is the absence of a log excerpt.

    Matches reasons like "No log excerpt provided for analysis", "Log excerpt
    is empty, unable to verify job-specific log evidence", "No job-scoped log
    content to confirm expected activity". Does NOT match a reason that cites
    specific content found in the excerpt (e.g. "database is locked in
    log_excerpt") -- that references a present string, not an absence.
    """
    if not _LOG_EXCERPT_REF_RE.search(reason):
        return False
    return bool(_EXCERPT_ABSENCE_RE.search(reason))


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


# --- Extra reason validation: reject fabricated no-work and counter claims.

_OUT_OF_BAND_ASSERTION_RE = re.compile(
    r"\b(?:out[_ ]of[_ ]band|out_of_band)\s*(?:[:=]\s*[\"']?|(?:is|are)\s+[\"']?)?"
    r"(fast|slow|in[_-]?band|in\sband|null|none)"
    r"[\"']?\b",
    re.IGNORECASE,
)

_LONGER_RE = re.compile(
    r"\b(?:longer|long|greater|exceeds|exceeded|above|more\sthan|>)\b",
    re.IGNORECASE,
)
_SHORTER_RE = re.compile(
    r"\b(?:shorter|short|less|below|under|lower|smaller|<)\b",
    re.IGNORECASE,
)
_SHORT_WORDS = re.compile(r"\b(?:short|shorter|fast)\b", re.IGNORECASE)
_LONG_WORDS = re.compile(r"\b(?:long|longer|slow)\b", re.IGNORECASE)
_UNUSUAL_RE = re.compile(
    r"\b(?:unusual|unusually|anomal(?:y|ous)|unexpected|strange|abnormal)\b",
    re.IGNORECASE,
)
_NO_WORK_RE = re.compile(
    r"\b(?:no\s+work|no\s+changes|no\s+jobs|no\s+backlog|did\s+no\s+work|"
    r"suggesting\s+no\s+work|indicates\s+no\s+changes)\b",
    re.IGNORECASE,
)


def _parse_baseline(packet: dict) -> tuple[float | None, float | None]:
    band = packet.get("baseline") or {}
    if band.get("status") != "ok":
        return None, None
    p10 = band.get("p10")
    p90 = band.get("p90")
    if not isinstance(p10, (int, float)) or not isinstance(p90, (int, float)):
        return None, None
    return float(p10), float(p90)


def _result_value_nonzero(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _has_evidence_of_work(packet: dict) -> bool:
    """Return True when the packet contains concrete evidence that the job did work.

    A db_delta with any non-zero change is authoritative. The job's result is
    secondary evidence, but only when it carries a non-trivial, non-zero value.
    """
    db_delta = packet.get("db_delta") or {}
    if isinstance(db_delta, dict) and any(
        v for v in db_delta.values() if isinstance(v, (int, float, bool)) and v
    ):
        return True

    result = packet.get("result")
    if result is None:
        return False
    if isinstance(result, str):
        # Some callers pass a pre-stringified repr of the result mapping
        # (e.g. "{'jobs_found': 0, 'jobs_new': 0}"). Treating any non-empty
        # string as truthy would misread a stringified all-zero result as
        # evidence of work, so parse dict/list-shaped strings back into their
        # structure. A dict/list-shaped string that fails to parse cannot be
        # verified to carry non-zero values, and the whole point of parsing
        # is to avoid treating an all-zero result as work -- so treat an
        # unparseable dict/list-shaped string as no evidence rather than
        # falling back to the bare-string heuristic, which would over-reject
        # a genuine no-work reason. The bare-string heuristic still applies
        # to plain (non-structured) result strings, which is the case it was
        # designed for.
        stripped = result.strip()
        if stripped[:1] in ("{", "["):
            try:
                result = ast.literal_eval(stripped)
            except (ValueError, SyntaxError):
                return False
    if isinstance(result, dict):
        return any(_result_value_nonzero(v) for v in result.values())
    if isinstance(result, (list, tuple, set)):
        return any(_result_value_nonzero(v) for v in result)
    return _result_value_nonzero(result)


def _reason_contradicts(packet: dict, reason: str) -> bool:
    """Return True if a model reason makes a claim the packet falsifies."""
    duration = packet.get("duration_s")
    band_assessment = packet.get("band_assessment")
    out_of_band = packet.get("out_of_band")
    p10, p90 = _parse_baseline(packet)

    # 1. Explicit out_of_band / duration assertion.
    m = _OUT_OF_BAND_ASSERTION_RE.search(reason)
    if m:
        asserted = m.group(1).lower().replace("-", "_").replace(" ", "_")
        if asserted in ("in_band", "inband", "null", "none"):
            if out_of_band is not None:
                return True
        elif asserted != out_of_band:
            return True

    # 2. Numeric p10/p90 claims when the band is usable.
    if band_assessment != "insufficient_history" and isinstance(duration, (int, float)):
        if p90 is not None and re.search(r"\bp90\b", reason, re.IGNORECASE):
            if _LONGER_RE.search(reason) and duration <= p90:
                return True
            if _SHORTER_RE.search(reason) and duration >= p90:
                return True
        if p10 is not None and re.search(r"\bp10\b", reason, re.IGNORECASE):
            if _SHORTER_RE.search(reason) and duration >= p10:
                return True
            if _LONGER_RE.search(reason) and duration <= p10:
                return True

    # 3. Generic short/fast or long/slow claims when the run is out of band.
    if out_of_band in ("fast", "slow"):
        if _SHORT_WORDS.search(reason) and out_of_band != "fast":
            return True
        if _LONG_WORDS.search(reason) and out_of_band != "slow":
            return True

    # 4. db_delta no-work claims contradicted by evidence of work.
    if _NO_WORK_RE.search(reason) and _has_evidence_of_work(packet):
        return True

    # 5. Negative progress-counter decreases called unusual.
    db_delta = packet.get("db_delta") or {}
    if not isinstance(db_delta, dict):
        return False
    for counter, delta in db_delta.items():
        if counter in ("total_jobs", "first_seen_today"):
            continue
        if not isinstance(delta, (int, float)) or delta >= 0:
            continue
        if counter.lower() not in reason.lower():
            continue
        if _UNUSUAL_RE.search(reason):
            return True
        if re.search(
            r"\b(?:decrease|decreased|dropped|drop|down|negative)\b",
            reason,
            re.IGNORECASE,
        ) and _UNUSUAL_RE.search(reason):
            return True

    return False


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


def _reason_cites_db_delta_counter(reason: str) -> bool:
    """True when a reason references a db_delta counter or the db_delta field.

    Used by the non-attributable guard to drop verdict reasons that reason
    from a counter movement the run did not necessarily cause (issue #1734).
    """
    if _DB_DELTA_REF_RE.search(reason):
        return True
    return any(_counter_mentioned(reason, k) for k in _DB_DELTA_COUNTER_KEYS)


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


def checkpoint_verdict(
    packet: dict,
    *,
    call_model: Callable[..., Any] | None = None,
    conn: Any = None,
    config: dict | None = None,
) -> dict:
    """Forced-FAIL only on the authoritative per-run signal (disposition=failed).

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

    Every return path carries ``rejected_reasons``: the count of model-supplied
    reasons dropped by the deterministic post-verdict guards as fabricated
    (falsified against the packet), defaulting to 0 where no model reasons
    reached validation (forced FAIL, unparseable verdict, verdict-call failure).

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

"""ADAPTED from job_finder/web/nightly_monitor/_checkpoint.py (verdict half)
@ 5221e7e6518c67e62996219e1c7c56747f10dd8f (private job-cannon).
Ledger L-0471, L-0585, L-0607.

Pure packet/reason predicates for the checkpoint-verdict guard chain:
db_delta counter-name matching, no-work/improvement phrase detection,
success-token scanning, log-excerpt-absence detection, and the per-reason
packet-falsification check (_reason_contradicts). Split out of
checkpoint_verdict.py under the module-size convention (issue #421) --
every function and regex moved verbatim; see checkpoint_verdict.py's
module docstring for this port's provenance and the guard-chain fidelity
anchor these predicates serve.
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Collection

from jobcannon.host.nightly.checkpoint_packet import _DB_DELTA_COUNTER_KEYS
from jobcannon.host.nightly.config import DEFAULT_NIGHTLY_SUCCESS_COUNT_KEYS

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


def _reason_cites_db_delta_counter(reason: str) -> bool:
    """True when a reason references a db_delta counter or the db_delta field.

    Used by the non-attributable guard to drop verdict reasons that reason
    from a counter movement the run did not necessarily cause (issue #1734).
    """
    if _DB_DELTA_REF_RE.search(reason):
        return True
    return any(_counter_mentioned(reason, k) for k in _DB_DELTA_COUNTER_KEYS)

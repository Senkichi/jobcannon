"""ADAPTED from job_finder/web/nightly_monitor/_morning.py (run_audit_stage
and its private helpers) @ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8
(private job-cannon). Ledger L-0387.

Morning audit stage: sample a bounded, random cohort of recently-scored
postings, dispatch each batch to the model for adversarial re-scoring, and
ingest the verdicts into score_audits.

# PORT-SEAM: private spawned a batch of tool-enabled `claude -p` sessions
# (ThreadPoolExecutor, up to `parallel_sessions` concurrent, each in its own
# tempdir, each Reading a batch input file and printing a JSON array to
# stdout that had to be fenced/prose-tolerant parsed) with typed session-
# limit / not-logged-in short-circuiting (#1487, #1799) and a per-item
# poison-isolation fallback (#1404) on a failed batch. All of that DIES,
# replaced by jobcannon.host.nightly.model_session.run_structured_session
# (one jobcannon.host.model_provider.call_model request per batch, schema-
# validated by the provider adapter itself -- no fenced-JSON-in-prose
# parsing needed). Sequential dispatch replaces the thread pool: private's
# concurrency existed to hide OS-subprocess spawn latency, which does not
# apply to a single hosted API call, and this is a once-nightly background
# job with no request waiting on it. The typed session-limit/not-logged-in
# classification is replaced by SessionResult.unavailable (see
# model_session.py's own PORT-SEAM) -- on the FIRST unavailable batch result
# (including a dispatcher that was never wired: call_model is None), the
# remaining not-yet-attempted batches are abandoned WITHOUT recording
# `skipped` rows for their jobs, mirroring private's own _record_skip
# docstring invariant ("a provider outage still never fabricates a skipped
# row in the first place"). A batch that DID get a real dispatch and still
# failed schema validation after `max_batch_retries` retries records every
# job in that batch as `skipped` -- private isolated the single poison item
# via a second real per-job dispatch layer; this port accepts poisoning up
# to `batch_size` candidates for one night instead of adding that isolation
# layer, since candidates are now a random sample (not a fixed top-N), so a
# poisoned batch is simply re-rolled on a later night, bounded as before by
# `max_skip_attempts` (#1806).
#
# The location-policy-verdict branch (`location_policy_verdict_json`,
# `effective_location_fit`, the #1484/#1578 location-only-dispute
# reclassification) is DROPPED, not ported: jobcannon.db._score_audits
# .select_audit_candidates already does not return those fields (see that
# module's own PORT-SEAM -- "no host postings.location_policy_verdict
# column to feed it"), so the branch would be permanently dead code here.
# location_fit is therefore always an ordinary audited axis on this host.
#
# `set_job_flag` (private's genuine-dispute side effect) is DROPPED: no
# per-posting "flagged" concept exists on this host (see
# jobcannon/db/_persistence.py's L-0073 PORT-SEAM). The dispute itself
# stays fully durable via `record_score_audit(verdict="dispute", ...)`.
#
# Batch input files (private wrote one JSON file per batch under an
# artifact dir the session Read) are DROPPED along with the file-reading
# session: batches are built in memory and embedded directly in the
# call_model prompt. Forensic writes (`_write_batch_failure`,
# `_write_audit_aborted`) are replaced by `record_scan_health` rows;
# `_write_audit_disputes` (a JSON export for the morning reviewer to read
# without DB access) is dropped -- run_audit_stage's return dict carries
# `disputes_detail` directly (already assembled in-process during
# ingestion), and review_stage.py consumes that list instead of re-querying
# and re-joining score_audits against postings for the same information.
#
# `random.sample` under the hard JC_NIGHTLY_AUDIT_MAX_JOBS ceiling (design
# item 5): `select_audit_candidates` is called with a generous pool cap
# (well above any realistic nightly cohort) so the ceiling is applied AFTER
# eligibility filtering, over the full eligible pool -- capping inside
# `select_audit_candidates` first would sample only from its axis-sum-
# descending top slice, defeating the poison-rotation relief the random
# sample exists to provide. `rng` is an injected `random.Random | None`
# (default the `random` module) purely for test determinism (design note
# §6 Q6).
#
# Private's `model` config value ("sonnet") is DROPPED: this host routes by
# workload tier (jobcannon.host.model_provider._VALID_WORKLOADS), not a
# model name, and "score" is the tier this audit maps to. No
# JC_NIGHTLY_AUDIT_MODEL env key is added -- it would be config for a knob
# this host has no way to honor.
"""

from __future__ import annotations

import json
import logging
import random
from typing import Any, Callable

from jobcannon.db import _score_audits
from jobcannon.engine.constants import SUB_SCORE_KEYS
from jobcannon.host.health_recorder import record_scan_health
from jobcannon.host.nightly.model_session import run_structured_session

logger = logging.getLogger(__name__)

_VALID_AUDIT_VERDICTS = ("agree", "dispute")
_VALID_JD_QUALITY_FLAGS = ("garbage", "truncated", "wrong_language")
_VALID_JD_CONTENT_VERDICTS = ("clean", "ambiguous", "reject")
_NOTES_CLIP = 400
_JD_FULL_TRUNCATION_MARKER = "...[truncated for audit budget]"
_AUDIT_MAX_TOKENS = 4096
# Generous cap on the eligible pool fetched before sampling -- not the real
# sampling ceiling (that is audit_cfg["max_jobs"] / JC_NIGHTLY_AUDIT_MAX_JOBS).
# Bounds a pathological query result; the WHERE clause (lookback_days,
# sub_scores_json IS NOT NULL) already keeps a realistic nightly pool small.
_ELIGIBLE_POOL_CAP = 10_000

AUDIT_SYSTEM_PROMPT = """You are an adversarial scoring auditor for a personal job-search pipeline.
Production is a six-axis ordinal rubric (1-5 per axis): title_fit,
location_fit, comp_fit, domain_match, seniority_match, skills_match.
Your job is scoring QA: catch scores that are wrong at the axis level, and
JD text that should never have been scored at all.

You will be given a JSON object {"jobs": [...]}. Each job has: dedup_key,
title, company, location, jd_full, sub_scores_json, axis_sum.

For each job, in order:
1. Read jd_full.
2. Independently derive all six axes BEFORE looking at the production
   values in sub_scores_json. Do not anchor on them.
3. Compare your audited axes to production's:
   - verdict "agree" = every audited axis within +/-1 of production.
   - verdict "dispute" = ANY of: an audited axis >= 2 off; jd_full is
     garbage / truncated / wrong-language content that slipped the content
     contract; a seniority or other hard profile-constraint leak.
4. axis_deltas = your value minus production's, per axis; include ONLY
   axes with a nonzero delta.
5. jd_quality_flag: null unless the JD text itself is defective -- then one
   of "garbage" | "truncated" | "wrong_language".

Return STRICT JSON matching the given schema: one entry per job, in any
order, every dedup_key from the input appearing exactly once. notes <= 400
characters per job."""

AUDIT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dedup_key": {"type": "string"},
                    "verdict": {"type": "string", "enum": list(_VALID_AUDIT_VERDICTS)},
                    "axis_deltas": {"type": "object"},
                    "jd_quality_flag": {"type": ["string", "null"]},
                    "notes": {"type": "string", "maxLength": _NOTES_CLIP},
                },
                "required": ["dedup_key", "verdict"],
            },
        },
    },
    "required": ["entries"],
}


def _parse_sub_scores(sub_scores_json: str | None) -> dict | None:
    """Return the sub-scores dict if it is valid JSON containing numeric axes."""
    if not sub_scores_json:
        return None
    try:
        data = json.loads(sub_scores_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_axis_deltas(raw_deltas: dict | None) -> tuple[dict[str, int], list[str]]:
    """Resolve raw model ``axis_deltas`` into a SUB_SCORE_KEYS-keyed int map.

    Returns ``(resolved, unknown_keys)``: *resolved* has exactly the
    SUB_SCORE_KEYS entries (non-numeric/absent -> 0); *unknown_keys* lists
    any keys present in *raw_deltas* that are not in SUB_SCORE_KEYS, logged
    rather than silently dropped (#1486).
    """
    raw = raw_deltas if isinstance(raw_deltas, dict) else {}
    unknown = [k for k in raw if k not in SUB_SCORE_KEYS]
    resolved: dict[str, int] = {}
    for key in SUB_SCORE_KEYS:
        d = raw.get(key, 0)
        if not isinstance(d, (int, float)) or isinstance(d, bool):
            d = 0
        resolved[key] = int(d)
    return resolved, unknown


def _build_dispute_entry(job: dict, entry: dict) -> dict:
    """Build one per-dispute record: reconstructs the auditor's absolute
    sub-scores from the axis deltas so the morning review can compare them
    to production without re-deriving anything.
    """
    current = _parse_sub_scores(job.get("sub_scores_json")) or {}
    resolved, _unknown = _resolve_axis_deltas(entry.get("axis_deltas"))
    audited_sub_scores: dict[str, int | None] = {}
    current_sub_scores: dict[str, int | None] = {}
    axis_deltas: dict[str, int] = {}
    for key in SUB_SCORE_KEYS:
        c = current.get(key)
        if not isinstance(c, (int, float)) or isinstance(c, bool):
            c = None
        d = resolved[key]
        current_sub_scores[key] = int(c) if c is not None else None
        axis_deltas[key] = d
        audited_sub_scores[key] = int(c + d) if c is not None else None
    flag = entry.get("jd_quality_flag")
    notes = entry.get("notes")
    return {
        "dedup_key": job["dedup_key"],
        "title": job.get("title"),
        "company": job.get("company"),
        "audited_sub_scores": audited_sub_scores,
        "current_sub_scores": current_sub_scores,
        "axis_deltas": axis_deltas,
        "jd_quality_flag": str(flag) if flag else None,
        "notes": str(notes)[:_NOTES_CLIP] if notes else None,
    }


def _aggregate_jd_quality_flag(summary: dict, dedup_key: str, flag: Any) -> None:
    """Count one audited row's jd_quality_flag into the summary (#1732)."""
    if not flag:
        return
    value = str(flag)
    if value not in _VALID_JD_QUALITY_FLAGS:
        logger.warning("nightly audit entry %r has unrecognized jd_quality_flag: %r", dedup_key, value)
    summary["jd_quality_flags"][value] = summary["jd_quality_flags"].get(value, 0) + 1
    summary["jd_quality_flagged"] += 1
    summary["jd_quality_flagged_keys"].append(dedup_key)


def _aggregate_jd_content_verdict(summary: dict, dedup_key: str, verdict: Any) -> None:
    """Count one audited row's recorded jd-content verdict into the summary
    (#1742). Trusted driver data (read by select_audit_candidates from the
    posting row), not LLM output.
    """
    if not verdict:
        return
    value = str(verdict)
    if value not in _VALID_JD_CONTENT_VERDICTS:
        logger.warning("nightly audit entry %r has unrecognized jd_content_verdict: %r", dedup_key, value)
    summary["jd_content_verdicts"][value] = summary["jd_content_verdicts"].get(value, 0) + 1


def _truncate_job_for_budget(job: dict, max_batch_chars: int) -> dict:
    """Return *job* with jd_full truncated if a single-job batch would
    exceed *max_batch_chars* (#1970): a single long jd_full could otherwise
    push the batch prompt so large that the model never reaches the
    production sub_scores_json that appears after it.
    """
    if len(json.dumps({"jobs": [job]})) <= max_batch_chars:
        return job
    truncated = dict(job)
    jd_full = truncated.get("jd_full") or ""
    lo, hi = 0, len(jd_full)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        truncated["jd_full"] = jd_full[:mid] + _JD_FULL_TRUNCATION_MARKER
        if len(json.dumps({"jobs": [truncated]})) <= max_batch_chars:
            lo = mid
        else:
            hi = mid - 1
    truncated["jd_full"] = jd_full[:lo] + _JD_FULL_TRUNCATION_MARKER
    return truncated


def _build_batches(candidates: list[dict], batch_size: int, max_batch_chars: int) -> list[list[dict]]:
    """Split *candidates* into batches bounded by both item count
    (*batch_size*) and serialized-size budget (*max_batch_chars*).
    """
    truncated = [_truncate_job_for_budget(job, max_batch_chars) for job in candidates]
    batches: list[list[dict]] = []
    current: list[dict] = []
    for job in truncated:
        if current:
            candidate_batch = current + [job]
            size = len(json.dumps({"jobs": candidate_batch}))
            if size > max_batch_chars or len(candidate_batch) > batch_size:
                batches.append(current)
                current = []
        current.append(job)
    if current:
        batches.append(current)
    return batches


def _new_summary() -> dict:
    return {
        "total_candidates": 0,
        "audited": 0,
        "disputes": 0,
        "malformed_entries": 0,
        "skipped": 0,
        "disagreement_rate": None,
        "failed_batches": 0,
        "total_batches": 0,
        "failed_batch_fraction": 0.0,
        "coverage_failure": False,
        "unavailable": False,
        "unavailable_reason": None,
        "jd_quality_flagged": 0,
        "jd_quality_flags": {},
        "jd_quality_flagged_keys": [],
        "jd_content_verdicts": {},
        "disputes_detail": [],
    }


def run_audit_stage(
    conn: Any,
    monitor_cfg: dict,
    *,
    call_model: Callable[..., Any] | None,
    config: dict,
    rng: random.Random | None = None,
) -> dict:
    """Sample a bounded random cohort, dispatch batches to the model, ingest
    verdicts into score_audits. Never raises.
    """
    audit_cfg = monitor_cfg["audit"]
    picker = rng if rng is not None else random
    eligible = _score_audits.select_audit_candidates(
        conn,
        score_threshold=audit_cfg["score_threshold"],
        lookback_days=audit_cfg["lookback_days"],
        max_jobs=_ELIGIBLE_POOL_CAP,
        max_skip_attempts=audit_cfg["max_skip_attempts"],
    )
    ceiling = audit_cfg["max_jobs"]
    candidates = picker.sample(eligible, min(ceiling, len(eligible))) if eligible else []

    summary = _new_summary()
    summary["total_candidates"] = len(candidates)
    if not candidates:
        return summary

    if call_model is None:
        summary["unavailable"] = True
        summary["unavailable_reason"] = "no call_model dispatcher wired (owner-tenant identity unresolved)"
        return summary

    batches = _build_batches(candidates, audit_cfg["batch_size"], audit_cfg["max_batch_input_chars"])
    summary["total_batches"] = len(batches)
    retries = audit_cfg["max_batch_retries"]

    seen_keys: set[str] = set()

    def _ingest_entries(entries: list[Any], batch_keys: dict[str, dict]) -> None:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = entry.get("dedup_key")
            verdict = entry.get("verdict")
            if key not in batch_keys or verdict not in _VALID_AUDIT_VERDICTS:
                logger.warning("nightly audit entry skipped (key=%r verdict=%r)", key, verdict)
                continue
            if key in seen_keys:
                logger.warning("nightly audit duplicate entry skipped (key=%r)", key)
                continue
            seen_keys.add(key)
            job_record = batch_keys[key]
            deltas = entry.get("axis_deltas")
            flag = entry.get("jd_quality_flag")
            notes = entry.get("notes")
            _score_audits.record_score_audit(
                conn,
                dedup_key=key,
                model="score",
                verdict=verdict,
                audited_sub_scores_json=job_record["sub_scores_json"],
                axis_deltas_json=json.dumps(deltas) if isinstance(deltas, dict) and deltas else None,
                jd_quality_flag=str(flag) if flag else None,
                notes=str(notes)[:_NOTES_CLIP] if notes else None,
            )
            summary["audited"] += 1
            _aggregate_jd_quality_flag(summary, key, flag)
            _aggregate_jd_content_verdict(summary, key, job_record.get("jd_content_verdict"))
            if verdict == "dispute":
                resolved, _unknown = _resolve_axis_deltas(deltas)
                has_non_axis_basis = bool(flag) or bool(notes)
                if not any(resolved.values()) and not has_non_axis_basis:
                    # Agrees on every axis, no flag/notes -- not a real
                    # disagreement (#1486, #1557); visible, not alarmed on.
                    summary["malformed_entries"] += 1
                    continue
                summary["disputes"] += 1
                summary["disputes_detail"].append(_build_dispute_entry(job_record, entry))

    def _record_skip(job: dict, reason: str | None) -> None:
        _score_audits.record_score_audit(
            conn,
            dedup_key=job["dedup_key"],
            model="score",
            verdict="skipped",
            audited_sub_scores_json=job["sub_scores_json"],
            axis_deltas_json=None,
            jd_quality_flag=None,
            notes=str(reason)[:_NOTES_CLIP] if reason else None,
        )
        summary["skipped"] += 1
        seen_keys.add(job["dedup_key"])

    for batch in batches:
        batch_keys = {j["dedup_key"]: j for j in batch}
        prompt = json.dumps({"jobs": batch})
        result = run_structured_session(
            tier="score",
            system=AUDIT_SYSTEM_PROMPT,
            prompt=prompt,
            output_schema=AUDIT_OUTPUT_SCHEMA,
            conn=conn,
            config=config,
            call_model=call_model,
            purpose="nightly_audit",
            max_tokens=_AUDIT_MAX_TOKENS,
        )
        attempts = 1
        while not result.ok and not result.unavailable and attempts <= retries:
            logger.warning("nightly audit batch failed on attempt %d; retrying", attempts)
            result = run_structured_session(
                tier="score",
                system=AUDIT_SYSTEM_PROMPT,
                prompt=prompt,
                output_schema=AUDIT_OUTPUT_SCHEMA,
                conn=conn,
                config=config,
                call_model=call_model,
                purpose="nightly_audit",
                max_tokens=_AUDIT_MAX_TOKENS,
            )
            attempts += 1

        if result.unavailable:
            summary["unavailable"] = True
            summary["unavailable_reason"] = result.error
            record_scan_health(
                kind="nightly_audit_aborted",
                reason=result.error,
                batch_size=len(batch),
            )
            break

        if not result.ok:
            summary["failed_batches"] += 1
            record_scan_health(
                kind="nightly_audit_batch_failed",
                reason=result.error,
                attempts=attempts,
                batch_size=len(batch),
                dedup_keys=list(batch_keys),
            )
            for job in batch:
                _record_skip(job, result.error)
            continue

        entries = (result.data or {}).get("entries")
        if not isinstance(entries, list):
            summary["failed_batches"] += 1
            record_scan_health(
                kind="nightly_audit_batch_failed",
                reason="model output missing 'entries' array",
                attempts=attempts,
                batch_size=len(batch),
                dedup_keys=list(batch_keys),
            )
            for job in batch:
                _record_skip(job, "model output missing 'entries' array")
            continue

        _ingest_entries(entries, batch_keys)

    if summary["audited"]:
        summary["disagreement_rate"] = summary["disputes"] / summary["audited"]
    if summary["total_batches"]:
        summary["failed_batch_fraction"] = summary["failed_batches"] / summary["total_batches"]

    if summary["unavailable"]:
        summary["coverage_failure"] = False
    else:
        summary["coverage_failure"] = (
            summary["total_candidates"] > 0
            and (summary["audited"] / summary["total_candidates"]) < audit_cfg["coverage_alarm_threshold"]
        ) or summary["failed_batch_fraction"] > audit_cfg["failed_batch_fraction_alarm_threshold"]

    return summary

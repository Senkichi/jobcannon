"""Unified v3.0 scoring module — single-tier ordinal rubric.

Replaces the Phase 1/2 two-tier (Haiku + Sonnet) scoring split. Emits a
JobAssessment (6 ordinal 1-5 sub-scores + 4-list rationale); classification
is Python-derived at persist time (see derive_classification in
jobcannon.engine.classification).

This module is a pure-function addition in Phase 34 Plan 1 — no in-tree
caller lands until a host wires score_and_persist_job (or equivalent)
through it; the private repo's Plan 2 orchestrator that did so is not
part of this engine port.

Routes through an injected call_model(tier="score", ...) callable (see the
score_job Args below) per CONTEXT D-09. The engine does NOT instantiate its
own provider or duplicate schema-retry/cascade logic — was
job_finder.web.model_provider.call_model in the private repo, which
inherited ~250 lines of battle-tested dispatcher behavior; the host is now
responsible for supplying an equivalent callable.

D-28 note: byte-identical determinism is not achievable on the local
Ollama + CUDA stack (non-deterministic reductions below Ollama). The
success criterion is ordinal stability — axis rankings preserved
across repeated invocations. No byte-identical test here; rescore
gates (Plan 4 G1-G4) capture the same intent via G3 correlation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from jobcannon.engine.constants import SUB_SCORE_KEYS
from jobcannon.engine.classification import JobAssessment
from jobcannon.engine.classification import _TERMINAL_ENRICHMENT_TIERS
from jobcannon.engine.jd_content_contract import JD_CONTENT_VERSION, JdVerdict
from jobcannon.engine.scoring_prompts.v3_scoring_prompt import JOB_ASSESSMENT_SCHEMA
from jobcannon.engine.scoring_types import build_comp_context

log = logging.getLogger(__name__)

# Re-homed from the private repo's providers/ollama_provider.py; provider-tuning
# default (Ollama context-window size). The engine has no provider of its own —
# this constant only feeds _derive_max_jd_chars' JD-truncation budget calc, so
# a host wiring a different provider can still leave this unchanged.
_DEFAULT_NUM_CTX = 12288

# Re-export the schema for callers that need the dispatcher-layer constant.
# This is the BASELINE schema; per-call schema for variant selection is
# resolved through _resolve_schema(config).
__all__ = [
    "JOB_ASSESSMENT_SCHEMA",
    "ScoringResult",
    "score_job",
    "scoring_precheck",
]

# Canonical sub-score keys (matches v3 prompt schema + CONTEXT D-05).
# The LLM emits these at the TOP LEVEL of the response alongside `rationale`
# and `legitimacy_note` — NOT nested under "sub_scores". Single source of truth
# is jobcannon.engine.constants.SUB_SCORE_KEYS; aliased here to keep the local name.
_SUB_SCORE_KEYS: tuple[str, ...] = SUB_SCORE_KEYS

# Token budget constants for JD truncation calculation.
# System prompt is ~2,300 tokens (rubric + field reinforcement + candidate context + fewshot).
# Response headroom is 2,048 tokens (max_tokens=2048 in score_job call).
# Conservative chars-per-token ratio: 3 chars/token (English text averages 3-4).
_SYSTEM_PROMPT_TOKENS = 2300
_RESPONSE_HEADROOM_TOKENS = 2048
_CHARS_PER_TOKEN = 3


def _derive_max_jd_chars(config: dict | None) -> int:
    """Derive JD character budget from configured num_ctx.

    The budget is calculated as:
        available_tokens = num_ctx - system_prompt_tokens - response_headroom
        max_jd_chars = available_tokens * chars_per_token

    This ensures that raising num_ctx actually extends visible JD content,
    fixing a prior silent ceiling where 24k was hardcoded regardless
    of num_ctx setting.

    Args:
        config: Application config dict. Reads providers.ollama.num_ctx
                (default: 12288).

    Returns:
        Maximum JD characters to send to the model.
    """
    if not config:
        num_ctx = _DEFAULT_NUM_CTX
    else:
        num_ctx = config.get("providers", {}).get("ollama", {}).get("num_ctx", _DEFAULT_NUM_CTX)

    available_tokens = max(0, num_ctx - _SYSTEM_PROMPT_TOKENS - _RESPONSE_HEADROOM_TOKENS)
    max_jd_chars = available_tokens * _CHARS_PER_TOKEN

    # Log the derivation at debug level for observability
    log.debug(
        "_derive_max_jd_chars: num_ctx=%d, system=%d, headroom=%d, available=%d tokens, max_jd_chars=%d",
        num_ctx,
        _SYSTEM_PROMPT_TOKENS,
        _RESPONSE_HEADROOM_TOKENS,
        available_tokens,
        max_jd_chars,
    )

    return max_jd_chars


@dataclass(frozen=True)
class ScoringResult:
    """Envelope returned by score_job(). status ∈ {"ok", "skipped", "error"}.

    - status="ok": data is a JobAssessment, provider + model are the attribution
      strings reported by the cascade.
    - status="skipped": data is None, provider/model are None, error is None —
      a precondition was not met (SCORER-05). ``reason`` names which gate fired:
        "awaiting_jd"              — jd_full absent/empty; job needs enrichment.
        "awaiting_location"        — locations_structured + location both empty
                                      and the job is still enrichable (D-7 / P3.2
                                      gate).
        "awaiting_jd_adjudication" — persisted jd_content_verdict is not CLEAN
                                      and the row is not adjudicated (D5, #1742).
    - status="error": data is None, provider/model are whatever the dispatcher
      reported if the call reached it, error is a human-readable reason.
    """

    status: str
    data: JobAssessment | None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    reason: str | None = None


def _variant_name(config: dict | None) -> str:
    """Read scoring.prompt_variant from config; default to 'baseline'."""
    if not config:
        return "baseline"
    return (config.get("scoring") or {}).get("prompt_variant") or "baseline"


def _resolve_variant_module(variant_name: str):
    """Return the prompt module for a named variant.

    'baseline' aliases the production v3_scoring_prompt module. Any other
    name is resolved as ``jobcannon.engine.scoring_prompts.variants.<name>``.
    Unknown names raise ImportError mentioning the requested variant — never
    silently fall back to baseline (silent fallback masks experiment errors).
    """
    if variant_name == "baseline":
        from jobcannon.engine.scoring_prompts import v3_scoring_prompt as mod

        return mod
    import importlib

    try:
        return importlib.import_module(f"jobcannon.engine.scoring_prompts.variants.{variant_name}")
    except ImportError as exc:
        raise ImportError(f"Unknown scoring prompt variant: {variant_name!r}") from exc


def _resolve_schema(config: dict | None) -> dict:
    """Resolve the JSON-schema dict for the variant named in config."""
    return _resolve_variant_module(_variant_name(config)).JOB_ASSESSMENT_SCHEMA


def _build_system_prompt(
    candidate_context: str,
    config: dict | None = None,
) -> str:
    """Assemble the full system prompt from the resolved variant module.

    Variant selection: ``config["scoring"]["prompt_variant"]`` picks the
    module. 'baseline' (or absent) loads ``v3_scoring_prompt``; any other
    name loads ``scoring_prompts.variants.<name>``. Each variant module
    must export V3_SCORING_PROMPT, FIELD_REINFORCEMENT, FEWSHOT_EXAMPLES,
    and JOB_ASSESSMENT_SCHEMA (V3_SCORING_PROMPT_HEADER is optional).

    Always splices candidate_context between FIELD_REINFORCEMENT and
    FEWSHOT_EXAMPLES so the model reads:
        rubric/dimensions header -> field reinforcement -> candidate context
        -> few-shot calibration examples.

    candidate_context is REQUIRED — the v3 location_fit / comp_fit / etc.
    anchors are unscorable without knowing the candidate's target locations,
    floor, and background. This engine has no orchestrator of its own (see
    module docstring); a host builds this string and passes it in, e.g. via
    ``jobcannon.host.candidate_context.build_candidate_context(profile)``
    (or ``resolve_candidate_context(conn, user_id)`` to load the profile row
    first). Tests inject a stub directly. The pre-Phase-2a no-context
    fallback was removed in this refactor — it silently produced wrong
    scores (e.g. rating an on-site Bangalore role as a 'feasible hybrid' = 4
    for a Remote/SF-only candidate) and existed only because the wiring
    across six of seven call sites had never been completed.
    """
    if not candidate_context:
        raise ValueError(
            "_build_system_prompt: candidate_context is required. "
            "Build it via jobcannon.host.candidate_context.build_candidate_context(profile), "
            "or pass an explicit test stub."
        )
    mod = _resolve_variant_module(_variant_name(config))
    header = getattr(mod, "V3_SCORING_PROMPT_HEADER", None) or mod.V3_SCORING_PROMPT
    field_reinforcement = mod.FIELD_REINFORCEMENT
    fewshot = mod.FEWSHOT_EXAMPLES

    return header + "\n\n" + field_reinforcement + "\n\n" + candidate_context + "\n\n" + fewshot


def _build_user_message(job: dict, config: dict | None = None) -> str:
    """User-side assembly: title + company + location + comp + JD.

    Keeps the request shape stable across candidates so the LLM sees a
    consistent prompt.

    - JD: the cleaned ``jd_full`` is sent WHOLE. Real JD prose is short and
      the local model has ample context headroom, so truncation is almost
      never needed — and a silent truncation that drops the requirements /
      location / compensation sections is far worse than a slightly larger
      prompt. As a pure safety net against a pathological / poorly-cleaned
      posting, anything past the derived max_jd_chars is hard-truncated WITH a
      logged warning (never a silent section-drop). The max is derived from
      num_ctx, so raising num_ctx actually extends visible JD
      content. Removing superfluous content properly is an upstream-extraction
      job (Layer 2).
    - Compensation: the salary_min/max range is always shown; richer
      ATS-sourced comp (equity / bonus / tier summary from comp_data_json)
      is appended via ``build_comp_context`` when present.

    Args:
        job: Job row dict.
        config: Application config dict (used to derive max_jd_chars from num_ctx).

    Returns:
        Formatted user message string.
    """
    title = job.get("title") or "(no title)"
    company = job.get("company_canonical") or job.get("company") or "(no company)"
    location = job.get("location") or "(no location)"
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    comp = ""
    if salary_min or salary_max:
        comp = f"\nSalary: {salary_min or '?'}-{salary_max or '?'}"
    comp_extra = build_comp_context(job)
    if comp_extra:
        comp += f"\nCompensation: {comp_extra}"

    jd_full = job.get("jd_full") or ""
    max_jd_chars = _derive_max_jd_chars(config)
    if len(jd_full) > max_jd_chars:
        log.warning(
            "score_job: jd_full for dedup_key=%s is %d chars (> %d cap); "
            "hard-truncating tail. Signals upstream cleaning bloat "
            "(HTML / duplication) — see Layer 2 extraction plan.",
            job.get("dedup_key"),
            len(jd_full),
            max_jd_chars,
        )
        jd = jd_full[:max_jd_chars]
    else:
        jd = jd_full
    return (
        f"Title: {title}\nCompany: {company}\nLocation: {location}{comp}\n\nJob Description:\n{jd}"
    )


class _IncompleteAssessmentError(ValueError):
    """A dispatcher payload was missing or uncoercible on a required axis.

    Fail-closed coercion. Raised by
    ``_coerce_assessment`` instead of silently dropping an axis and producing
    a partial sub-score vector that ``derive_classification`` could then read
    as ``apply``. ``score_job`` catches this and returns status="error" so the
    job is left unscored rather than wrongly classified.
    """


def _coerce_assessment(
    data: dict, provider: str | None, *, degenerate: bool = False
) -> JobAssessment:
    """Coerce dispatcher-returned dict into a JobAssessment.

    The v3 schema emits the 6 sub-score fields at the TOP LEVEL of `data`
    alongside `rationale` and `legitimacy_note` — it does NOT nest them
    under a "sub_scores" key. This function extracts them by name.

    Ignores any top-level 'classification' field the model may emit —
    classification is Python-derived at persist time (anti-pattern 3
    defense; see db.derive_classification).

    Fail-closed coercion: every one of the six axes
    is REQUIRED. A missing or uncoercible axis raises
    ``_IncompleteAssessmentError`` rather than being silently dropped. The
    previous behaviour produced a *partial* sub-score vector, which
    ``derive_classification`` could then read with ``all(v >= 3 ...)`` passing
    vacuously over the surviving axes → a spurious ``apply``. Making the
    partial vector unrepresentable here closes that hole regardless of which
    upstream schema is in play. The baseline schema already rejects partial
    vectors at the dispatcher, so this is latent insurance against variant
    schemas — but cheap and correct insurance.

    ``degenerate`` is threaded through from the cascade (ModelResult.degenerate)
    so persistence can route an all-providers-degenerate result to low_signal.
    """
    sub_scores: dict[str, int] = {}
    for key in _SUB_SCORE_KEYS:
        raw = data.get(key)
        if raw is None:
            raise _IncompleteAssessmentError(
                f"assessment missing required axis {key!r} "
                f"(present axes: {sorted(k for k in _SUB_SCORE_KEYS if data.get(k) is not None)})"
            )
        # Some prompts/LLMs emit each axis as {"evidence": "...", "score": <int>}.
        # Unwrap the score; everything downstream (derive_classification,
        # persistence) only needs the integer.
        if isinstance(raw, dict) and "score" in raw:
            raw = raw["score"]
        try:
            sub_scores[key] = int(raw)
        except (TypeError, ValueError) as exc:
            raise _IncompleteAssessmentError(
                f"assessment axis {key!r} is not coercible to int (got {raw!r})"
            ) from exc
    rationale = data.get("rationale") or {}
    # classification is the sentinel — persist_job_assessment overwrites
    # it with derive_classification(sub_scores, row.legitimacy_note).
    return JobAssessment(
        sub_scores=sub_scores,
        classification="",
        rationale=rationale,
        provider=provider,
        degenerate=degenerate,
    )


def scoring_precheck(job: dict) -> str | None:
    """Return the pre-call skip reason for *job*, or ``None`` if it is ready to score.

    Pure — no I/O, no model call. The SINGLE source of truth for the three
    completeness gates ``score_job`` enforces before spending a model call
    (D-7, "completeness gates, not garbage-in scoring"):

      ``"awaiting_jd"``              — ``jd_full`` absent/empty (SCORER-05).
      ``"awaiting_location"``        — ``locations_structured`` AND ``location``
                                        both empty, the job is still enrichable
                                        (enrichment tier is NOT terminal), and
                                        the row does not already carry
                                        ``"location_missing"`` in
                                        ``unresolved_reasons`` (P3.2).
      ``"awaiting_jd_adjudication"`` — the row carries a PERSISTED jd-content
                                        contract verdict (``jd_content_verdict``,
                                        as stamped by a host's ``set_jd_full``
                                        at write time via
                                        ``jd_content_contract.classify_jd_content``
                                        — see that module) that is not CLEAN,
                                        and the adjudicator has not vouched for
                                        it (private-repo D5, issue #1742). A
                                        REJECT/AMBIGUOUS body is not proven to
                                        be this job's actual posting, so
                                        scoring it burns a model call against
                                        garbage-in / off-topic text.

    Deliberately fails OPEN on a NULL ``jd_content_verdict`` (no gate applied):
    this function stays pure/no-I/O (no ``classify_jd_content`` call here), so
    a row no host has stamped yet — which, on THIS engine today, is every row,
    since the port's public ``jobcannon.db._jd_full.set_jd_full`` does not yet
    persist the verdict columns (Wave-1 hosted-schema gap; see that module's
    docstring and the port's WAIVE note) — scores exactly as it did before this
    gate existed rather than being silently blocked pending a value nothing has
    ever computed for it. This is "measure first, gate what's proven": the
    gate only fires for rows a persisted verdict has actually characterized,
    and is correct-but-inert engine parity code until a host stamps the column.

    ``score_job`` is the only in-tree caller. This engine ships no
    candidate-counting predicate of its own to keep in sync — a host that
    surfaces an "N unscored" style count (dashboard, batch-session total,
    a Score-Now button, etc.) should call this function directly, or mirror
    its exact conditions, rather than re-deriving them; that discipline is
    what prevents the count from desyncing from what ``score_job`` will
    actually score. See ``tests/engine/test_scoring_precheck.py`` for this
    function's contract in isolation from ``score_job``.
    """
    # jd_full gate (SCORER-05). Strip before testing so a whitespace-only body
    # ('   ') is treated as absent — garbage-in for the scorer. A host
    # mirroring this gate in SQL wants the same condition (``TRIM(jd_full) != ''``).
    if not (job.get("jd_full") or "").strip():
        return "awaiting_jd"

    # P3.2 location gate: a row is gated when it carries no
    # location signal AND can still be enriched into one. Terminal-tier rows
    # pass through (their location is as good as it will ever get); rows that
    # already recorded "location_missing" pass through (blocking forever would
    # orphan them). Batch scoring re-selects classification IS NULL continuously,
    # so the gate self-heals once enrichment fills location — it cannot orphan.
    _locs_structured_raw = job.get("locations_structured")
    _locs_structured: list = []
    if _locs_structured_raw:
        try:
            _parsed = json.loads(_locs_structured_raw)
            if isinstance(_parsed, list):
                _locs_structured = _parsed
        except (json.JSONDecodeError, TypeError):
            pass  # treat malformed JSON as empty — no structured data

    _location_flat = job.get("location") or ""
    _enrichment_tier = job.get("enrichment_tier")
    _unresolved_reasons: list[str] = []
    _unresolved_raw = job.get("unresolved_reasons")
    if _unresolved_raw:
        try:
            _parsed_reasons = json.loads(_unresolved_raw)
            if isinstance(_parsed_reasons, list):
                _unresolved_reasons = _parsed_reasons
        except (json.JSONDecodeError, TypeError):
            pass

    if (
        not _locs_structured
        and not _location_flat.strip()
        and _enrichment_tier not in _TERMINAL_ENRICHMENT_TIERS
        and "location_missing" not in _unresolved_reasons
    ):
        return "awaiting_location"

    # D5 / #1742: jd-content contract gate. Reads the PERSISTED verdict only —
    # this function stays pure/no-I/O, so there is no per-call
    # classify_jd_content recompute here (that cost belongs once, at a host's
    # set_jd_full write chokepoint, not on every scoring-precheck call). A
    # NULL verdict (no host has stamped this row yet) fails OPEN: no gate,
    # scores exactly as it did before this gate existed. A row the adjudicator
    # has already vouched for (jd_adjudicated_version stamped at-or-above the
    # CURRENT contract version) also passes through — this is the terminal,
    # resolved state. A host that re-selects unscored rows continuously (as
    # the private repo's batch scoring does) gets self-healing for free: a
    # REJECT/AMBIGUOUS row becomes scorable the moment the adjudicator (or a
    # re-fetch that overwrites jd_full with a clean body, re-stamping the
    # verdict) resolves it — it cannot orphan.
    _verdict_raw = job.get("jd_content_verdict")
    if _verdict_raw is not None:
        _adjudicated_version = job.get("jd_adjudicated_version")
        _adjudicated = (
            isinstance(_adjudicated_version, int) and _adjudicated_version >= JD_CONTENT_VERSION
        )
        if _verdict_raw != JdVerdict.CLEAN.value and not _adjudicated:
            return "awaiting_jd_adjudication"

    return None


def score_job(
    job: dict,
    conn: sqlite3.Connection,
    config: dict,
    candidate_context: str,
    *,
    call_model: Callable[..., Any],
) -> ScoringResult:
    """Score a single job with the v3.0 ordinal rubric.

    Three completeness gates (D-7, no garbage-in scoring), all defined once
    in ``scoring_precheck``:

    SCORER-05 (jd_full gate): empty or missing jd_full returns
    status='skipped' (reason='awaiting_jd') without invoking call_model —
    no API call, no cost, no log spam.

    P3.2 (location gate): when locations_structured AND location
    are both empty AND the job is not at a terminal enrichment tier AND the
    row does not carry "location_missing" in unresolved_reasons, returns
    status='skipped' (reason='awaiting_location'). Batch scoring re-selects
    classification IS NULL continuously, so the gate self-heals once P2.3
    fills location — it cannot orphan jobs.

    D5 (jd-content contract gate, issue #1742): when the row carries a
    persisted jd_content_verdict that does not verdict CLEAN and the row is
    not already adjudicated at-or-above JD_CONTENT_VERSION, returns
    status='skipped' (reason='awaiting_jd_adjudication'). Self-heals the same
    way. Fails open (no gate) when no host has stamped a verdict for this row
    — see ``scoring_precheck``'s docstring.

    Routes through the injected ``call_model(tier='score', output_schema=JOB_ASSESSMENT_SCHEMA, ...)``
    callable — the engine has no provider cascade of its own (was
    ``job_finder.web.model_provider.call_model`` in the private repo, which
    inherited schema retry, cascade fallback, rate limiting, and provider
    attribution ~250 lines deep). Hosts inject a callable matching that
    signature; the return value must expose ``.data``, ``.schema_valid``,
    ``.provider``, ``.model``, and (optionally) ``.degenerate`` — see
    ``ScoringResult``/``_coerce_assessment`` below for exactly which
    attributes are read.

    Args:
        job: Job row dict with dedup_key, title, company_canonical (or company),
            location, locations_structured, salary_min, salary_max, jd_full,
            enrichment_tier, unresolved_reasons.
        conn: Open sqlite3 connection (forwarded to call_model for cost
            recording and rate-limit bootstrap; the engine itself does not
            read or write through it).
        config: Application config dict.
        candidate_context: REQUIRED prompt-ready candidate-context block. The
            v3 rubric anchors (location_fit, comp_fit, etc.) reference
            candidate-specific facts (target locations, comp floor, target
            titles) — scoring without this block silently produces wrong
            scores. This engine has no orchestrator of its own; hosts build
            this string themselves, e.g. via
            ``jobcannon.host.candidate_context.build_candidate_context(profile)``
            or ``resolve_candidate_context(conn, user_id)``. Direct callers
            (eval harness, tests) may build it the same way or pass an
            explicit stub.
        call_model: REQUIRED keyword-only model-dispatch callable, matching
            the private repo's ``model_provider.call_model`` signature
            (``tier=``, ``system=``, ``messages=``, ``conn=``, ``config=``,
            ``output_schema=``, ``job_id=``, ``purpose=``, ``max_tokens=``).
            Hosts own provider selection, cascade, and cost tracking; the
            engine only shapes the prompt/schema and coerces the response.

    Returns:
        ScoringResult envelope.
          ok      → data is JobAssessment, provider is attribution string.
          skipped → data is None; reason='awaiting_jd' or 'awaiting_location'.
          error   → data is None, error is reason string.
    """
    # D-7 completeness gates (jd_full + P3.2 location), centralised in
    # scoring_precheck so the live scorer and the candidate counters share one
    # definition. Both gates skip WITHOUT a model call (no API cost, no log
    # spam) and self-heal: batch scoring re-selects classification IS NULL, so a
    # row becomes scorable the moment enrichment fills jd_full / location.
    _skip_reason = scoring_precheck(job)
    if _skip_reason is not None:
        log.info(
            "score_job: skip dedup_key=%s (%s, enrichment_tier=%r)",
            job.get("dedup_key"),
            _skip_reason,
            job.get("enrichment_tier"),
        )
        return ScoringResult(status="skipped", data=None, reason=_skip_reason)

    system = _build_system_prompt(candidate_context=candidate_context, config=config)
    user_content = _build_user_message(job, config=config)
    output_schema = _resolve_schema(config)

    try:
        result = call_model(
            tier="score",
            system=system,
            messages=[{"role": "user", "content": user_content}],
            conn=conn,
            config=config,
            output_schema=output_schema,
            job_id=job.get("dedup_key"),
            purpose="score_job",
            max_tokens=2048,
        )
    except Exception as exc:
        log.exception(
            "score_job: dispatcher error for dedup_key=%s",
            job.get("dedup_key"),
        )
        return ScoringResult(status="error", data=None, error=str(exc))

    if not result.data or not result.schema_valid:
        return ScoringResult(
            status="error",
            data=None,
            provider=result.provider,
            model=result.model,
            error="dispatcher returned empty or schema-invalid data",
        )

    try:
        assessment = _coerce_assessment(
            result.data,
            result.provider,
            degenerate=getattr(result, "degenerate", False),
        )
    except _IncompleteAssessmentError as exc:
        # Fail-closed: a partial/uncoercible vector must not be
        # persisted as a complete score. Leave the job unscored.
        log.warning(
            "score_job: incomplete assessment for dedup_key=%s from provider=%s: %s",
            job.get("dedup_key"),
            result.provider,
            exc,
        )
        return ScoringResult(
            status="error",
            data=None,
            provider=result.provider,
            model=result.model,
            error=f"incomplete assessment: {exc}",
        )
    return ScoringResult(
        status="ok",
        data=assessment,
        provider=result.provider,
        model=result.model,
    )

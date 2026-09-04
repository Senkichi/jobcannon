# PORTED from job_finder/web/primary_source_tiebreak.py @ a1de3bc10a4d9e9b43d13562d1e9acf9d1011544 (private job-cannon). Ledger L-0230.
"""Quick-tier LLM tie-breaker for ambiguous primary-posting matches (Phase 4).

When resolve_primary_posting leaves a job loose (title drift, abbreviation
soup, multi-location duplicates the location tokens couldn't split), one
call_model(tier="quick") call judges which board posting — if any — is the
SAME job. The quick tier's primary is Ollama, so the marginal cost is $0;
the cascade's budget gates and providers.daily_limits machinery already
protect the paid fallbacks.

Used by the company-batched resolver only (primary_source_resolver). The free
enrichment-tier hook stays heuristic — it runs inline in enrichment passes
where an extra model round-trip per job is not worth the latency.

Forced-match bias (pitfall P13 / the no-signal-vs-midpoint lesson): the
prompt gives the model an explicit "none of these / can't tell" exit, and
only a confident verdict with a valid unique index upgrades anything. Every
other shape — not confident, null, out of range, wrong type — returns None
and the match stays loose. Provider failures propagate to the caller so it
can stop tie-breaking for the rest of the run instead of timing out per job.

The verdict is an upgrade decision, not a third confidence value:
direct_url_confidence stays 'strict', and the resolver tags LLM-upgraded
merges with a 'primary_source_llm' source label so they stay auditable
(SELECT ... WHERE sources LIKE '%primary_source_llm%').

# PORT-SEAM: call_model is an injected keyword-only parameter (design note
# PR-4 section 1c), not the private module's deferred
# `from job_finder.web.model_provider import call_model` import — the engine
# has no provider of its own; the host supplies this (job_scorer.score_job
# precedent). The caller (jobcannon.engine.primary_source_resolver, L-0229)
# reaches this function via the already-declared
# `svc.tiebreak_primary_posting` ScanServices field (services.py) rather
# than a direct import — that seam predates this port and is left as-is
# (host wiring binds it to a partial closing over the host's call_model).
# DEFAULT_MAX_BOARD is copied verbatim into primary_source_resolver.py
# already (same convention as ats_slug_challenge.TRIGGER_PREFIX_CAREERS_URL).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from jobcannon.engine.direct_link import _posting_link

logger = logging.getLogger(__name__)

# Boards larger than this skip the tie-breaker: the candidate list would blow
# the quick model's effective attention and the odds of a real 1:1 match are
# lower anyway (config: direct_link.resolver.llm_tiebreak_max_board).
DEFAULT_MAX_BOARD = 40

_SNIPPET_CHARS = 400

_TIEBREAK_SCHEMA = {
    "type": "object",
    "properties": {
        "match_index": {"type": ["integer", "null"]},
        "confident": {"type": "boolean"},
    },
    "required": ["match_index", "confident"],
}

_SYSTEM_PROMPT = (
    "You match a job listing found on an aggregator (LinkedIn, Glassdoor, a "
    "search API) to the postings on the hiring company's own careers board.\n"
    "Decide which posting, if any, is the SAME job as the listing.\n\n"
    'Respond with JSON only: {"match_index": <0-based index>, "confident": true}\n'
    "when exactly one posting is clearly the same job.\n"
    'Respond {"match_index": null, "confident": false} when no posting clearly '
    "matches, when several could match, or when you cannot tell.\n\n"
    'Titles may differ in wording ("Sr. SWE II" vs "Senior Software Engineer - '
    'Platform") — match on role meaning, seniority, and location, not exact '
    "words. Never guess: an uncertain match must return confident=false."
)


def _format_candidates(candidates: list[dict]) -> str:
    lines = []
    for i, posting in enumerate(candidates):
        title = (posting.get("title") or "").strip() or "(untitled)"
        location = (posting.get("location") or "").strip() or "unspecified"
        lines.append(f"{i}. {title} — {location}")
    return "\n".join(lines)


def tiebreak_primary_posting(
    postings: list[dict],
    job_title: str,
    job_location: str,
    job_snippet: str | None,
    conn: sqlite3.Connection,
    config: dict,
    *,
    call_model: Callable[..., Any],
    job_id: str | None = None,
    max_board: int = DEFAULT_MAX_BOARD,
) -> dict | None:
    """Return the board posting the model confidently matched, else None.

    None means "stay loose" (no candidates, board too large, model declined,
    or the verdict failed validation). Provider/cascade errors are NOT
    swallowed — the resolver uses them to disable tie-breaking for the rest
    of the run.

    Args:
        call_model: REQUIRED keyword-only model-dispatch callable, matching
            the private repo's ``model_provider.call_model`` signature
            (tier, system, messages, conn, config, output_schema, job_id,
            purpose, max_tokens). Bound by the host via a partial on the
            ``ScanServices.tiebreak_primary_posting`` field.
    """
    candidates = [p for p in (postings or []) if _posting_link(p)]
    if not candidates or len(candidates) > max_board:
        return None

    snippet = (job_snippet or "").strip()[:_SNIPPET_CHARS]
    user_msg = (
        f"Job listing:\n"
        f"  title: {job_title}\n"
        f"  location: {job_location or 'unspecified'}\n"
        + (f"  description snippet: {snippet}\n" if snippet else "")
        + f"\nCareers-board postings:\n{_format_candidates(candidates)}"
    )

    result = call_model(
        tier="quick",
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        conn=conn,
        config=config,
        output_schema=_TIEBREAK_SCHEMA,
        job_id=job_id,
        purpose="primary_source_tiebreak",
        max_tokens=128,
    )

    data = result.data if isinstance(result.data, dict) else {}
    index = data.get("match_index")
    if data.get("confident") is not True:
        return None
    # bool is an int subclass — a model emitting true/false here must not
    # silently index posting 1/0.
    if isinstance(index, bool) or not isinstance(index, int):
        return None
    if not 0 <= index < len(candidates):
        logger.warning(
            "tiebreak: model returned out-of-range index %s for %s (%d candidates)",
            index,
            job_id,
            len(candidates),
        )
        return None
    return candidates[index]

"""PORTED from job_finder/web/jd_adjudicator.py @ 0cbf333a (private job-cannon). Ledger L-0189.

Engine half of the L-0189 three-way residence split (see the jd-adjudication
design addendum). Ports ``adjudicate_jd`` -- the LLM tie-breaker for the
AMBIGUOUS jd-content middle (PR2 of the jd-content contract, see
``jobcannon.engine.jd_content_contract``) -- plus its module constants. The
DB write-back half (``stamp_adjudicated`` / ``select_adjudication_candidates``)
lives in ``jobcannon.db._jd_adjudication``; the scheduled batch driver lives in
``jobcannon.host.jd_adjudication_backfill``.

# PORT-SEAM: the module-level `from job_finder.web.model_provider import
# call_model` is dropped. `adjudicate_jd` grows a REQUIRED keyword-only
# `call_model` param -- identical idiom to `jobcannon.engine.job_scorer.score_job`
# (`call_model: Callable[..., Any]`, injected model-dispatch callable; the
# engine has no provider of its own, the host supplies this).

The deterministic contract (``jd_content_contract``) confidently CLEANs and
REJECTs most stored ``jd_full`` bodies. The remainder are AMBIGUOUS -- a real
JD that lacks standard headings vs. a chrome / landing / listing page that
happens to mention the role. This function resolves that middle with a cheap
quick-tier LLM yes/no, run by a BACKGROUND job (never on the hot ingest
path), so the contract stays fast and deterministic wherever it can be.

A row the LLM (or the deterministic CLEAN check) vouches for is stamped with
the live ``JD_CONTENT_VERSION`` in ``jd_adjudicated_version`` so it is judged
once per contract version.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Chars of the body shown to the judge — enough to decide, bounded for cost/latency.
_PROMPT_JD_CHARS = 3500

#: Per-call request timeout for the adjudication LLM call. This is a 128-token
#: yes/no, so it must not inherit a provider's generous default (Ollama's is 300s):
#: a single stuck primary call would otherwise freeze the whole backfill — and the
#: scheduled noon job — for minutes. On timeout the provider raises and
#: ``call_model`` advances the cascade to the next quick-tier provider (both
#: Gemini and the Claude CLI are $0), so a stalled primary recovers in ~this many
#: seconds instead of 300; the row only becomes ``undetermined`` if the ENTIRE
#: cascade is exhausted. Set well above the observed ~4-15s/call Ollama latency
#: (with cold-load + busy-machine headroom) so a merely-slow call is NOT abandoned
#: onto rate-limited fallbacks — only a genuine stall trips it.
_ADJUDICATION_TIMEOUT_S = 90.0

_ADJUDICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_job_description": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["is_job_description"],
    "additionalProperties": True,
}

_SYSTEM = (
    "You are a strict classifier deciding whether a block of text scraped from a "
    "web page IS the actual job posting / description for a SPECIFIC role at a "
    "SPECIFIC company.\n"
    "Answer is_job_description=true ONLY if the text describes that role's duties, "
    "responsibilities, requirements, or qualifications.\n"
    "Answer is_job_description=false if the text is a different page: a company "
    "About/marketing page, a job-listing index or search-results page, a login / "
    "blocked / captcha page, a cookie-consent notice, an unrelated article (e.g. a "
    "Wikipedia entry), a closed/expired-posting notice, or a posting for a "
    "DIFFERENT role.\n"
    "Respond with JSON only."
)


def adjudicate_jd(
    conn: sqlite3.Connection,
    title: str | None,
    company: str | None,
    jd_full: str | None,
    *,
    call_model: Callable[
        ..., Any
    ],  # PORT-SEAM: injected model-dispatch callable (was model_provider.call_model)
    config: dict,
) -> bool | None:
    """Ask the quick-tier LLM whether *jd_full* is the posting for title@company.

    Returns True (is the JD), False (is not), or None when the call errors or the
    model returns nothing usable — the caller leaves a None row unstamped so it is
    retried on the next backfill pass.

    Args:
        conn: Open connection (used by call_model for cost recording), matching
            ``jobcannon.engine.job_scorer.score_job``'s convention verbatim --
            keeping it is NOT a DB coupling, the engine never imports
            ``jobcannon.db.*``; it only holds an opaque handle passed straight
            through to the injected ``call_model``.
        call_model: REQUIRED keyword-only model-dispatch callable. The engine
            has no provider of its own; the host supplies this.
        config: Application config dict.
    """
    if not jd_full:
        return None
    body = jd_full.strip()[:_PROMPT_JD_CHARS]
    user_msg = (
        f"TITLE: {title or '(unknown)'}\n"
        f"COMPANY: {company or '(unknown)'}\n"
        f"--- SCRAPED TEXT (first {_PROMPT_JD_CHARS} chars) ---\n{body}"
    )
    try:
        result = call_model(
            tier="quick",
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            conn=conn,
            config=config,
            output_schema=_ADJUDICATION_SCHEMA,
            purpose="jd_content_adjudication",
            max_tokens=128,
            timeout=_ADJUDICATION_TIMEOUT_S,
        )
        data = result.data
    except Exception as exc:
        # A cascade-exhaustion error is an ordinary Exception subclass, caught
        # here like any other call_model failure -- degrades to None (retried
        # on the next backfill pass) exactly like any other call_model error,
        # per this function's documented contract.
        logger.warning("adjudicate_jd: call_model failed for %r: %s", (title or "")[:60], exc)
        return None
    if not isinstance(data, dict) or "is_job_description" not in data:
        return None
    return bool(data["is_job_description"])

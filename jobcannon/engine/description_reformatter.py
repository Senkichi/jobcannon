# PORTED from job_finder/web/description_reformatter.py @ 65e5ce021068b70a2369ac279c75395a078e1013 (private job-cannon). Ledger L-0175.
"""Quick-tier-assisted job description reformatting.

Reformats raw job descriptions (pipe-separated, bullet lists, messy email-parsed
formatting) into clean section/paragraph style resembling a real job posting.

Per user decision: ALL job descriptions are reformatted — not just merged ones.
A ``description_reformatted`` flag on the ``jobs`` row prevents re-running on
already-processed jobs.

Design:
  - reformat_description: Single-job reformatting via the quick-tier cascade.
  - run_description_reformat_pass: One-time background pass over all unformatted jobs.
  - Both are graceful-degradation: failures return original text unchanged.
  - Already-well-formatted descriptions (2+ section headers) are skipped.
  - A conversational refusal/meta-response from the LLM (looks_like_llm_refusal)
    is treated the same as a failure — original text unchanged, never persisted.

Cost note: in production the quick-tier resolves to Ollama or a free-tier
provider — $0/call on the primary path.

Exports:
    reformat_description: Reformat a single description string via the cascade.
    run_description_reformat_pass: One-time background pass over all unformatted jobs.

# PORT-SEAM: call_model is an injected keyword-only parameter on
# reformat_description (design note PR-4 section 1c), not the private
# module-level `from job_finder.web.model_provider import call_model` import
# — the engine has no provider of its own (job_scorer.score_job precedent).
# The private `ProviderCascadeExhaustedError`-triggered retry-via-CLI
# fallback (`claude_client.call_claude`) is DROPPED entirely: the public
# `call_model` dispatcher's own cascade already includes the CLI tier
# end-to-end, so a same-shaped second fallback call would be redundant
# (careers_scraper.py precedent, #369). `db_helpers.standalone_connection`
# is DIES -> `svc.connection_factory()`; `run_description_reformat_pass`'s
# `db_path: str` parameter is dropped accordingly.
#
# PORT-SEAM: `run_description_reformat_pass`'s SQL reads/writes
# `jobs.description_reformatted` as a 0/1 flag column. That column does not
# exist in the public `jobs` schema yet (no migration adds it) — this
# function is ported faithfully but stays unwired/inert (no caller in this
# PR invokes it), the same "port standalone, unwired" treatment already
# applied to L-0054's ollama probe. Adding the column is schema work outside
# this row's scope; flagged here for whichever unit picks up the one-time
# background pass itself.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from jobcannon.engine.llm_refusal_guard import looks_like_llm_refusal
from jobcannon.engine.services import get_services

logger = logging.getLogger(__name__)


# Regex pattern for common section headers (2+ indicates already formatted)
_SECTION_HEADER_PATTERN = re.compile(
    r"(?:About|Overview|Summary|Responsibilities|Requirements|Qualifications|Benefits|What You|Minimum|Preferred|Nice to Have|The Role|Your Role|Who You Are|What We)",
    re.IGNORECASE,
)

# Minimum number of section headers to consider a description already formatted
_ALREADY_FORMATTED_THRESHOLD = 2

# Structured output schema so both the CLI tier and any Ollama cascade entry
# return the same {"text": ...} shape. Without this, Ollama's forced
# "format":"json" would invent arbitrary keys and result.get("text", "")
# would silently read empty strings.
_REFORMAT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Reformatted job description with clear section headers and paragraphs",
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}

# System prompt for the quick-tier reformatting call
_SYSTEM_PROMPT = (
    "You are a job description formatter. Reformat the following job description into "
    "clean, professional sections with headers and paragraphs — like a real job posting. "
    "Use section headers like 'About the Role', 'Responsibilities', 'Requirements', "
    "'Qualifications', 'Benefits', etc. as appropriate. Convert bullet lists and "
    "pipe-separated items into proper paragraphs or clean bullet lists. Preserve all "
    "factual content — do not add or remove information. Return the reformatted text "
    "in the 'text' field."
)


def reformat_description(
    description: str | None,
    conn: Any = None,
    config: dict | None = None,
    *,
    call_model: Callable[..., Any],
) -> str | None:
    """Use the quick-tier cascade to reformat a job description into section/paragraph style.

    Takes raw description text (pipe-separated, bullet lists, or messy formatting)
    and returns clean section/paragraph text resembling a real job posting.

    Per user decision: "All job descriptions reformatted to section/paragraph style
    (like real job postings) — applies to ALL jobs, not just merged ones."

    Returns original description on any failure (graceful degradation).

    Args:
        description: Raw job description text to reformat. Returns as-is if None/empty.
        conn: Optional DB connection for cost recording (required by call_model).
        config: Optional application config dict.
        call_model: REQUIRED keyword-only model-dispatch callable, matching
            the private repo's ``model_provider.call_model`` signature
            (tier, system, messages, conn, config, output_schema, job_id,
            purpose, max_tokens). PORT-SEAM: the engine has no provider of
            its own; the host supplies this.

    Returns:
        Reformatted description text, or original if skipped/failed.
    """
    if not description:
        return description

    if config is None:
        config = {}

    # Skip if already well-formatted: check for 2+ section headers
    header_count = len(_SECTION_HEADER_PATTERN.findall(description))
    if header_count >= _ALREADY_FORMATTED_THRESHOLD:
        return description

    # call_model() requires a non-None conn for cost recording. When conn is
    # None (e.g. single-shot callers not passing a DB handle), skip the
    # reformat entirely and return the original text unchanged — there is no
    # CLI-direct fallback path in this port (see module PORT-SEAM above).
    if conn is None:
        return description

    try:
        model_result = call_model(
            tier="quick",
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": description[:4000]}],
            conn=conn,
            config=config,
            output_schema=_REFORMAT_SCHEMA,
            job_id=None,
            purpose="description_reformat",
            max_tokens=2048,
        )
        result = model_result.data

        # Both providers return {"text": ...} under _REFORMAT_SCHEMA
        if isinstance(result, dict):
            reformatted = result.get("text", "")
        else:
            reformatted = str(result)

        if reformatted and reformatted.strip():
            reformatted = reformatted.strip()
            if looks_like_llm_refusal(reformatted):
                logger.warning(
                    "reformat_description: LLM returned a conversational refusal/meta-response "
                    "instead of reformatted text — keeping original description unchanged"
                )
                return description
            return reformatted

        return description

    except Exception as e:
        logger.warning("reformat_description failed (returning original): %s", e)
        return description


def run_description_reformat_pass(
    config: dict | None = None,
    *,
    call_model: Callable[..., Any],
) -> int:
    """One-time background pass to reformat all job descriptions.

    Processes jobs where description_reformatted=0 and description IS NOT NULL.
    Sets description_reformatted=1 after each job is successfully reformatted.
    Opens a connection via ``svc.connection_factory()``.

    PORT-SEAM: unwired/inert — the public ``jobs`` schema does not yet carry
    a ``description_reformatted`` column (see module docstring). No caller
    in this port invokes this function.

    Returns count of jobs reformatted.

    Args:
        config: Optional application config dict.
        call_model: REQUIRED keyword-only model-dispatch callable, threaded
            through to each reformat_description call.

    Returns:
        Count of jobs where reformatting was attempted (including already-formatted).
    """
    if config is None:
        config = {}

    svc = get_services()
    try:
        with svc.connection_factory() as conn:
            rows = conn.execute(
                "SELECT dedup_key, description FROM jobs "
                "WHERE description_reformatted = 0 AND description IS NOT NULL"
            ).fetchall()

            reformatted_count = 0

            for row in rows:
                dedup_key = row["dedup_key"]
                original = row["description"]

                try:
                    reformatted = reformat_description(
                        original, conn=conn, config=config, call_model=call_model
                    )

                    if reformatted != original and reformatted is not None:
                        # Text changed — update both description and flag
                        conn.execute(
                            "UPDATE jobs SET description = ?, description_reformatted = 1 "
                            "WHERE dedup_key = ?",
                            (reformatted, dedup_key),
                        )
                        reformatted_count += 1
                    else:
                        # Text unchanged (already formatted or the model returned the same text)
                        # Mark as processed so it's not retried
                        conn.execute(
                            "UPDATE jobs SET description_reformatted = 1 WHERE dedup_key = ?",
                            (dedup_key,),
                        )

                    conn.commit()

                except Exception as e:
                    logger.warning(
                        "Failed to reformat description for '%s' (non-fatal): %s",
                        dedup_key,
                        e,
                    )
                    # Mark as processed anyway to avoid infinite retry loop
                    try:
                        conn.execute(
                            "UPDATE jobs SET description_reformatted = 1 WHERE dedup_key = ?",
                            (dedup_key,),
                        )
                        conn.commit()
                    except Exception:
                        logger.debug("description reformat commit failed", exc_info=True)

            logger.info("Reformatted %d job descriptions", reformatted_count)
            return reformatted_count

    except Exception as e:
        logger.warning("run_description_reformat_pass failed: %s", e)
        return 0

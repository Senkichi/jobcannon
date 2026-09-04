"""PORTED from job_finder/web/jd_adjudicator.py @ 0cbf333a (private job-cannon). Ledger L-0189.

Host half of the L-0189 three-way residence split (see the jd-adjudication
design addendum): the scheduled batch driver for
``run_jd_adjudication_backfill``. Opens no connection itself (threaded in by
the caller, matching the private original and the DI-injected ``call_model``
convention); registered as a procrastinate periodic task in
``jobcannon/host/tasks.py``. Imports the DB write-back primitives from
``jobcannon.db._jd_adjudication`` and the engine LLM tie-breaker from
``jobcannon.engine.jd_adjudicator`` -- references no scoring entrypoint
(``score_job``/``scoring_precheck``) and wires no ``score_and_persist_job``,
so this module does NOT itself trip the #183 WIRED scan
(``tests/test_scoring_precheck_wiring_guard.py``); it is what makes that
guard's ``writer_exists`` half true, via ``stamp_adjudicated``.

# PORT-SEAM: private ``_heal_offsite`` is NOT ported/called in this unit (see
# ``jobcannon.db._jd_adjudication``'s module docstring PORT-SEAM for why, and
# the PR body's Modularity note for the fast-follow). A REJECT verdict or an
# LLM "no" decision here is counted in the `rejected` tally but NOT applied --
# the row is left completely untouched and is naturally re-selected (and
# re-classified against its current content) on the next scheduled tick,
# exactly like an `undetermined` decision. This is a scoped-down but
# functional driver: the stamp path (the #183-gating half) is fully wired
# end-to-end; only the heal leg is deferred.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from jobcannon.db._jd_adjudication import select_adjudication_candidates, stamp_adjudicated
from jobcannon.engine.jd_adjudicator import adjudicate_jd
from jobcannon.engine.jd_content_contract import JdVerdict, classify_jd_content

logger = logging.getLogger(__name__)


def run_jd_adjudication_backfill(
    conn: Any,
    config: dict,
    *,
    call_model: Callable[..., Any],
    limit: int = 200,
    unscored_reserve: int | None = None,
) -> dict:
    """Adjudicate a bounded batch of AMBIGUOUS jd_full rows (the scheduled entry point).

    Selects candidates via ``select_adjudication_candidates`` (present jd_full,
    not yet adjudicated at the live JD_CONTENT_VERSION, not already jd-content-
    quarantined). Each row is classified deterministically first, so only the
    genuinely AMBIGUOUS rows cost an LLM call:
      * CLEAN      -> stamp (vouched; won't re-select)
      * REJECT     -> counted, not applied (heal leg peeled, see module docstring)
      * AMBIGUOUS  -> LLM: YES stamps, NO counted-not-applied, None leaves it to retry.

    Returns a summary dict (scanned / llm_calls / kept / rejected / undetermined /
    skipped_stale).

    Durability contract (private issue #1060), narrowed to the stamp-only path
    this unit ships: NO write transaction is ever held across an LLM call --
    all decisions are collected in memory first (zero writes during the
    classification loop), then each stamp decision is applied and committed
    (via ``stamp_adjudicated``'s own ``commit_unless_nested``) immediately
    after that loop completes. There is no cross-batch atomicity: a crash
    between two items leaves every already-committed item fully applied and
    every not-yet-reached item completely untouched -- safe by construction,
    since an unwritten item was never stamped and is simply re-selected next
    tick.

    Every stamp is additionally guarded by the content premise captured at
    classification time: the UPDATE only matches when ``jd_full`` still
    equals what was actually classified. A concurrent writer that rewrites
    ``jd_full`` between selection and this write-back causes the guard to
    miss; the row is skipped (logged, counted in ``skipped_stale``, left
    completely untouched) rather than vouching for content the classifier
    never saw. It is naturally re-picked and re-classified against its
    current content next tick.

    Args:
        conn: Open connection, threaded down to ``adjudicate_jd`` for
            ``call_model``'s cost recording and down to the DB primitives for
            the actual writes.
        config: Application config dict.
        call_model: REQUIRED keyword-only model-dispatch callable, threaded
            to ``jobcannon.engine.jd_adjudicator.adjudicate_jd``.
        limit: Batch size cap.
        unscored_reserve: Passed through to ``select_adjudication_candidates``
            (issue #1939 two-cohort partition); defaults to half of ``limit``.
    """
    rows = select_adjudication_candidates(conn, limit=limit, unscored_reserve=unscored_reserve)

    # Collect all stamp decisions in memory first (no writes during the LLM
    # loop). Each decision carries the jd_full seen at classification time --
    # the premise the write-back UPDATE below is guarded against.
    decisions: list[tuple[str, str]] = []  # (dedup_key, expected_jd_full)
    scanned = llm_calls = kept = rejected = undetermined = 0

    for row in rows:
        scanned += 1
        dedup_key, title, company, jd_full = (
            row["dedup_key"],
            row["title"],
            row["company"],
            row["jd_full"],
        )
        verdict = classify_jd_content(jd_full, title, company, config)
        if verdict.verdict is JdVerdict.REJECT:
            # PORT-SEAM: heal leg peeled -- counted, not applied (see module docstring).
            rejected += 1
            continue
        if verdict.verdict is JdVerdict.CLEAN:
            decisions.append((dedup_key, jd_full))
            kept += 1
            continue
        # AMBIGUOUS -> the LLM tie-breaker (the only path that costs a call).
        llm_calls += 1
        decision = adjudicate_jd(
            conn, title, company, jd_full, call_model=call_model, config=config
        )
        if decision is None:
            undetermined += 1
            continue  # leave unstamped -> retried next pass
        if decision:
            decisions.append((dedup_key, jd_full))
            kept += 1
        else:
            # PORT-SEAM: heal leg peeled -- counted, not applied (see module docstring).
            rejected += 1

    # Apply decisions (no LLM calls here). Each stamp is applied and committed
    # via stamp_adjudicated's own commit_unless_nested.
    skipped_stale = 0
    for dedup_key, expected_jd_full in decisions:
        applied = stamp_adjudicated(conn, dedup_key, expected_jd_full)
        if not applied:
            skipped_stale += 1
            logger.info(
                "jd adjudication backfill: skipping stale-premise row "
                "dedup_key=%s action=stamp reason=stale-premise",
                dedup_key,
            )

    logger.info(
        "jd adjudication backfill: scanned=%d llm=%d kept=%d rejected=%d "
        "undetermined=%d skipped_stale=%d",
        scanned,
        llm_calls,
        kept,
        rejected,
        undetermined,
        skipped_stale,
    )
    return {
        "scanned": scanned,
        "llm_calls": llm_calls,
        "kept": kept,
        "rejected": rejected,
        "undetermined": undetermined,
        "skipped_stale": skipped_stale,
    }

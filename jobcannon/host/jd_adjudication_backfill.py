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

# PORT-SEAM: private ``_heal_offsite`` is ported NON-literally (issue #360
# fast-follow). The design addendum's §1a.3 inline UPDATE would have written
# ``jd_content_verdict``/``jd_content_signal``/``jd_adjudicated_version``'s
# NULL-invalidation directly from this module -- a second writer of columns
# ``jobcannon.db._jd_full`` owns (see that module's docstring). Per the
# addendum's §7 Q-1 Rec (b), the content-side heal lives in
# ``_jd_full.py::clear_jd_full`` instead; the score-retraction half stays with
# its own owner, ``_assessment_writer.invalidate_job_score``. This driver
# composes the two under ONE ambient ``with_write_txn`` per healed row, so
# a REJECT verdict (deterministic or LLM "no") is applied atomically: body
# cleared + verdict columns nulled + quarantine reason appended + scoring
# tuple retracted, or nothing at all.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from jobcannon.db._assessment_writer import invalidate_job_score
from jobcannon.db._jd_adjudication import select_adjudication_candidates, stamp_adjudicated
from jobcannon.db._jd_full import clear_jd_full
from jobcannon.db.pool import with_write_txn
from jobcannon.engine.jd_adjudicator import adjudicate_jd
from jobcannon.engine.jd_content_contract import JD_OFFSITE, JdVerdict, classify_jd_content

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
      * REJECT     -> heal (body cleared, row quarantined, score retracted)
      * AMBIGUOUS  -> LLM: YES stamps, NO heals (as REJECT), None leaves it to retry.

    Returns a summary dict (scanned / llm_calls / kept / rejected / undetermined /
    skipped_stale).

    Durability contract (private issue #1060): NO write transaction is ever
    held across an LLM call -- all decisions are collected in memory first
    (zero writes during the classification loop), then each stamp decision is
    applied and committed (via ``stamp_adjudicated``'s own
    ``with_write_txn``) immediately after that loop completes, and each
    heal decision is applied under ONE ambient ``with_write_txn`` that
    commits once -- ``clear_jd_full``'s content-side heal and
    ``invalidate_job_score``'s score retraction land together or not at all
    (see the module docstring PORT-SEAM for why the two writes live in
    different owner modules). There is no cross-batch atomicity: a crash
    between two items leaves every already-committed item fully applied and
    every not-yet-reached item completely untouched -- safe by construction,
    since an unwritten item was never stamped/healed and is simply re-selected
    next tick.

    Every write-back is additionally guarded by the content premise captured
    at classification time: the UPDATE only matches when ``jd_full`` still
    equals what was actually classified. A concurrent writer that rewrites
    ``jd_full`` between selection and this write-back causes the guard to
    miss; the row is skipped (logged, counted in ``skipped_stale``, left
    completely untouched -- for a heal, the score retraction is skipped too)
    rather than vouching for, or deleting, content the classifier never saw.
    It is naturally re-picked and re-classified against its current content
    next tick.

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

    # Collect all decisions in memory first (no writes during the LLM loop).
    # Each decision carries the jd_full seen at classification time -- the
    # premise the write-back UPDATEs below are guarded against.
    decisions: list[tuple[str, str]] = []  # stamps: (dedup_key, expected_jd_full)
    heals: list[tuple[str, str, str]] = []  # heals: (dedup_key, expected_jd_full, reason)
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
            # PORT-SEAM: heal via clear_jd_full (not §1a.3's inline UPDATE --
            # see module docstring). REJECT always carries its contract reason
            # (jd_full_offsite/_expired/_truncated); the `or JD_OFFSITE` covers
            # a hypothetical reason-less REJECT the same way the private heal
            # treated every "not the posting" as offsite.
            heals.append((dedup_key, jd_full, verdict.reason or JD_OFFSITE))
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
            # LLM "no" == the private heal's offsite case: no deterministic
            # reason exists, so the generic offsite quarantine code applies.
            heals.append((dedup_key, jd_full, JD_OFFSITE))
            rejected += 1

    # Apply decisions (no LLM calls here). Each stamp is applied and committed
    # via stamp_adjudicated's own with_write_txn.
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

    # Apply heals. Each is ONE ambient transaction spanning two owner-module
    # writers (see module docstring): clear_jd_full runs FIRST because it
    # carries the premise guard -- on a stale-premise miss the score
    # retraction is skipped too, so a concurrently-rewritten row keeps
    # whatever (fresh, valid) score its new content earned. On a hit the
    # scoring tuple is retracted in the same commit so the stale score
    # cannot outlive the body it was computed against. with_write_txn's
    # trailing commit_unless_nested is what actually commits on a bare
    # pooled connection (its `raw.transaction()` block degrades to a
    # savepoint inside the implicit transaction the SELECTs opened -- same
    # convention as _jd_full.py / nightly/state.py); under tests' ambient
    # transaction it is a no-op and the fixture rollback covers everything.
    for dedup_key, expected_jd_full, reason in heals:
        with with_write_txn(conn):
            healed = clear_jd_full(conn, dedup_key, expected_jd_full, reason=reason)
            if healed:
                invalidate_job_score(conn, dedup_key)
        if not healed:
            skipped_stale += 1
            logger.info(
                "jd adjudication backfill: skipping stale-premise row "
                "dedup_key=%s action=heal reason=stale-premise",
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

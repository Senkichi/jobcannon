"""PORTED from job_finder/web/jd_adjudicator.py @ 0cbf333a (private job-cannon). Ledger L-0189.

DB half of the L-0189 three-way residence split (see the jd-adjudication design
addendum): the writer that satisfies issue #183's guard
(``tests/test_scoring_precheck_wiring_guard.py``), which fails any wiring of
scoring into a host/db/worker module unless a non-NULL ``jd_adjudicated_version``
writer already exists under ``jobcannon/db/``. ``stamp_adjudicated`` is that
writer. ``select_adjudication_candidates`` is the batched eligibility SELECT the
backfill driver (``jobcannon/host/jd_adjudication_backfill.py``) uses to build its
work batch.

# PORT-SEAM: private ``_heal_offsite`` (jd_adjudicator.py:149-202) is NOT ported in
# this unit. It is not a jd_adjudicated_version writer (it only NULLs jd_full,
# which cascades the watermark to NULL, never sets a value) so it does not gate
# the #183 guard. Porting it as the design addendum's §1a.3 inline UPDATE would
# NULL jd_content_verdict/jd_content_signal/jd_adjudicated_version from a second
# module -- exactly the second-writer condition _jd_full.py's own module
# docstring and _assessment_writer.py's `invalidate_job_score` PORT-SEAM (see
# that module) exist to prevent; the addendum's own §7 Q-1 leaves the correct
# surface (fold into a new `_jd_full.py::clear_jd_full`, its Rec (b)) as an open,
# undesigned question. §7 Q-2 explicitly sanctions peeling `heal_offsite` + the
# driver's heal leg into a fast-follow once Q-1 lands, confirming this still
# unblocks L-0259 (the scoring wiring this writer exists for). See the PR body's
# Modularity note.

column-ownership amendment (over ``_assessment_writer.py``'s existing note): that
module's ``invalidate_job_score`` declines to touch ``jd_content_verdict`` /
``jd_content_signal`` / ``jd_adjudicated_version`` because ``_jd_full.py::set_jd_full``
owns them -- but only the NULL-invalidation path. Nothing in the repo stamped a
NON-NULL ``jd_adjudicated_version`` before this module; ``stamp_adjudicated`` is the
first and sole writer of that value, so it does not create a second writer of the
column, it creates the FIRST one. Reader of either module: see this note and
``_assessment_writer.py``'s own PORT-SEAM for the two halves of the split.
"""

from __future__ import annotations

from typing import Any

from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine.jd_content_contract import (
    JD_CONTENT_REASON_CODES,
    JD_CONTENT_VERSION,
    JdVerdict,
)


def stamp_adjudicated(conn: Any, dedup_key: str, expected_jd_full: str) -> bool:
    """Mark a row vouched-for at the current contract version (won't re-select).

    Guarded by the premise captured at classification time (private issue #1060,
    Blocker 1): the UPDATE only fires when ``jd_full`` still equals
    ``expected_jd_full`` -- the exact text the classifier evaluated. Between
    classification and this write-back a concurrent writer (the ingest path,
    ``_jd_full.py::set_jd_full``) may have rewritten ``jd_full`` for this row;
    stamping unconditionally in that case would vouch for content the classifier
    never saw.

    This is the writer ``m0009``'s module docstring calls for and
    ``tests/test_scoring_precheck_wiring_guard.py`` (#183) requires to exist
    before any host/db/worker module may wire scoring.

    Returns True if the row was stamped; False if the guard missed (stale
    premise) -- the caller leaves the row unstamped so it is naturally re-picked
    (and re-classified against its now-current content) on the next scheduled
    tick.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    with raw.transaction():
        cur = raw.execute(
            "UPDATE postings SET jd_adjudicated_version = %(version)s "
            "WHERE dedup_key = %(dedup_key)s AND jd_full = %(expected_jd_full)s",
            {
                "version": JD_CONTENT_VERSION,
                "dedup_key": dedup_key,
                "expected_jd_full": expected_jd_full,
            },
        )
    commit_unless_nested(raw)
    return cur.rowcount > 0


def select_adjudication_candidates(
    conn: Any, *, limit: int, unscored_reserve: int | None = None
) -> list[Any]:
    """Batched eligibility SELECT for the jd-adjudication backfill (two cohorts).

    Eligible: a present, non-blank ``jd_full``; not already jd-content-quarantined
    (no ``JD_CONTENT_REASON_CODES`` member in ``unresolved_reasons``); not yet
    adjudicated at the live ``JD_CONTENT_VERSION`` (NULL watermark = never judged).
    The private eligibility predicate defended against *malformed*
    ``unresolved_reasons`` text with a ``json_valid``/``json_each`` CASE ladder --
    on Postgres ``unresolved_reasons`` is ``jsonb NOT NULL DEFAULT '[]'``, so that
    whole ladder collapses to a shape-safe set test (mirrors
    ``_jd_full.py``'s ``jsonb_array_elements_text`` idiom); a jsonb column cannot
    be malformed, so the private "malformed => eligible" branch is unreachable
    and correctly drops here.

    The batch is PARTITIONED between two cohorts (private issue #1939): a single
    ORDER BY with one cap cannot serve both goals under a bounded limit -- the
    scored-retraction sweep (stale score on garbage retracted soonest) would
    starve the blocked-unscored cohort (unscored rows gated by the D5
    ``awaiting_jd_adjudication`` precheck, whose only blocker is adjudication)
    when scored rows dominate the queue. ``unscored_reserve`` rows of each batch
    are reserved for the blocked-unscored cohort (``classification IS NULL`` +
    a non-CLEAN persisted ``jd_content_verdict``); the remainder goes to the
    scored-retraction sweep (and routine unscored rows that are not D5-blocked).
    A small blocked cohort does not waste batch capacity: the unused portion of
    the reserve flows back to the remainder slice. Defaults to half the limit so
    neither cohort can monopolize the batch.

    Returns a list of row objects with ``dedup_key``/``title``/``company``/
    ``jd_full`` fields (dict-style access -- this host's row factory supports
    both index and key access).
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    if unscored_reserve is None:
        unscored_reserve = max(0, limit // 2)
    unscored_reserve = max(0, min(unscored_reserve, limit))

    # Shared eligibility predicate, reused verbatim by both cohort SELECTs below
    # so the eligibility invariant stays single-sourced (mirrors the private
    # original's `_eligible` string).
    eligible = (
        "jd_full IS NOT NULL AND btrim(jd_full) <> '' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM jsonb_array_elements_text(unresolved_reasons) e "
        "WHERE e = ANY(%(reason_codes)s)) "
        "AND (jd_adjudicated_version IS NULL OR jd_adjudicated_version < %(version)s)"
    )
    base_params = {
        "reason_codes": list(JD_CONTENT_REASON_CODES),
        "version": JD_CONTENT_VERSION,
        "clean": JdVerdict.CLEAN.value,
    }

    unscored_rows = raw.execute(
        f"SELECT dedup_key, title, company, jd_full FROM postings "
        f"WHERE {eligible} "
        f"AND classification IS NULL "
        f"AND jd_content_verdict IS NOT NULL AND jd_content_verdict <> %(clean)s "
        f"ORDER BY first_seen DESC "
        f"LIMIT %(unscored_limit)s",
        {**base_params, "unscored_limit": unscored_reserve},
    ).fetchall()

    scored_limit = limit - len(unscored_rows)
    scored_rows = (
        raw.execute(
            f"SELECT dedup_key, title, company, jd_full FROM postings "
            f"WHERE {eligible} "
            f"AND NOT (classification IS NULL "
            f"          AND jd_content_verdict IS NOT NULL "
            f"          AND jd_content_verdict <> %(clean)s) "
            f"ORDER BY (classification IS NOT NULL) DESC, first_seen DESC "
            f"LIMIT %(scored_limit)s",
            {**base_params, "scored_limit": scored_limit},
        ).fetchall()
        if scored_limit > 0
        else []
    )
    return [*unscored_rows, *scored_rows]

"""PORTED from job_finder/db/_score_audits.py
@ dcbde72e65d42662d6790ec53bd619e87fd1d2a0 (private job-cannon).
Ledger L-0079, L-0282.

score_audits -- nightly scoring-audit ledger (sole writer for the table).
Single-writer discipline mirrors _companies.py / _jobs.py / _jd_full.py:
tests/host/test_score_audits_single_writer.py AST-scans jobcannon/host,
jobcannon/web, and jobcannon/db (exempting this file and
jobcannon/db/migrations) for INSERT literals against score_audits and fails
the build if any turn up outside this file.

UNWIRED (m0018 migration plan item 1): the table exists and this module is a
complete, tested writer/reader, but no caller inserts into it yet -- the
nightly audit STAGE that will call select_audit_candidates /
record_score_audit has not landed on this host (no APScheduler,
`claude -p`, or `charlie` CLI exist here yet; see the design note this port
follows). Same "schema-only, writer lands unwired" shape as m0014 /
scan_title_outcomes (L-0287).

Eligibility is snapshot-based (NIGHTLY_MONITOR_SPEC.md §5.1): a job is
auditable when its current sub_scores_json differs from the latest audit
row's snapshot (or none exists). select_audit_candidates follows the
count_scorable single-source design -- coarse SQL candidate select, then the
SAME Python predicate per row.

# PORT-SEAM: calling contract for sub_scores_json / audited_sub_scores_json.
axis_sum / is_audit_eligible operate on JSON as an opaque STRING (matching
private's SQLite TEXT column and this module's own byte-identical fidelity
requirement -- see m0018's migration docstring for why
score_audits.audited_sub_scores_json/axis_deltas_json stay `text`, not
`jsonb`). Host's postings.sub_scores_json (m0015) is `jsonb` -- psycopg
decodes it to a native dict on read, not a string. select_audit_candidates
below casts it at the SQL boundary (`sub_scores_json::text`) so every
snapshot comparison downstream (Python `!=` in is_audit_eligible, SQL `=` in
the skip_count subquery) compares Postgres's own jsonb->text rendering
against itself -- never json.dumps() on a psycopg-decoded dict, which would
re-serialize with different key order/whitespace than Postgres's renderer
and silently break every future equality check both directions. A future
caller writing a NEW audit row must pass record_score_audit the exact string
this module read back via ::text (or re-SELECT ...::text at write time),
never a hand-serialized dict.

# PORT-SEAM: is_audit_eligible / select_audit_candidates drop the #1806
cutover-watermark mechanism (private: `schema_meta` KV row seeded by
migration m209589853, read once per call and threaded into the skip_count
subquery as `audited_at >= cutover`). The host schema has no `schema_meta`
table or any KV-store analog, and -- critically -- no legacy score_audits
data predating this table's own creation to rescue: private's cutover
existed solely to stop a NEW skip-counting rule from retroactively burning
skip budget on OLD rows that were written before the rule existed. On this
host m0018 IS the table's birth; every row in it postdates the rule by
construction, so "count all skipped rows toward the bound" (unconditional,
no watermark) is behaviorally identical to "count only skipped rows at/after
the cutover" when the cutover equals table-creation. The `max_skip_attempts`
bound itself -- the actual #1806 fix -- is fully preserved; only the
now-inapplicable retroactivity guard is gone. See §7 of the design note.

# PORT-SEAM: select_audit_candidates drops `location_policy_verdict_json` /
`effective_location_fit` from both the SQL SELECT and the candidate dict.
Private read `j.location_policy_verdict` off the jobs table; host's
postings table has NO location_policy_* columns at all -- m0015's own
docstring: "No location_policy_* columns (6 in private): no LocationPolicy
class [ported]... location_policy_verdict_json: str | None seam instead"
(a caller-supplied parameter on _assessment_writer.py's WRITE side, not a
readable column). _jobs.py's module docstring notes the same scope-out for
set_location_policy_columns. This is an unconditional drop, not a
probe-for-presence: there is no column, in any form, to read.
get_effective_location_fit itself is not gone -- it lives on unchanged at
jobcannon.engine.classification.get_effective_location_fit for its existing
callers -- it is simply not invoked here, because there is no verdict value
on this host's postings row to feed it.

# PORT-SEAM: select_audit_candidates keeps private's exact selection
algorithm (coarse SQL filter -> Python is_audit_eligible per row -> sort by
axis_sum DESC -> cap at max_jobs) UNCHANGED. The design note's owner
Gate-2 rework (axis-sum-DESC-head -> random.sample(eligible,
min(ceiling, len(eligible)))) changes the CALLER's selection policy, not
this function's contract -- migration-plan item 1 (this PR) covers only the
table + this module; the sampling caller is migration-plan step 5, a future
PR against the still-unwritten audit_stage.py. A caller wanting the
design's sampled-relief behavior today can pass an effectively-unbounded
`max_jobs` and apply random.sample to the returned list itself; capping
here first would sample over the top-N by axis-sum rather than the full
eligible cohort, defeating the poison-rotation relief the design describes.
Filed as a Modularity-note follow-up in this PR's body, not applied inline.
"""

from __future__ import annotations

import json

# PORT-SEAM: sqlite3 import dropped; dispatch goes through psycopg's conn.raw.
from typing import Any

from jobcannon.db.pool import commit_unless_nested
from jobcannon.engine.constants import SUB_SCORE_KEYS
# PORT-SEAM: get_effective_location_fit / utc_now_iso imports dropped -- see
# module docstring PORT-SEAM (location fields dropped entirely; audited_at
# is DB-generated via DEFAULT now(), never passed explicitly by the writer).

_VALID_VERDICTS = ("agree", "dispute", "skipped")


def axis_sum(sub_scores_json: str | None) -> int | None:
    """Sum of the six ordinal axes, or None if missing/malformed/non-numeric."""
    if not sub_scores_json:
        return None
    try:
        scores = json.loads(sub_scores_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(scores, dict):
        return None
    total = 0
    for key in SUB_SCORE_KEYS:
        value = scores.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        total += int(value)
    return total


def is_audit_eligible(
    sub_scores_json: str | None,
    last_audit_snapshot: str | None,
    *,
    score_threshold: int,
    last_audit_verdict: str | None = None,
    skip_attempt_count: int = 0,
    max_skip_attempts: int = 0,
) -> bool:
    """The single-source audit-eligibility predicate (spec §5.1).

    #1799 (T5.4 remediation): a ``skipped`` verdict is never a real audit
    opinion -- it is either poison-item isolation (the job's own content
    could not be audited) or, previously, a fabricated placeholder written
    while a provider outage was misclassified as a per-batch content failure
    (#1799's root cause). Neither represents "this snapshot was actually
    reviewed", so a ``skipped`` row must not consume eligibility the way an
    ``agree``/``dispute`` verdict does. When the latest audit row's verdict is
    ``skipped``, its snapshot is ignored entirely: the job is treated exactly
    as if it had never been audited. This is read-side and structural --
    it un-poisons any existing skipped-with-equal-snapshot key automatically
    (no bulk backfill needed) and prevents any future fabricated-skip class
    from burning eligibility again.

    #1806: the #1799 fix is deliberately unbounded -- a genuinely poison item
    is high-scoring by construction (select_audit_candidates sorts by axis-sum
    descending) and would be re-selected at the head of the queue every night
    indefinitely. The ``first_seen >= now - lookback_days`` candidate filter
    bounds this to ~lookback_days nights worst case, but a recurring upstream
    bug that introduces fresh poison daily would rotate through indefinitely.
    ``max_skip_attempts`` bounds it: once a job has been skipped at least that
    many times at the *current* snapshot (``skip_attempt_count``), a
    ``skipped`` latest verdict falls back to normal snapshot-consuming
    behavior -- the job drops out of the cohort until its snapshot changes
    (a rescore resets the count because the new snapshot has zero prior
    skips). ``max_skip_attempts <= 0`` preserves the unbounded #1799 behavior
    for callers that do not opt in (tests, direct callers).

    # PORT-SEAM: private had a further paragraph here describing the #1806
    # cutover watermark that scoped skip_attempt_count -- dropped along
    # with the mechanism itself (see this module's docstring PORT-SEAM).
    # This predicate was already cutover-agnostic (it only ever received
    # the already-scoped count); nothing below changed.
    """
    total = axis_sum(sub_scores_json)
    if total is None or total <= score_threshold:
        return False
    if last_audit_verdict == "skipped":
        if max_skip_attempts > 0 and skip_attempt_count >= max_skip_attempts:
            return sub_scores_json != last_audit_snapshot
        return True
    return sub_scores_json != last_audit_snapshot


def record_score_audit(
    conn: Any,  # PORT-SEAM: was sqlite3.Connection; bare/wrapped psycopg conn via .raw
    *,
    dedup_key: str,
    model: str,
    verdict: str,
    audited_sub_scores_json: str,
    axis_deltas_json: str | None = None,
    jd_quality_flag: str | None = None,
    notes: str | None = None,
) -> int:
    """Insert one audit row; returns the new id. Sole writer of score_audits.

    # PORT-SEAM: private returned ``int(cur.lastrowid)`` (SQLite) and passed
    ``utc_now_iso()`` explicitly for ``audited_at``; this port uses
    ``RETURNING id`` (psycopg) and omits ``audited_at`` from the INSERT
    entirely, relying on m0018's ``DEFAULT now()`` -- matching every other
    host single-writer INSERT's own "when written" column (see m0018's
    migration docstring). ``?`` placeholders -> ``%s`` (psycopg).
    """
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {_VALID_VERDICTS}, got {verdict!r}")
    # PORT-SEAM: ? -> %s; RETURNING id (psycopg) replaces cur.lastrowid
    # (SQLite); audited_at omitted -- DEFAULT now() fills it (see docstring).
    raw = conn.raw if hasattr(conn, "raw") else conn
    with raw.transaction():
        row = raw.execute(
            "INSERT INTO score_audits "
            "(dedup_key, model, verdict, audited_sub_scores_json, "
            " axis_deltas_json, jd_quality_flag, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                dedup_key,
                model,
                verdict,
                audited_sub_scores_json,
                axis_deltas_json,
                jd_quality_flag,
                notes,
            ),
        ).fetchone()
    commit_unless_nested(raw)
    return int(row["id"])


def select_audit_candidates(
    conn: Any,  # PORT-SEAM: was sqlite3.Connection; bare/wrapped psycopg conn via .raw
    *,
    score_threshold: int,
    lookback_days: int,
    max_jobs: int,
    max_skip_attempts: int = 0,
) -> list[dict]:
    """Coarse SQL select + is_audit_eligible per row; sum-desc, capped.

    JSON parsing stays in Python (json_each in SQL is unsafe on malformed
    JSON without CASE guards — standing lesson), which also keeps this the
    same predicate the parity test replays.

    ``max_skip_attempts`` (#1806): once a job has been skipped this many
    times at its current snapshot, a ``skipped`` latest verdict stops being
    non-consuming and the job drops out of the cohort until its snapshot
    changes. ``<= 0`` preserves the unbounded #1799 behavior.

    # PORT-SEAM: private scoped the skip_count subquery to
    ``audited_at >= <schema_meta cutover watermark>`` (migration
    m209589853). This host has no schema_meta table and no score_audits
    rows predating m0018 to protect -- table-creation IS the cutover, so
    every ``skipped`` row at the job's current snapshot counts toward the
    bound unconditionally. See this module's docstring PORT-SEAM and the
    design note §7 for the full argument. ``jobs`` -> ``postings`` (host's
    table name); ``j``/``a`` aliases kept for a minimal diff against the
    private query shape.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    rows = raw.execute(
        "SELECT p.dedup_key, p.title, p.company, p.location, p.jd_full, "
        # PORT-SEAM: ::text cast -- see module docstring's calling-contract note.
        "       p.sub_scores_json::text AS sub_scores_json, p.first_seen, "
        "       p.jd_content_verdict, "
        "  (SELECT a.audited_sub_scores_json FROM score_audits a "
        "   WHERE a.dedup_key = p.dedup_key ORDER BY a.id DESC LIMIT 1) AS snap, "  # PORT-SEAM: j->p
        "  (SELECT a.verdict FROM score_audits a "
        "   WHERE a.dedup_key = p.dedup_key ORDER BY a.id DESC LIMIT 1) AS last_verdict, "  # PORT-SEAM: j->p
        "  (SELECT COUNT(*) FROM score_audits a "
        "   WHERE a.dedup_key = p.dedup_key AND a.verdict = 'skipped' "
        "   AND a.audited_sub_scores_json = p.sub_scores_json::text) AS skip_count "
        "FROM postings p "
        "WHERE p.sub_scores_json IS NOT NULL "
        # PORT-SEAM: private used strftime epoch math over a naive-UTC ISO
        # first_seen column (SQLite has no native timestamp type); host's
        # first_seen is a real `timestamptz`, so a direct interval
        # comparison replaces the epoch-cast dance -- no 'T' vs ' '
        # separator hazard exists here because there is no string compare.
        "AND p.first_seen >= now() - make_interval(days => %s)",
        (int(lookback_days),),
    ).fetchall()
    candidates = []
    for r in rows:
        if not is_audit_eligible(
            r["sub_scores_json"],
            r["snap"],
            score_threshold=score_threshold,
            last_audit_verdict=r["last_verdict"],
            skip_attempt_count=r["skip_count"],
            max_skip_attempts=max_skip_attempts,
        ):
            continue
        # PORT-SEAM: effective_location_fit computation dropped -- no host
        # postings.location_policy_verdict column to feed it. See module
        # docstring PORT-SEAM.
        candidates.append(
            {
                "dedup_key": r["dedup_key"],
                "title": r["title"],
                "company": r["company"],
                "location": r["location"],
                "jd_full": r["jd_full"],
                "sub_scores_json": r["sub_scores_json"],
                "axis_sum": axis_sum(r["sub_scores_json"]),
                # PORT-SEAM: location_policy_verdict_json / effective_location_fit
                # keys dropped -- no host column (see module docstring PORT-SEAM).
                "jd_content_verdict": r["jd_content_verdict"],
            }
        )
    candidates.sort(key=lambda c: c["axis_sum"], reverse=True)
    return candidates[: int(max_jobs)]

# design-hooks-scoring — decision record

The private design note `design-hooks-scoring.md` governs the scoring
runner port (L-0259, L-0263) and is cited throughout ported code as "the
design note" (e.g. its §4/§5 Q-B entry and the L-0263 seam #4 entry). The
note itself is private-only; this file is the public decision record for
owner rulings on those anchors that bind public code. Entries are dated,
mechanical records — what was decided, when, and what would reopen it.

## 2026-09-29 — L-0263 follow-up rulings (issue #368)

Two follow-ups left open by the scoring-runner port (#367) are settled.
Both rulings bind until revisited under the stated conditions.

### §4/§5 Q-B — `run_scoring` concurrency: keep the thread pool

**Ruling: deferred.** `run_scoring` keeps the in-process
`ThreadPoolExecutor` ported from private, sized from a single host env
var (`JC_SCORE_WORKERS` via `_worker_count`,
`jobcannon/host/scoring_runner.py`). Do not decompose the pool into
per-job procrastinate tasks. Revisit only if scoring throughput or retry
behavior becomes an observed problem — not on architectural preference.

### L-0263 seam #4 — `update_pipeline_status` gap: host-side only (option b)

**Ruling: option (b).** Private's global, open-vocabulary
`update_pipeline_status(conn, dedup_key, "dismissed"/"archived", ...)`
calls — auto-dismiss on exclusion-filter and auto-archive on expiry —
have no public target and will not get one. Both legs stay host-side
only, expressed by state already written or already derivable:
`postings.expiry_status`/`postings.expiry_checked_at` for the expired leg
(`persist_job_expiry_state`, `jobcannon/host/scoring_runner.py`) and
`profiles.exclusions` (m0030) plus the pure `should_exclude` verdict for
the exclusion leg — "is this posting excluded" is recomputed
deterministically, so no per-posting exclusion row is needed. Neither leg
ever becomes a user-facing `pipeline_status` transition:
`jobcannon.db._user_actions` remains the sole writer of `pipeline_status`,
per-user with its closed `{dismissed, applied}` vocabulary. Do not add a
system-authored writer.

Consequence, already in effect and now the settled design rather than a
provisional gate: exclusion-filtered and expired jobs are skipped in
`_process_one_job` (`jobcannon/host/scoring_runner.py`), counted in the
run summary, and never persisted as `dismissed`/`archived`.

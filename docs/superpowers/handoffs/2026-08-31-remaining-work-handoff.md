# Remaining Work Handoff — post Feed & Shell Redesign (Spec 1)

> Written 2026-08-31 by the session that shipped PR #263. Deliberately left
> **untracked** — fold it into your first docs commit if you want it in
> history, or delete it once the tracks below are dispatched.
>
> Structure: three tracks designed to run concurrently. Track A is a
> subagent fleet needing zero owner input until push time; Track B is
> owner-interactive brainstorming that occupies the main session while the
> fleet runs; Track C is a batched question set. Dispatch A, then start B,
> with C's questions folded into B's opening message.

## Baseline (verify before dispatching anything)

As of 2026-08-31T00:50Z — a drifted baseline invalidates every
"zero regressions" claim downstream, so re-verify first:

- `main` = `57525d9` ("feat: redesign feed density, chips, masthead, and
  posting detail (#263)"), local == origin, tree clean.
- Full suite: **3311 passed, 14 skipped, 0 failed** (~505s locally).
  `POSTGRES_ADMIN_DSN` must be set for host tests; `tests/host/conftest.py:90`
  creates unique-per-run DBs, so **parallel agents running pytest cannot
  collide** — lean on that.
- `ruff check .` and `ruff format --check .` clean tree-wide.

Verification move: `git fetch && git log --oneline -1 origin/main`, then kick
the full suite in the background (`uv run --no-sync --active pytest -q
--tb=short > baseline.log 2>&1` — drop `--active` inside any worktree) while
you proceed with the owner questions. Don't dispatch implement agents until
it lands green.

## Track A — independent-fix fleet (dispatch first, runs in background)

Six open issues, mutually orthogonal (different files/subsystems — no merge
or dedup stage needed). Three are dispatch-ready; three need a recon step
**inside the workflow** before their implementer runs — do not let a fleet
agent invent policy silently.

**Dispatch-ready:**

1. **#261 — detail route 500s on DB outage.** `jobcannon/web/posting_detail.py`:
   wrap `connection_factory` / `get_posting_detail` like
   `_read_preview_postings` (onboarding.py) and return an inline
   "details unavailable right now" fragment. **Never 404** — that claims the
   posting doesn't exist. Nuance the implementer must verify against
   `_posting_row.html`'s swap wiring: htmx ignores 5xx bodies by default, so
   either return 200 with a distinct `data-detail-unavailable` attr (document
   why in the route), or add explicit hx error handling for a 503. Test:
   monkeypatch the connection factory to raise; assert the fragment renders
   and the expander doesn't dead-click.
2. **#246 — stale docstring** in `posthog_admin.py` ("no automatic retry
   configured") vs `tasks.py` `RetryStrategy(max_attempts=5)`. Mechanical.
3. **#243 — `import_legal_text.py` conflates Effective date and Last updated
   on re-import.** Small; implementer reads the import function + existing
   tests first.

**Recon-first (recon agent produces the direction, implementer executes it):**

4. **#245 — `upsert_job` lost update:** concurrent upserts on the same
   `dedup_key` wholesale-replace parser-owned `unresolved_reasons`. Recon
   question: merge semantics at the SQL level (array/jsonb union in the
   `ON CONFLICT` clause vs read-modify-write with a version check vs advisory
   lock). Answer depends on the actual column type and every writer of that
   column — recon must enumerate writers before choosing.
5. **#258 — static-asset caching policy** (from #257; now also covers Spec 1's
   `favicon.svg` / `apple-touch-icon.png`). Policy decision: recon how assets
   are actually served (Flask static? reverse proxy?), then recommend
   cache-control + busting strategy (content-hash filenames vs versioned
   query params). Surface the recommendation to the owner with the Track C
   batch if it changes serving infrastructure; implement directly if it's
   config-only.
6. **#264 — strip workplace tokens from scraped `location` at ingest.** Recon
   questions: where in the pipeline to normalize, and the backfill decision —
   forward-only, backfill existing rows, or normalize-on-read. Forward-only
   is the likely recommendation (UI `dedupe_location` already covers display,
   demoting to safety net), but verify row counts before asserting backfill
   cost either way. Owner sign-off on the backfill choice belongs in the
   Track C batch.

**Fleet shape:** Workflow tool, per-item `pipeline()` of
(recon where marked) → implement → verify, all sonnet-pinned (hook-enforced).
Reuse the proven template in
`docs/superpowers/plans/2026-08-30-feed-shell-redesign.md` — including its
schema fix: **no `required` arrays** in agent schemas, so a step-0
`{"blocked": ...}` halt validates. Pre-warm workspaces OUTSIDE the workflow
(stall-retry rule 9), and since these items want **separate branches**,
pre-warm one worktree per item — worktree discipline applies (no `git stash`;
`git worktree remove`, never `rm -rf`; plain `uv run --no-sync` without
`--active` inside them).

**Branch/PR strategy:** one branch + PR per item, squash-merged (repo
convention; `deleteBranchOnMerge` on). **Auto-merge is disabled at the repo
level** — merge manually with `gh pr merge <N> --squash` after checks.
CodeQL default setup gates every PR with no local equivalent
(`project_jobcannon_codeql_pr_gate` memory: hostname-parse, never substring;
query alerts via `ref=refs/pull/<N>/head`). Commit subjects ≤72 chars
(`validate-commit.sh` hook).

## Track B — Spec 2: profile editor (owner-interactive, main session)

Entry point is the **brainstorming skill** — this cannot be fleet-ified; it
runs in the main session while Track A's fleet works. Seeds:

- Ratified scope (Spec 1 doc, Decisions §2, verbatim): "**Profile surface**:
  full edit form — deferred to Spec 2, including new write paths for
  `experience_summary` and `target_locations` (no writer exists today) and
  per-user pipeline stats."
- Original feedback item it answers: "how is the user supposed to see their
  profile?" — the nav still has no profile link.
- **#262 must be resolved by this spec:** `POST /start` writes the
  anon/pending profile domain while the new `_profile_prefill()`
  (`jobcannon/web/onboarding.py:539–578`, fail-open) reads the clerk domain
  via `_current_identity()` / `VERIFY_REQUEST`. The editor's write path must
  mirror that read path — either route authed `/start` submissions into the
  clerk domain or redirect authed users off `/start` entirely.
- Existing semantics to respect: `_profiles.py` submits literally by design
  (the reason prefill was a safety patch); `get_profile` is the read
  primitive.

After spec approval → **writing-plans** with the same standing directive
("structure the plan to maximally leverage subagent fleets and dynamic
workflows… optimize parallelization, reduce gates, maximize throughput
without sacrificing quality") → Workflow execution off the Spec 1 plan
template.

## Track C — owner-gated / blocked (batch into ONE opening message)

The serial bottleneck in this whole handoff is owner approval, not compute.
Open the session by batching:

1. **Blanket push/PR approval for Track A**: "Track A is 6 independent
   fixes; requesting approval now to push branches, open PRs, and
   squash-merge each as it goes green." Without this, six fixes serialize at
   the gate.
2. **#258 / #264 decisions** if recon surfaces owner-visible choices
   (serving infra change; backfill or not).
3. **#167** — wiring CLA Assistant Lite into required merge checks: repo
   settings mutation, owner call.
4. **#138 / #139** — F7 scope decisions (ATS company-lifecycle epic timing;
   hosted redrive). Strategic; just ask whether to schedule.
5. **#221** stays blocked ("once `feed_state` has a writer" — same gate as
   numeric rank display). **#133** is business/legal, not agent work. Neither
   needs a question; listed so nobody re-triages them.

## Execution playbook (references, not copies — read these before dispatch)

- **Skill `parallelizing-with-workflow`** — stall-kill rules, model tiering,
  artifact-files-not-payloads.
- **Plan doc** `docs/superpowers/plans/2026-08-30-feed-shell-redesign.md` —
  the proven workflow script: phases, schemas (no `required`), pre-warm
  Task 0, WIP-commit discipline.
- **Memories:** `project_windows_tail_monitor_hazard` (never `tail -F |
  grep` a live journal — wc-l offset poll loop), `project_jobcannon_codeql_pr_gate`,
  `project_claude_cli_upstream_quirks` (background Bash `| tail` can return
  empty on exit 0 — log to a file, echo the exit code).
- **Stall diagnosis before any relaunch:**
  `python ~/.claude/scripts/classify-workflow-stall.py <run-dir>` —
  API_SILENCE means throttling (Spec 1's run had exactly one; retry-idempotency
  recovered it in seconds because implementers commit WIP as they go — keep
  that property).
- **Test invocation:** `uv run --no-sync --active pytest -q --tb=short`
  (main checkout) / drop `--active` in worktrees. Lint:
  `uv run --no-sync ruff check .` + `ruff format --check .`.

## Opening choreography (the max-parallelism order)

1. Verify baseline; start full suite in background.
2. Post the Track C batched question message (includes the blanket approval
   ask); simultaneously pre-warm Track A worktrees.
3. Baseline green → dispatch the Track A workflow (background).
4. Run the Spec 2 brainstorm with the owner in the main session while the
   fleet works.
5. As Track A items complete: push/PR/merge under the blanket approval,
   monitor via offset-poll, classifier on any stall.
6. Spec 2 spec approved → writing-plans → its own workflow run (check fleet
   contention before dispatching a second heavy run alongside Track A).

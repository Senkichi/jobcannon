# Deploy runbook

Operational procedure for standing up the hosted Job Cannon skeleton on
Render. This document is public-safe by design: no strategy narrative, no
cost figures, no owner-identifying detail — those live in the private repo's
planning notes. Everything below is either already public (Render's own
product surface) or code-facing (env var names, log lines, CLI usage).

All owner-gated steps here require Render billing (paid resources); nothing
in this document runs itself.

## 1. Prereqs

- A Render account with billing configured.
- A Clerk **production** instance (a development instance is fine for local
  testing but should not back a public deploy).
- This repo connected to Render, then **New > Blueprint** deploying from
  `render.yaml` at the repo root. Render provisions `jobcannon-db`, then the
  `jobcannon-web` and `jobcannon-worker` services.
- Both services pin `PYTHON_VERSION` in `render.yaml` so a new deploy never
  silently inherits Render's current default instead of a version this
  repo's CI actually exercises — keep that pin in sync if CI's tested
  Python version(s) change.

## 2. Secrets to fill

`render.yaml` marks these `sync: false` — Render prompts for them once during
the Blueprint creation flow (or they can be set later per-service in the
Render dashboard). None of them are committed anywhere in this repo.

| Env var | Service(s) | Source |
|---|---|---|
| `CLERK_SECRET_KEY` | web | Clerk dashboard → API Keys → Secret key |
| `CLERK_JWT_KEY` | web | Clerk dashboard → API Keys → Advanced → JWT public key (enables networkless RS256 verification — required, see `jobcannon/web/auth.py`) |
| `CLERK_PUBLISHABLE_KEY` | web | Clerk dashboard → API Keys → Publishable key (`pk_live_...`). Loads clerk-js in the browser so it can complete Clerk's cross-domain sign-in handshake: clerk-js makes a credentialed call to the Frontend API host (`clerk.<domain>`), the browser attaches the `__client`/`__client_uat` cookies set on that host, and clerk-js writes the returned session token as `__session` on this host. Without it, the hosted Account Portal sign-in redirect never leaves this host a session and every signed-in human 401s forever (issue #149). Publishable keys are public by design (meant to ship to the browser); `sync: false` here only keeps every `CLERK_*` var's provenance in one place. The web service refuses to boot without it, or if it isn't a well-formed `pk_live_`/`pk_test_` key. **Two Clerk-side prerequisites this variable cannot express:** (1) Clerk dashboard → Sessions → Allowed origins must permit every origin listed in `CLERK_AUTHORIZED_PARTIES` (on a production instance the instance's own domain and subdomains are permitted by default; a custom-domain or satellite change re-opens this), otherwise clerk-js's credentialed FAPI call is refused and the symptom is exactly #149 again; (2) the session token's `azp` claim is stamped from the browser `Origin` of the page that loaded clerk-js, so it must match an entry in `CLERK_AUTHORIZED_PARTIES` — after any deploy that changes hosts, decode one live `__session` JWT (unverified, `azp` claim only) and confirm it. |
| `CLERK_AUTHORIZED_PARTIES` | web | Comma-separated list of the origins allowed to present a session token for this deploy (e.g. the Render web service's public URL) — **bare origins, no trailing slash** (e.g. `https://jobcannon.dev`, not `https://jobcannon.dev/`; each configured value still has any trailing slashes trimmed). This is an operator-chosen value, not something copied verbatim from Clerk — it is Clerk's `azp` replay-defense check. |
| `CLERK_WEBHOOK_SIGNING_SECRET` | web | Clerk dashboard → Webhooks → the endpoint created in step 4 → Signing Secret (`whsec_...`) |
| `JC_SECRET_KEY` | web | Operator-generated random secret (Flask session-signing key) — not sourced from any external dashboard, unlike the Clerk/PostHog rows. The web service refuses to boot without it. |
| `CLERK_SIGN_UP_URL` | web | Operator-chosen value: the Clerk-hosted sign-up page URL for this deploy (Clerk dashboard → your application's hosted pages; typically `https://<your-clerk-subdomain>.accounts.dev/sign-up` or your custom domain equivalent). The 401 page's sign-up link renders from it. |
| `POSTHOG_API_KEY` | web, worker | PostHog project settings → Project API Key |
| `JC_ANALYTICS_PSEUDONYM_SALT` | web, worker | Operator-generated random secret, same shape as `JC_SECRET_KEY` — but a DIFFERENT value from it; this is the HMAC key `jobcannon/host/posthog_client.py` uses to derive the pseudonymous identifier sent to PostHog, so it must not double as session-signing material. Required on both services (each builds its own PostHog client). If left unset, PostHog fan-out disables itself silently — events still write to Postgres, and the raw Clerk user id is never sent as a fallback. |

`DATABASE_URL` is wired automatically on both services via `render.yaml`'s
`fromDatabase` reference — never set it manually.

`POSTHOG_HOST` is committed in `render.yaml` (not `sync: false`) on both
services, routing PostHog ingestion to the EU region
(`https://eu.i.posthog.com`) — it's a routing choice, not a secret, so it
doesn't need per-deploy filling in. Both services build their own PostHog
client through the same seam (`jobcannon/host/wiring.py`), so the value
must stay identical on both.

## 3. First boot ordering

**Migration ordering guarantee (issue #196).** `jobcannon-web` and
`jobcannon-worker` deploy independently on Render, with no guarantee about
which one finishes first — a release that reads a table/column its own
migration creates otherwise has a window where web serves against the old
schema until the worker happens to reboot. `render.yaml`'s `jobcannon-web`
service closes that window with a `preDeployCommand` that runs
`python -m jobcannon.db.migrate` (Render docs: "Command that runs after the
build command but before the start command"; on failure, "the entire deploy
fails" — a broken migration blocks the new web code from ever going live
against a schema it doesn't match, render.com/docs/deploys). That command
resolves `DATABASE_URL` the exact same way the worker does
(`load_host_config`) and calls the same `run_migrations()`.

Because both web's pre-deploy step and the worker's boot-time call can now
run against the same database — sequentially on a normal deploy, but
possibly overlapping if a worker restarts mid-deploy — `run_migrations()`
takes a session-level Postgres advisory lock (`jobcannon/db/migrate.py`'s
`_ADVISORY_LOCK_KEY`) for its entire ledger DDL / read / apply sequence, so
two concurrent callers serialize instead of racing the `schema_migrations`
INSERT. The worker's boot-time call is no longer the single migration
authority; it stays as an idempotent, lock-serialized belt-and-braces —
still exercised on every worker boot in case a worker ever comes up before
web's pre-deploy has run (e.g. first deploy of a brand-new environment,
where nothing has applied any migration yet).

On first boot the worker owns, in order: our schema ledger
(`run_migrations`, a no-op if web's pre-deploy already applied everything),
then the four engine seams, then procrastinate's own queue schema (guarded
apply, via a `to_regclass('public.procrastinate_jobs')` existence probe —
see the Two-Schema-Authorities design in the worker's module docstring),
then `run_worker()`.

**Verifying after a deploy.** Render's deploy logs for `jobcannon-web` show
the pre-deploy step running before the start command — look for
`schema_migrations: N applied, M pending` and, if any were newly applied,
one `applied migration V (name)` line per migration
(`jobcannon/db/migrate.py`). To confirm the ledger itself:
`SELECT version, name, applied_at FROM schema_migrations ORDER BY version;`
against the live database. After merging any render.yaml change, also
confirm the Blueprint actually picked it up — a `render.yaml` edit only
takes effect on the next Blueprint sync. **The Render API field for this is
namespaced, not flat**: `serviceDetails.envSpecificDetails.preDeployCommand`
is the field to read — a flat `serviceDetails.preDeployCommand` reads
`null`/absent even when the Blueprint applied the command correctly
(verified 2026-08-27; a plain `serviceDetails.preDeployCommand` read was
mistakenly believed to be the authoritative field before this correction).
Even stronger than reading the field is proving pre-deploy actually RAN:
`GET /v1/services/<svc>/events` shows a `pre_deploy_started` event followed
by `pre_deploy_ended` with `preDeployStatus: "succeeded"` for that deploy
(verified 2026-08-27 on deploy `dep-da7uc6814ptc73969a5g`) — that pair is
the real proof, independent of which field name happens to be current.

**Rollback caveat.** A rollback of `jobcannon-web` (or `jobcannon-worker`)
to a commit that predates a migration already recorded in the
`schema_migrations` ledger **FAILS**, not serves benignly: pre-deploy
re-runs `python -m jobcannon.db.migrate` on the rolled-back code, which
doesn't know that ledger row, so the orphan guard (`orphans = applied -
known` in `jobcannon/db/migrate.py`) raises `DatabaseNewerThanCodeError`
and aborts the deploy. A rolled-back worker fails at boot for the exact
same reason — that guard predates this PR; issue #196 only added the web
pre-deploy path that now also hits it.

The escape hatch is the `JC_MIGRATE_ALLOW_NEWER_DB` config setting
(truthy values: `1` or `true`): set it on the rolling-back service's
environment — Render dashboard → Environment, or the Render API — *before*
triggering the rollback, and the orphan check logs a WARNING naming the
unknown version(s) instead of raising, then continues normally. **Remove
the var again once the rollback is resolved** — it's a per-incident
override, not a standing config value. This is safe ONLY because every
migration in this repo is expand-only (additive, backward-compatible with
the previous release — the same discipline §10 documents for the general
rollback case): the rolled-back code simply never reads the newer
column/table it doesn't know about. A rollback across a genuinely
contract-shaped migration (a hypothetical future `DROP COLUMN` / type
narrowing / constraint tightening) is **not** safe to override this way —
that needs a database restore, never `JC_MIGRATE_ALLOW_NEWER_DB`.

**Migration/writer ordering also inverted, not just eliminated.** Pre-deploy
runs migrations *before* the new web code goes live, which flips the
ordering a data-backfill-shaped migration wants: one written to rewrite
rows that only the *new* release's writer produces (e.g.
`m0010_events_referrer_host.py`'s "Deploy order" docstring, which predates
this guarantee) previously wanted migration-after-code, since only after
web deployed would there be new-format rows to backfill. Now migrations
always land before the new writer is live, so a backfill of that shape
strands every row the outgoing release's writer produced in the gap
between migration commit and web's cutover — pre-deploy closes the
old race but opens this one for that migration *shape* specifically.
A future migration that rewrites rows a not-yet-deployed writer will
produce must either be order-independent (safe against running before its
own writer exists) or ship with an explicit follow-up sweep; it can no
longer rely on "the worker will boot after the writer's release lands."
`m0010_events_referrer_host.py` itself is already applied on every existing
deploy and is **not** affected by this — it's cited above only to
illustrate the migration *shape* that needs the caution going forward.

### Migration deploy-safety guard

`tests/test_migration_deploy_safety.py` (issue #199) makes the two
discipline rules above mechanical rather than prose-only. It derives every
input from the `MIGRATIONS` registry (`jobcannon/db/migrations/__init__.py`)
— never a hand-maintained version list — so a new migration is covered the
moment its module lands, with no guard-file edit required. It needs no
database and runs in the default `tests/` sweep, not `tests/host/`.

1. **Contract-shaped DDL against a pre-existing table/column fails by
   default.** `DROP COLUMN`, `DROP TABLE`, `ALTER COLUMN ... TYPE`,
   `ADD COLUMN ... NOT NULL` without `DEFAULT`, `ADD CONSTRAINT ...
   CHECK`/`UNIQUE`, and `ALTER COLUMN ... SET NOT NULL` all fail the guard
   when they target a table/column an EARLIER migration created (the same
   statement acting on a table/column the CURRENT migration itself just
   created is fine — nothing running the previous release ever queried
   it). A migration that genuinely needs one of these shapes (e.g. m0003's
   CHECK widen, which drops and re-adds the constraint by name) declares
   it deliberately: a bare `contract_step = True` module attribute plus a
   docstring paragraph starting `Contract justification:` explaining why
   the change is still safe for the previous release during the
   zero-downtime overlap window. `jobcannon/db/migrate.py`'s
   `_apply_migration` then logs a `CONTRACT-STEP migration ...` WARNING
   line when applying it, so it's visible in the Render deploy log — that
   is the exact line to check before deciding whether
   `JC_MIGRATE_ALLOW_NEWER_DB` is safe to use for a rollback across it
   (it is **not**, per the Rollback caveat above).
2. **An inverted `Deploy order: ... AFTER` backfill fails by default.**
   Pre-deploy now always runs migrations before the new release's writer
   goes live, inverting the ordering a "run this migration AFTER the
   writer deploys" docstring assumed. A migration whose docstring matches
   that pattern and is genuinely safe to run before its writer exists
   (e.g. m0010, whose backfill UPDATE is a no-op the moment no row still
   carries the old key) declares `inverted_order_safe = True` plus a
   docstring explanation using the word "idempotent", so the guard can
   confirm an actual explanation exists, not just the bare flag.

Both checks are pure static analysis over the registry's own SQL statement
strings (a small paren/quote-depth-aware tokenizer plus anchored regexes —
no `sqlglot`/`pglast` dependency) and are deliberately conservative: a
statement shape or column reference the scanner can't positively prove is
safe is treated as contract-shaped rather than silently passed.

Expect these log lines on a fresh database's first worker boot (the ledger
`name` column — and therefore the second `%s` migrate.py logs — is the
migration file's stem, not its description):

```
waiting for schema_migrations advisory lock
schema_migrations: 0 applied, 9 pending (m0001_initial_schema, m0002_scan_health_log, m0003_companies_scan_columns, m0004_users_consent, m0005_postings_embedding, m0006_analytics_consent_version, m0008_profiles_comp_floor, m0009_postings_jd_content, m0010_events_referrer_host)
applied migration 1 (m0001_initial_schema)
applied migration 2 (m0002_scan_health_log)
applied migration 3 (m0003_companies_scan_columns)
applied migration 4 (m0004_users_consent)
applied migration 5 (m0005_postings_embedding)
applied migration 6 (m0006_analytics_consent_version)
applied migration 8 (m0008_profiles_comp_floor)
applied migration 9 (m0009_postings_jd_content)
applied migration 10 (m0010_events_referrer_host)
applying procrastinate schema (first boot)
```

`waiting for schema_migrations advisory lock` and
`schema_migrations: N applied, M pending` are logged on **every** call to
`run_migrations` — including every subsequent boot, not just the first —
because the advisory-lock acquire and the ledger read always run; only the
`applied migration V (name)` lines are conditional on there being pending
work. So on every subsequent boot, expect those same two lines again with
`M` at `0` and no `applied migration ...` lines (already-applied migrations
are skipped), and the procrastinate probe finds `procrastinate_jobs`
already present, so neither schema step re-runs its DDL.

**The web service needs the database, but not the schema, to report
healthy.** `/healthz` runs a bounded (2.5 s) pooled `SELECT 1` — schema-free
by design, so the web service goes green as soon as the database itself
accepts connections, independent of the worker's migration authority. If the
probe fails, `/healthz` returns 503 and Render's health checks replace the
instance instead of leaving a wedged one in rotation (2026-08-26 incident:
a web instance whose DB path died post-boot kept serving corpus-empty pages
behind a static healthz indefinitely). Each 503 also logs the failure
detail (exception type, or the hang-timeout note when the probe never
returned) plus pool stats at WARNING.

**A wedged pool self-heals without a redeploy.** Both services run a pool
watchdog (`jobcannon/db/pool.py`): every `JC_POOL_WATCHDOG_S` seconds
(default 15; `0` disables) it runs the same bounded probe `/healthz` uses,
and after 3 consecutive failures it swaps in a freshly built pool
(build-first, then a bounded close of the old one), rate-limited to one
recycle per 60 s. This targets the 2026-08-26 terminal mode directly: an
established connection that goes dark while its TCP flow keeps ACKing wedges
psycopg_pool's untimed reset/check round-trips, and a wedged pool never
attempts another connect on its own — even though fresh connects were proven
to succeed on every instance restart. A recycle logs at CRITICAL with the
last probe detail and pool stats; probes double as an app-level keepalive on
the warm connection. Recycling recovers only what a fresh connect can reach
— if the network path itself is down, probes keep failing, `/healthz` stays
503, and platform replacement remains the backstop.

**The pool is fork-safe, and preload is explicit.** The 2026-08-26
incident's actual root cause: the web app was built before gunicorn forked
its workers (production did this even without `--preload` on the start
command — mechanism never identified; the flag is now committed in
`render.yaml` precisely so the topology is pinned rather than
environment-dependent). Each worker inherited a pool whose background
threads (psycopg_pool's workers, the watchdog) did not exist in the child
and whose one connection socket was shared across processes — the shared
connection died within seconds and the child could never build another
(refill tasks queue to threads that aren't there), with zero errors logged.
`jobcannon/db/pool.py` now registers an `os.register_at_fork` hook: after
every fork the child abandons the inherited pool (never closes it — that
would write into the parent's socket) and builds its own, with its own
threads and watchdog. Cost of explicit preload: the master keeps a small
pool (one idle connection) and its own watchdog — harmless, and it makes
the master's pin line a boot-sequence landmark. Log signature to expect on
a healthy boot: one `pinned DB hostaddr ... (pid N)` line for the master's
pre-fork build, plus one `pool rebuilt after fork in pid M` (with its own
pin line) per gunicorn worker.

Clerk auth + consent resolution still
fail closed (no session / no consent) without a live schema, and routes that
read/write `postings`, `companies`, etc. will error until the worker has
applied both schemas; that window is normally seconds on a fresh deploy and
is otherwise closed once the worker service reports healthy in the Render
dashboard.

## 4. Webhook endpoint registration

In the Clerk dashboard, add a webhook endpoint pointing at
`https://<jobcannon-web-service>.onrender.com/webhooks/clerk`, subscribed to
`user.created`, `user.updated`, `user.deleted`. Copy the endpoint's Svix
Signing Secret into `CLERK_WEBHOOK_SIGNING_SECRET` (step 2) — the route
verifies every delivery against it and returns 400 on a bad/missing
signature (never a 500 on untrusted input).

## 5. Staging scan (spec §8)

Before committing real volume, verify the wedge seed list and run a small
batch:

```
python scripts/preseed_corpus.py data/seed_companies.csv --verify
python scripts/preseed_corpus.py data/seed_companies.csv --limit 8
python scripts/scan_block_report.py --since 2
```

**Run `--verify` from a normal residential network, not from a cloud/CI
host.** It performs plain GET requests against public ATS board URLs, and
Greenhouse's CDN (CloudFront) has been observed returning HTTP 406 to
datacenter-IP ranges even for a perfectly valid slug — a `--verify` run from
Render's own build environment, a CI runner, or any other datacenter IP can
report false failures. Confirm the `--limit 8` batch's postings land and the
block report is clean before proceeding to the full pre-seed.

## 6. Full pre-seed

```
python scripts/preseed_corpus.py data/seed_companies.csv
```

Upserts every wedge company and enqueues one `scan` job per company through
the queue (never bypassing procrastinate) — this is also the scheduler's
first real-volume proof.

## 7. ASN load test

The full pre-seed above **is** the load test's volume — no separate step
generates traffic. After a full scan cycle completes:

```
python scripts/scan_block_report.py --since 24
```

Use the report to make two decisions:

- **Worker concurrency:** pin `JC_WORKER_CONCURRENCY` based on the
  observed error/block rate. **Pinning this means editing `render.yaml`'s
  `JC_WORKER_CONCURRENCY` value and merging/deploying that change** — this
  var is not `sync: false`, so Render treats it as blueprint-owned and
  reapplies the committed value on every blueprint sync. A dashboard-only
  override is silently reverted the next time the Blueprint syncs.
- **Static outbound IP:** decide whether a static IP add-on is
  warranted based on the platform-level block/error shares in the report.

**Rulings (2026-08-26, production corpus):** both decisions were made
against block reports over a multi-day window and a single-day window with
the full company corpus rotating daily.

- **Worker concurrency stays at 1.** The observed platform-level
  error/block shares did not argue for a change in either direction, and
  the full rotation completes comfortably within its daily window — the
  binding constraint on raising concurrency is worker memory on the
  current plan (see the `JC_WORKER_CONCURRENCY` comment in `render.yaml`),
  not throughput or anti-bot pressure. Revisit alongside a plan
  re-evaluation, not independently of one.
- **Static outbound IP: not warranted at current posture.** The report
  showed no egress-reputation signal that a dedicated IP would address.
  Revisit only if scan health degrades in a way that implicates
  shared-egress reputation specifically (per-platform block shares rising
  without a corresponding code or corpus change). The underlying report
  numbers live in the operator's private ops notes, deliberately not in
  this repo.

## 8. Storage alert

`db_storage_check` (a daily periodic on the worker, default `17 6 * * *`
UTC) reports `pg_database_size` against `JC_DB_STORAGE_LIMIT_MB` (default
5120, derived from `jobcannon-db`'s `diskSizeGB: 5` in `render.yaml` — 5GB *
1024MB; `tests/host/test_render_config.py` guards the two values against
drift) on **every** daily tick, via a `scan_health_log` row
(`payload->>'source' = 'db_storage_check'`) — by design, so the block report
can read these rows as a liveness signal even when usage is nowhere near the
limit. Once usage crosses 80% of the limit, that same tick additionally logs
at ERROR. Render's own managed-Postgres storage threshold emails are the
belt; this check is the suspenders, surfacing the same signal in-app
alongside every other health event.

Upgrade procedure depends on what's actually under pressure — `plan`
(RAM/compute) and `diskSizeGB` (disk) are decoupled, and bumping one does
not by itself change the other:

- **Storage pressure:** raise `diskSizeGB` in `render.yaml` (keep
  `JC_DB_STORAGE_LIMIT_MB` at `diskSizeGB * 1024`, the relationship the
  drift guard enforces), then deploy. This is a genuinely no-downtime
  change.
- **Compute pressure:** bump the database's `plan` in `render.yaml` (see the
  plan-value table in Render's dashboard for the next tier up), then
  deploy. Expect a few minutes of database unavailability while Render
  spins up the new instance for this path — unlike the storage-only path
  above, a plan change is NOT no-restart.

### Orphaned `doing` jobs

If the worker process is hard-killed mid-job (e.g. Render's redeploy grace
period expires before an in-flight scan finishes), that job's
`procrastinate_jobs` row stays `status = 'doing'` permanently.
Procrastinate's stalled-worker pruning only deletes the dead
`procrastinate_workers` row (which sets the job's `worker_id` to `NULL` via
`ON DELETE SET NULL`) — nothing in procrastinate itself resets a `doing`
job's status. This does **not** block future scans of the same company
(queueing locks are per-defer, so a stuck row cannot wedge re-enqueueing),
but the orphaned row should still be cleaned up periodically:

```sql
SELECT id, task_name FROM procrastinate_jobs WHERE status = 'doing' AND worker_id IS NULL;
```

This is handled automatically: `reclaim_orphaned_jobs` is a periodic task on
the worker (default `*/15 * * * *` UTC, override via `JC_RECLAIM_CRON`,
`periodic_id="reclaim_orphaned_jobs"`) that selects exactly the rows the
query above finds and retries each one through procrastinate's `JobManager`
(`doing` -> `todo`, so a worker picks it back up on the next tick). Each
retry is isolated: `retry_job_v2` never touches `queueing_lock`, so an
orphan can collide with an unrelated `todo` job already holding the same
lock — that row is logged and skipped rather than aborting the tick, and the
count shows up in `skipped`. To inspect a run's result, find that task's
`job_processed` line in the worker log — the periodic tick logs its return
value inline, e.g. `{"reclaimed": 2, "skipped": 0, "disposition": "retry",
"job_ids": [123, 124]}` — or run the query above directly; a healthy fleet
keeps it empty between ticks.

## 9. Guest demo

**Requires the demo-shell PR (jobcannon #20 — guest demo + empty states);
skip this step if `scripts/seed_guest_demo.py` is not yet present in your
deploy.**

Once the corpus has live postings (after step 6 or 7):

```
python scripts/seed_guest_demo.py
```

Seeds the sentinel guest user + a canned-but-realistic profile. `/demo`
(public, unauthenticated) then shows the live corpus stats alongside that
profile card.

## 10. Rollback

Redeploy the previous commit from the Render dashboard (or `render deploys
rollback` via the CLI). Migrations in this phase are additive-only (no
column drops, no destructive schema changes), so rolling back the
application code is safe without a corresponding down-migration.

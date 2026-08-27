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

`jobcannon/worker/__main__.py` is the **single migration authority** in this
deploy: `create_app()` never runs migrations. On first boot the worker owns,
in order: our schema ledger (`run_migrations`), then the four engine seams,
then procrastinate's own queue schema (guarded apply, via a
`to_regclass('public.procrastinate_jobs')` existence probe — see the
Two-Schema-Authorities design in the worker's module docstring), then
`run_worker()`.

Expect these log lines on a fresh database's first worker boot (the ledger
`name` column — and therefore the second `%s` migrate.py logs — is the
migration file's stem, not its description):

```
applied migration 1 (m0001_initial_schema)
applied migration 2 (m0002_scan_health_log)
applied migration 3 (m0003_companies_scan_columns)
applied migration 4 (m0004_users_consent)
applied migration 5 (m0005_postings_embedding)
applying procrastinate schema (first boot)
```

On every subsequent boot, `run_migrations` logs nothing new (already-applied
migrations are skipped) and the procrastinate probe finds
`procrastinate_jobs` already present, so neither schema step re-runs.

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

**Migration 7 (`revoked_subjects`)** previously had a real, not-benign
deploy-order dependency on this section's general worker-first case (a
missing table would raise inside `jobcannon.db._revoked_subjects.
revoke_subject`, called from `jobcannon/web/account.py`'s account-deletion
route and `jobcannon/web/webhooks.py`'s `user.deleted` handler). That
dependency is resolved by issue #196's web pre-deploy migration step
(#197) — see `m0007_revoked_subjects.py`'s docstring for the current
guarantee and the narrow self-healing window that remains.

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

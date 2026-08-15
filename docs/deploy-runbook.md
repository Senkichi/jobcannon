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
| `CLERK_AUTHORIZED_PARTIES` | web | Comma-separated list of the origins allowed to present a session token for this deploy (e.g. the Render web service's public URL). This is an operator-chosen value, not something copied verbatim from Clerk — it is Clerk's `azp` replay-defense check. |
| `CLERK_WEBHOOK_SIGNING_SECRET` | web | Clerk dashboard → Webhooks → the endpoint created in step 4 → Signing Secret (`whsec_...`) |
| `POSTHOG_API_KEY` | web, worker | PostHog project settings → Project API Key |

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

**The web service is DB-free until the worker has booted at least once.**
`/healthz` returns a static dict with no DB touch, and Clerk auth + consent
resolution both fail closed (no session / no consent) without a live schema
— so `jobcannon-web` starts and serves `/healthz` green even if it wins the
race against the worker's first boot. Routes that read/write `postings`,
`companies`, etc. will error until the worker has applied both schemas; this
window is normally seconds on a fresh deploy and is otherwise closed once
the worker service reports healthy in the Render dashboard.

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

Requeue (re-defer the same task) or delete each row found, as appropriate. A
proper reclaim maintenance task is tracked as jobcannon issue #19 — until
that lands, this is a manual operator step.

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

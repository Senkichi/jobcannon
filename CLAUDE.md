# CLAUDE.md — Project Invariants for Worker Agents

## Commands

```bash
uv sync --dev                        # install dependencies (dev extras: pytest, ruff)
uv run ruff check .                  # lint
uv run ruff format --check .         # format check (CI fails on unformatted code)
uv run pytest -q --tb=short          # run tests
```

These are the exact commands CI runs (`.github/workflows/ci.yml`, `test`
job) — run them locally before opening a PR.

## Tests: two tiers, one needs Postgres

- `tests/engine/` — DB-free, runs anywhere with no setup.
- `tests/host/` — needs a live Postgres 17 + pgvector instance. Set
  `POSTGRES_ADMIN_DSN` (a superuser DSN with `CREATEDB` privilege) before
  running this tier; `tests/host/conftest.py` reads it directly and the
  fixtures there create/drop throwaway databases per test. Without it, tests
  in this tier skip rather than fail.
- CI provisions this via a `pgvector/pgvector:pg17` service container pinned
  by digest (see `ci.yml`) and sets `POSTGRES_ADMIN_DSN` to point at it — you
  do not need to match that exactly locally, any reachable PG17+pgvector
  instance with a superuser DSN works.

## Schema migrations

Never hand-pick a migration `version=`. Always run:

```bash
python scripts/new_migration.py "<slug>"
```

This mints a version number that is collision-free against `origin/main` and
every currently-open PR at creation time. A CI job
(`migration-collision-guard`) re-checks at PR-review time as a backstop —
`jobcannon/db/migrations/__init__.py`'s duplicate-version import check is the
final backstop if two PRs still race past both checks. See
`docs/deploy-runbook.md` §3 ("Creating a new migration") for the full
procedure.

## CI and contribution flow

- This repo runs CI on GitHub-hosted `ubuntu-latest` runners (public repo,
  free Actions minutes) — never add a self-hosted-runner requirement here.
- Work on a branch in this repo directly when you have push access. If you
  don't (a fork PR), your workflow run sits **pending** (not failing) until a
  maintainer approves it from the PR's Checks tab — this is expected, not an
  error, and needs no action from you beyond opening the PR normally.
- CLA: first-time contributors must sign per `CONTRIBUTING.md` before merge
  (the `CLA Assistant Lite` check gates it). Existing signers need no action.
- `ruff` line length is 100 (`pyproject.toml`'s `[tool.ruff]`), Python
  `>=3.12`.

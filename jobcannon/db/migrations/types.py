"""Migration dataclasses — Postgres flavor of the private repo's ledger pattern.

Differences from the private (SQLite) original, all deliberate:
- No PRAGMA user_version cache: the schema_migrations ledger is the SOLE
  authority (Postgres has no equivalent pragma, and the ledger was already
  authoritative in the original — the pragma was only a best-effort cache).
- No legacy-DB backfill path: this repo has no pre-ledger databases.
- Each migration applies inside ONE transaction (Postgres DDL is
  transactional): all sql statements + the optional py hook + the ledger
  INSERT commit atomically, so a mid-migration crash leaves no half-applied
  migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import psycopg


@dataclass(frozen=True)
class MigrationContext:
    conn: psycopg.Connection
    dsn: str


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    sql: list[str] = field(default_factory=list)
    py: Callable[[MigrationContext], None] | None = None
    name: str = ""
    # True when this migration deliberately ships contract-shaped DDL (a
    # DROP / narrowing / tightening that is NOT guaranteed backward-
    # compatible with the previous release during Render's zero-downtime
    # deploy overlap window -- docs/deploy-runbook.md Sec 3). PREFER a bare
    # `contract_step = True` module attribute (never a Migration(...) kwarg
    # passed directly in the migration file) so it lives next to the
    # docstring's "Contract justification:" section -- see
    # jobcannon/db/migrations/__init__.py, the single place that reads the
    # attribute via getattr() and folds it into this field. That fold is an
    # OR against whatever value was passed here directly, specifically so a
    # `Migration(..., contract_step=True)` kwarg is never silently
    # overwritten back to False by the module-attribute default (#218
    # review M1) -- the module attribute is still the documented, DX-
    # preferred way to set it. Requires a docstring "Contract justification:"
    # section (tests/test_migration_deploy_safety.py, issue #199).
    # jobcannon/db/migrate.py's _apply_migration logs a loud one-line notice
    # when applying one.
    contract_step: bool = False
    # True when this migration deliberately ships a non-CONCURRENT
    # `CREATE INDEX` against a table an EARLIER migration created -- that
    # statement takes a SHARE lock for the whole build, blocking writes to
    # the table while the previous release's web instance keeps serving
    # them during Render's zero-downtime overlap window (issue #219). PREFER
    # a bare `lock_step = True` module attribute (never a Migration(...)
    # kwarg passed directly in the migration file), same reasoning as
    # `contract_step` above -- it lives next to the docstring's "Lock
    # justification:" section naming the expected row count / build time.
    # jobcannon/db/migrations/__init__.py's `_fold_lock_step` folds either
    # source (module attribute OR this kwarg) onto this field, same OR
    # semantics as contract_step. An index on a table the SAME migration
    # creates never needs this -- the table is empty, so the build is
    # instant regardless of lock kind. Required alongside a docstring "Lock
    # justification:" section (tests/test_migration_deploy_safety.py).
    lock_step: bool = False
    # True when this migration's `sql` statements must run OUTSIDE the
    # per-migration ledger transaction, on an autocommit connection --
    # `CREATE INDEX CONCURRENTLY` (the escape hatch from the lock-duration
    # hazard `lock_step` documents an ACCEPTED risk for) refuses to run
    # inside a transaction block at all, and every migration otherwise
    # applies inside exactly one (jobcannon/db/migrate.py's
    # `_apply_migration`). PREFER a bare `autocommit = True` module
    # attribute, folded via `_fold_autocommit` exactly like `contract_step`/
    # `lock_step` above. tests/test_migration_deploy_safety.py keeps this
    # escape hatch narrow: a CONCURRENTLY statement always requires
    # `autocommit = True` and vice versa, and an autocommit migration's
    # `sql` may contain only CREATE/DROP INDEX statements (no py hook,
    # no other DDL) -- the only thing autocommit buys you is running an
    # index build outside a transaction, so there is no expand-safe reason
    # to ever combine it with anything else.
    autocommit: bool = False

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

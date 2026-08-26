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

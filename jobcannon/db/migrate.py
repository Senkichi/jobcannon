"""Postgres migrations driver — the private repo's applied-set ledger, ported.

"Applied" is SET MEMBERSHIP in schema_migrations — never a max-version
comparison. A migration merged in below the current max still runs. A ledger
row this code doesn't know (orphan) means the DB was migrated by newer code:
refuse to touch it (DatabaseNewerThanCodeError), same contract as the
private repo's downgrade guard.
"""

from __future__ import annotations

import logging

import psycopg

from jobcannon.db.migrations import MIGRATIONS
from jobcannon.db.migrations.types import Migration, MigrationContext

logger = logging.getLogger(__name__)

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    bigint PRIMARY KEY,
    name       text NOT NULL,
    checksum   text,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class DatabaseNewerThanCodeError(RuntimeError):
    pass


def applied_versions(conn: psycopg.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def _apply_migration(conn: psycopg.Connection, dsn: str, migration: Migration) -> None:
    with conn.transaction():
        for statement in migration.sql:
            conn.execute(statement)
        if migration.py is not None:
            migration.py(MigrationContext(conn=conn, dsn=dsn))
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
            (migration.version, migration.name),
        )
    logger.info("applied migration %s (%s)", migration.version, migration.name)


def run_migrations(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute(_LEDGER_DDL)

        applied = applied_versions(conn)
        known = {m.version for m in MIGRATIONS}
        orphans = applied - known
        if orphans:
            raise DatabaseNewerThanCodeError(
                f"This database has applied migration(s) this code does not know "
                f"(lowest unknown: {min(orphans)}). It was migrated by a newer "
                f"version. Upgrade the code or restore a backup. "
                f"The database has NOT been modified."
            )

        for migration in MIGRATIONS:
            if migration.version not in applied:
                _apply_migration(conn, dsn, migration)

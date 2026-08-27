"""Postgres migrations driver — the private repo's applied-set ledger, ported.

"Applied" is SET MEMBERSHIP in schema_migrations — never a max-version
comparison. A migration merged in below the current max still runs. A ledger
row this code doesn't know (orphan) means the DB was migrated by newer code:
refuse to touch it (DatabaseNewerThanCodeError), same contract as the
private repo's downgrade guard.

Runnable directly (`python -m jobcannon.db.migrate`) as a pre-deploy step —
see render.yaml's jobcannon-web `preDeployCommand` and
docs/deploy-runbook.md §3. web and worker deploy independently with no
ordering guarantee, so the pre-deploy command is what makes migrations land
before the new web code ever serves a request against the old schema; the
worker's boot-time call to run_migrations() stays as an idempotent,
lock-serialized belt-and-braces (see _ADVISORY_LOCK_KEY below) in case a
worker ever boots first.
"""

from __future__ import annotations

import logging
import os
import sys

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

# Session-level advisory lock key: serializes concurrent run_migrations()
# callers against the same database (e.g. a web pre-deploy and a worker boot
# racing each other) so they can't collide on the schema_migrations ledger
# INSERT. A fixed 64-bit constant rather than one computed at connect time —
# stable across every process and every deploy, no extra round trip. Derived
# once via:
#   SELECT hashtextextended('jobcannon.db.migrate: schema_migrations advisory lock', 0)
# and hardcoded here; never recompute this at runtime.
_ADVISORY_LOCK_KEY = 5255127982483983144


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
        # Acquire the session-level advisory lock in its OWN transaction, same
        # reasoning as the ledger DDL/read below: a bare execute() would leave
        # an implicit transaction open and downgrade every subsequent `with
        # conn.transaction():` to a SAVEPOINT. pg_advisory_lock is
        # session-scoped, not transaction-scoped, so it survives this
        # transaction's commit and stays held until pg_advisory_unlock (or
        # session end) regardless. A second run_migrations() call against the
        # same database — e.g. web's pre-deploy racing the worker's boot-time
        # call — blocks here until the first releases, so the two never
        # interleave DDL or ledger writes.
        # Logged before the (unbounded, blocking) acquire call: if a peer
        # holding this lock was SIGKILLed mid-migration, Postgres releases
        # its session-level lock immediately on backend termination, but
        # until then this call blocks with no output of its own — this line
        # is what makes that wait visible in a Render deploy log instead of
        # looking like silence.
        logger.info("waiting for schema_migrations advisory lock")
        with conn.transaction():
            conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        try:
            with conn.transaction():
                conn.execute(_LEDGER_DDL)

            # Read inside its own transaction so the connection returns to
            # IDLE afterward. A bare execute() would leave an implicit
            # transaction open (psycopg autocommit=False), and every
            # subsequent per-migration `with conn.transaction():` in
            # _apply_migration would then be downgraded to a SAVEPOINT of
            # that one lingering transaction — committing (or rolling back)
            # the whole run atomically at connection exit instead of one
            # migration at a time.
            with conn.transaction():
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

            pending = [m for m in MIGRATIONS if m.version not in applied]
            logger.info(
                "schema_migrations: %d applied, %d pending%s",
                len(applied),
                len(pending),
                f" ({', '.join(m.name for m in pending)})" if pending else "",
            )
            for migration in pending:
                _apply_migration(conn, dsn, migration)
        finally:
            # Best-effort explicit release rather than relying solely on
            # session-end (connection close already releases every
            # session-level advisory lock the backend holds, but an explicit
            # unlock frees it immediately instead of only when this
            # connection object is torn down).
            with conn.transaction():
                conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))


def main() -> int:
    """Entry point for `python -m jobcannon.db.migrate` — the render.yaml
    jobcannon-web `preDeployCommand` (docs/deploy-runbook.md §3). Resolves
    the DSN through the exact same path the worker uses (load_host_config)
    rather than re-parsing DATABASE_URL here. Exits non-zero on any failure
    so a failed pre-deploy aborts the Render release instead of promoting
    web code to a schema it doesn't match."""
    logging.basicConfig(level=os.environ.get("JC_LOG_LEVEL", "INFO"))
    # Deferred import: jobcannon.host pulls in the full engine-seam wiring
    # stack, which run_migrations() itself has no need of — keep that
    # dependency scoped to the CLI entry point, not the importable driver.
    from jobcannon.host import load_host_config

    try:
        host_config = load_host_config()
        run_migrations(host_config.database_url)
    except Exception:
        logger.exception("pre-deploy migration run failed")
        return 1
    logger.info("pre-deploy migration run: schema_migrations up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

# Escape hatch for a rollback across a migration boundary (issue #196 H1):
# the orphan guard below is fail-CLOSED by default on BOTH the pre-deploy
# CLI path (main()) and the worker boot path (jobcannon/worker/__main__.py)
# — a rolled-back release whose code doesn't know a ledger row it finds
# raises DatabaseNewerThanCodeError and aborts, same as today. Setting this
# var is the one sanctioned way to override that on a specific deploy (see
# docs/deploy-runbook.md's Rollback caveat for when this is actually safe:
# only across expand-only migrations, never a contract-shaped one). Parsed
# in exactly ONE place (allow_newer_db_from_env below) so the CLI and the
# worker can never diverge on what counts as "truthy".
_ALLOW_NEWER_DB_ENV = "JC_MIGRATE_ALLOW_NEWER_DB"


def allow_newer_db_from_env() -> bool:
    """Single source of truth for parsing JC_MIGRATE_ALLOW_NEWER_DB.
    Both jobcannon.db.migrate.main() (pre-deploy) and
    jobcannon.worker.__main__.main() (worker boot) call this — never read
    the env var directly — so the two paths can't drift on what "truthy"
    means."""
    return os.environ.get(_ALLOW_NEWER_DB_ENV, "").strip().lower() in ("1", "true")


class DatabaseNewerThanCodeError(RuntimeError):
    pass


def applied_versions(conn: psycopg.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def _drop_invalid_indexes(conn: psycopg.Connection) -> None:
    """`CREATE INDEX CONCURRENTLY` is the one DDL statement Postgres does NOT
    roll back atomically on failure: a build that dies partway (deploy
    timeout, connection drop, a duplicate value tripping a UNIQUE build)
    leaves a real catalog entry behind, flagged `pg_index.indisvalid =
    false` -- unusable, and `CREATE INDEX ... IF NOT EXISTS` on retry sees
    the name already taken and silently no-ops instead of finishing the
    build (issue #219's retry hazard). A plain, non-CONCURRENT `CREATE
    INDEX` is fully transactional and can NEVER leave one of these, so this
    sweep only ever runs from _apply_autocommit_migration -- a database with
    no autocommit-flagged migration pending never issues this query at all.

    Deliberately UNSCOPED to any particular index name (drops every invalid
    index in the database, not just ones this migration's statements name).
    Two things make that safe rather than reckless: an invalid index is
    already unusable for anything (the planner refuses to plan against one,
    so dropping it destroys no live behavior, only unusable catalog
    wreckage), and the ledger guarantees whichever migration could have left
    one behind was NOT recorded as applied -- so it is always retried before
    any migration after it, meaning any invalid index this sweep finds
    belongs either to the autocommit migration about to (re-)run or to
    manual out-of-band intervention, never to a completed, ledgered step.
    An alternative considered and rejected: parse each migration's own SQL
    (via pglast, already a dev dependency for
    tests/test_migration_deploy_safety.py) to scope the sweep to only the
    index names this migration's statements declare. Rejected because it
    would make `python -m jobcannon.db.migrate` -- the render.yaml
    preDeployCommand that gates every deploy -- import pglast at runtime for
    the first time; a wheel/resolution hiccup on Render would then fail
    EVERY deploy at import, not just the rare failed-CONCURRENTLY-build
    case this exists to recover from. That trade was upside down, so the
    static parser stays dev-only and this sweep stays a blunt, dependency-
    free SQL query."""
    rows = conn.execute(
        "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE NOT i.indisvalid"
    ).fetchall()
    for (index_name,) in rows:
        logger.warning(
            "dropping INVALID index %r -- leftover from a previously failed "
            "CREATE INDEX CONCURRENTLY build -- before retrying",
            index_name,
        )
        conn.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{index_name}"')


def _apply_autocommit_migration(conn: psycopg.Connection, migration: Migration) -> None:
    """autocommit=True migrations run their `sql` statements OUTSIDE the
    normal per-migration transaction, on the SAME connection/session
    `run_migrations` already holds the advisory lock on -- toggling
    `conn.autocommit` at runtime rather than opening a second connection, so
    the lock (session-scoped) covers this stretch by construction with no
    extra plumbing. `CREATE INDEX CONCURRENTLY` refuses to run inside a
    transaction block at all, which is the whole reason this path exists
    (issue #219). tests/test_migration_deploy_safety.py enforces the escape
    hatch stays narrow: only a CONCURRENTLY statement may set autocommit,
    only an autocommit migration may use CONCURRENTLY, and an autocommit
    migration's `sql` may contain nothing but CREATE/DROP INDEX statements
    -- so a `py` hook here would silently never run; fail loudly instead of
    letting that possibility exist quietly.

    The ledger INSERT happens in its own ordinary transaction AFTER every
    statement succeeds -- never inside the autocommit stretch itself (an
    INSERT there would commit statement-by-statement with no atomicity, and
    a mid-migration crash could ledger a partially-applied migration). A
    crash between the last statement and the ledger INSERT instead leaves
    the migration looking un-applied, which is exactly what
    _drop_invalid_indexes + `IF NOT EXISTS` make a safe, idempotent retry."""
    if migration.py is not None:
        raise ValueError(
            f"migration {migration.version} ({migration.name}) sets both "
            f"autocommit=True and a py hook -- the hook would never run; "
            f"see _apply_autocommit_migration's docstring"
        )
    conn.autocommit = True
    try:
        _drop_invalid_indexes(conn)
        for statement in migration.sql:
            conn.execute(statement)
    finally:
        # Swallow a failure here for the same reason run_migrations' own
        # advisory-unlock finally does: if the loop above just failed
        # because the connection itself died, resetting autocommit fails
        # too, and letting THAT replace the real migration exception would
        # hide the failure an operator actually needs to see. Whether this
        # reset lands or not, the connection is discarded (closed, never
        # reused) at run_migrations()'s own `with psycopg.connect(...)`
        # exit, so a stuck autocommit=True never leaks into another
        # migration's transaction.
        try:
            conn.autocommit = False
        except Exception:
            logger.warning(
                "resetting autocommit off failed after applying migration "
                "%s (%s); the connection is discarded at run_migrations() "
                "exit either way",
                migration.version,
                migration.name,
                exc_info=True,
            )
    with conn.transaction():
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
            (migration.version, migration.name),
        )


def _apply_migration(conn: psycopg.Connection, dsn: str, migration: Migration) -> None:
    if migration.contract_step:
        # Loud on purpose (WARNING, not INFO): this is the log line
        # docs/deploy-runbook.md's "Migration deploy-safety guard" section
        # points an operator at before deciding whether
        # JC_MIGRATE_ALLOW_NEWER_DB is safe to use for a rollback across
        # this migration -- it is NOT, for a contract-shaped one (see the
        # Rollback caveat above that line).
        logger.warning(
            "CONTRACT-STEP migration %s (%s) applying -- not guaranteed "
            "backward-compatible with the previous release during the "
            "zero-downtime deploy overlap window; see the migration's own "
            "'Contract justification:' docstring and "
            "docs/deploy-runbook.md's Rollback caveat",
            migration.version,
            migration.name,
        )
    if migration.lock_step:
        # Same loudness rationale as CONTRACT-STEP above -- this is the log
        # line docs/deploy-runbook.md's guard section points an operator at:
        # this migration accepted a lock-duration risk instead of using
        # CONCURRENTLY (issue #219), see its own 'Lock justification:'
        # docstring section for why that was judged safe at the time it was
        # written (e.g. an empty table).
        logger.warning(
            "LOCK-STEP migration %s (%s) applying -- ships a non-CONCURRENT "
            "index build against a pre-existing table instead of accepting "
            "autocommit=True; see the migration's own 'Lock justification:' "
            "docstring",
            migration.version,
            migration.name,
        )
    if migration.autocommit:
        _apply_autocommit_migration(conn, migration)
        logger.info("applied migration %s (%s)", migration.version, migration.name)
        return
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


def run_migrations(dsn: str, *, allow_newer_db: bool = False) -> None:
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
                if not allow_newer_db:
                    raise DatabaseNewerThanCodeError(
                        f"This database has applied migration(s) this code does not know "
                        f"(lowest unknown: {min(orphans)}). It was migrated by a newer "
                        f"version. Upgrade the code or restore a backup. "
                        f"The database has NOT been modified."
                    )
                logger.warning(
                    "database is newer than this code: versions %s; %s set, continuing",
                    sorted(orphans),
                    _ALLOW_NEWER_DB_ENV,
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
            # connection object is torn down). Swallow any failure HERE:
            # if a migration just failed because the connection died, this
            # statement fails too, and letting it raise would replace the
            # real migration exception in main()'s logger.exception output
            # with an unrelated "connection already closed" traceback — the
            # thing an operator actually needs to see is the migration
            # failure, not this best-effort cleanup's own error. The
            # connection-close backstop above still releases the lock
            # either way.
            try:
                with conn.transaction():
                    conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
            except Exception:
                logger.warning(
                    "advisory unlock failed; the lock is released when the connection closes",
                    exc_info=True,
                )


def main() -> int:
    """Entry point for `python -m jobcannon.db.migrate` — the render.yaml
    jobcannon-web `preDeployCommand` (docs/deploy-runbook.md §3). Resolves
    the DSN through the exact same path the worker uses (load_host_config)
    rather than re-parsing DATABASE_URL here. Exits non-zero on any failure
    so a failed pre-deploy aborts the Render release instead of promoting
    web code to a schema it doesn't match."""
    logging.basicConfig(level=os.environ.get("JC_LOG_LEVEL", "INFO"))
    try:
        # Deferred + leaf import: jobcannon.host.config only, not the
        # jobcannon.host package (which also pulls in the full engine-seam
        # wiring stack — build_scan_services/init_engine_seams — that this
        # pre-deploy step has no need of). Deliberately INSIDE the try: an
        # import-time failure anywhere in that graph must still produce the
        # same "pre-deploy migration run failed" log line as any other
        # pre-deploy failure (docs/deploy-runbook.md points operators at
        # that line), not an uncaught traceback with no diagnostic line.
        from jobcannon.host.config import load_host_config

        host_config = load_host_config()
        run_migrations(host_config.database_url, allow_newer_db=allow_newer_db_from_env())
    except Exception:
        logger.exception("pre-deploy migration run failed")
        return 1
    logger.info("pre-deploy migration run: schema_migrations up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())

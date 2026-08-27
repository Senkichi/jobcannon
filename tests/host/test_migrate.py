import threading
import time

import psycopg
import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def test_run_migrations_is_idempotent_and_ledgered():
    from jobcannon.db.migrate import run_migrations

    dsn, db_name = create_throwaway_db("jobcannon_mig_idem")
    try:
        run_migrations(dsn)
        run_migrations(dsn)  # second run must be a no-op, not an error
        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
        assert rows[0][0] == 1
        assert "initial_schema" in rows[0][1]
    finally:
        drop_throwaway_db(db_name)


def test_unknown_applied_version_raises_newer_than_code():
    from jobcannon.db.migrate import DatabaseNewerThanCodeError, run_migrations

    dsn, db_name = create_throwaway_db("jobcannon_mig_orphan")
    try:
        run_migrations(dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (999999999, 'm999999999_from_the_future', now())"
            )
            conn.commit()
        with pytest.raises(DatabaseNewerThanCodeError):
            run_migrations(dsn)
    finally:
        drop_throwaway_db(db_name)


def test_migration_failure_does_not_roll_back_earlier_committed_migrations(monkeypatch):
    """Regression for the bare-SELECT transaction-status bug (F2): reading
    applied_versions() via a bare execute() left the connection mid-transaction
    (psycopg autocommit=False implicitly opens one), so every migration's own
    `with conn.transaction():` became a SAVEPOINT of that ONE lingering
    transaction instead of a real, independently-committing transaction. The
    whole run then only committed (or rolled back) atomically at connection
    exit — so a later migration's failure silently undid earlier migrations
    that had already "applied". Verify migration 1 truly commits on its own
    even though migration 2 fails.
    """
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations.types import Migration, MigrationContext

    def _boom(ctx: MigrationContext) -> None:
        raise RuntimeError("boom")

    fake_migrations = [
        Migration(
            version=900001,
            description="ok",
            sql=["CREATE TABLE t_ok (id int)"],
            name="m900001_ok",
        ),
        Migration(version=900002, description="fails", py=_boom, name="m900002_fails"),
    ]
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", fake_migrations)

    dsn, db_name = create_throwaway_db("jobcannon_mig_partial")
    try:
        with pytest.raises(RuntimeError, match="boom"):
            migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            applied = {
                r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            assert 900001 in applied
            assert 900002 not in applied
            exists = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 't_ok'"
            ).fetchone()
            assert exists is not None
    finally:
        drop_throwaway_db(db_name)


def test_run_migrations_serializes_concurrent_callers_via_advisory_lock(monkeypatch):
    """Regression for the missing concurrency guard: two migrators racing the
    same database (e.g. jobcannon-web's preDeployCommand and
    jobcannon-worker's boot-time call, or two overlapping deploys) must never
    interleave the ledger DDL/read/insert. Without the advisory lock this is
    a real, reproducible race: both threads can read applied_versions() as
    empty while the slow migration is still mid-flight, both attempt
    _apply_migration(), and the loser's INSERT INTO schema_migrations hits
    the version PRIMARY KEY and raises -- run_migrations() propagates that,
    so the test below (no exception from either thread) would fail without
    the lock.

    Patches MIGRATIONS with one migration whose py hook sleeps ~1s, fires
    two real threads with independent connections at run_migrations() via a
    Barrier so they genuinely race rather than merely starting close
    together, then proves both the race-safety (no exception, exactly one
    ledger row, hook ran exactly once) and true serialization rather than
    lucky scheduling: the LOSER cannot return from run_migrations() until
    AFTER the winner's slow hook finishes, because it is blocked on
    pg_advisory_lock for that whole window.
    """
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations.types import Migration, MigrationContext

    hook_calls: list[tuple[float, float]] = []

    def _slow(ctx: MigrationContext) -> None:
        start = time.monotonic()
        time.sleep(1.0)
        hook_calls.append((start, time.monotonic()))

    fake_migrations = [
        Migration(version=900101, description="slow", py=_slow, name="m900101_slow"),
    ]
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", fake_migrations)

    dsn, db_name = create_throwaway_db("jobcannon_mig_concurrent")
    barrier = threading.Barrier(2)
    finish_times: dict[str, float] = {}
    errors: dict[str, BaseException] = {}

    def _worker(name: str) -> None:
        barrier.wait()
        try:
            migrate_mod.run_migrations(dsn)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors[name] = exc
        finish_times[name] = time.monotonic()

    try:
        threads = [threading.Thread(target=_worker, args=(name,)) for name in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not any(t.is_alive() for t in threads), (
            "a thread never returned from run_migrations() -- likely a lock "
            "never released (missing pg_advisory_unlock)"
        )

        assert not errors, f"run_migrations raised under concurrency: {errors}"
        assert len(finish_times) == 2

        assert len(hook_calls) == 1, (
            f"migration py hook ran {len(hook_calls)} times under concurrency, expected "
            "exactly 1 -- the ledger read/apply is not serialized"
        )
        _, hook_end = hook_calls[0]

        # Both threads -- winner and loser alike -- can only have returned
        # AFTER the lock holder released, which happens after the hook
        # finished. If either finished before hook_end, it proceeded past
        # the ledger check while the migration was still mid-flight.
        assert min(finish_times.values()) >= hook_end, (
            f"a thread returned from run_migrations() before the slow "
            f"migration finished (finish_times={finish_times}, hook_end={hook_end}) "
            "-- the advisory lock did not serialize the two callers"
        )

        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                "SELECT version FROM schema_migrations WHERE version = %s", (900101,)
            ).fetchall()
        assert len(rows) == 1
    finally:
        drop_throwaway_db(db_name)

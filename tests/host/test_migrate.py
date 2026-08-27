import threading
import time

import psycopg
import pytest

from tests.host.conftest import ADMIN_DSN, create_throwaway_db, drop_throwaway_db, requires_postgres

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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("", False),
        ("  1  ", True),
    ],
)
def test_allow_newer_db_from_env_parses_truthy_values(monkeypatch, value, expected):
    """Single-parse-site guard for the JOBCANNON_MIGRATE_ALLOW_NEWER_DB escape
    hatch (issue #196 H1) -- both the pre-deploy CLI and the worker boot path
    call this same helper, so its parsing rules are exactly what both paths
    honor."""
    from jobcannon.db.migrate import allow_newer_db_from_env

    monkeypatch.setenv("JOBCANNON_MIGRATE_ALLOW_NEWER_DB", value)
    assert allow_newer_db_from_env() is expected


def test_allow_newer_db_from_env_defaults_false_when_unset(monkeypatch):
    from jobcannon.db.migrate import allow_newer_db_from_env

    monkeypatch.delenv("JOBCANNON_MIGRATE_ALLOW_NEWER_DB", raising=False)
    assert allow_newer_db_from_env() is False


def test_run_migrations_with_allow_newer_db_continues_past_orphan(caplog):
    """In-process counterpart to the subprocess override test below: proves
    run_migrations(..., allow_newer_db=True) does not raise on an orphan
    ledger version, logs it at WARNING instead, and still completes (default
    behavior stays fail-closed -- test_unknown_applied_version_raises_newer_than_code
    above covers that)."""
    import logging

    from jobcannon.db.migrate import run_migrations

    dsn, db_name = create_throwaway_db("jobcannon_mig_orphan_allow")
    try:
        run_migrations(dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (999999999, 'm999999999_from_the_future', now())"
            )
            conn.commit()

        with caplog.at_level(logging.WARNING, logger="jobcannon.db.migrate"):
            run_migrations(dsn, allow_newer_db=True)  # must not raise

        assert "database is newer than this code" in caplog.text
        assert "999999999" in caplog.text
    finally:
        drop_throwaway_db(db_name)


def test_migrate_subprocess_exits_nonzero_on_orphan_without_override():
    """CLI-subprocess coverage for the orphan guard (review-1 LOW L-b): an
    unknown ledger version must fail the actual `python -m jobcannon.db.migrate`
    subprocess Render invokes, not just the in-process run_migrations() call
    covered by test_unknown_applied_version_raises_newer_than_code above."""
    import os
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    dsn, db_name = create_throwaway_db("jobcannon_mig_orphan_subproc")
    try:
        from jobcannon.db.migrate import run_migrations

        run_migrations(dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (999999999, 'm999999999_from_the_future', now())"
            )
            conn.commit()

        env = dict(os.environ)
        env["DATABASE_URL"] = dsn
        env.pop("JOBCANNON_MIGRATE_ALLOW_NEWER_DB", None)

        result = subprocess.run(
            [sys.executable, "-m", "jobcannon.db.migrate"],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "DatabaseNewerThanCodeError" in result.stderr, result.stderr
    finally:
        drop_throwaway_db(db_name)


def test_migrate_subprocess_orphan_with_override_exits_zero_and_warns():
    """The escape hatch, exercised the way Render would actually set it: a
    subprocess env var, not an in-process monkeypatch of allow_newer_db."""
    import os
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    dsn, db_name = create_throwaway_db("jobcannon_mig_orphan_override")
    try:
        from jobcannon.db.migrate import run_migrations

        run_migrations(dsn)
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) "
                "VALUES (999999999, 'm999999999_from_the_future', now())"
            )
            conn.commit()

        env = dict(os.environ)
        env["DATABASE_URL"] = dsn
        env["JOBCANNON_MIGRATE_ALLOW_NEWER_DB"] = "1"

        result = subprocess.run(
            [sys.executable, "-m", "jobcannon.db.migrate"],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "database is newer than this code" in result.stderr, result.stderr
        assert "999999999" in result.stderr, result.stderr
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
    a real, reproducible race -- verified by sabotage (temporarily removing
    the lock acquisition and rerunning this test 3x, all 3 failed). The
    observed failure mode was two threads racing `CREATE TABLE IF NOT
    EXISTS schema_migrations` itself: both see the table absent and both
    attempt to create it, and the loser's implicit catalog row insert hits
    a UniqueViolation on pg_type's own namespace/name index -- an even
    earlier collision than the ledger row PRIMARY KEY it also guards against
    (two callers reading applied_versions() as empty while a slow migration
    is still mid-flight would otherwise collide there next). Either
    collision propagates out of run_migrations(), so the test below (no
    exception from either thread) would fail without the lock.

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


def test_advisory_lock_key_matches_documented_derivation():
    """Guards the hardcoded `_ADVISORY_LOCK_KEY` comment against a silent
    transcription typo (cross-family review LEAD 4): re-derives the constant
    from the exact string the module comment claims it comes from, against
    live Postgres, rather than trusting the hardcoded value on faith. Any
    connection works here -- hashtextextended is a pure function of its
    arguments, not tied to a particular database -- so this uses the admin
    DSN directly instead of a throwaway db."""
    import jobcannon.db.migrate as migrate_mod

    with psycopg.connect(ADMIN_DSN) as conn:
        row = conn.execute(
            "SELECT hashtextextended(%s, 0)",
            ("jobcannon.db.migrate: schema_migrations advisory lock",),
        ).fetchone()
    assert row[0] == migrate_mod._ADVISORY_LOCK_KEY


def test_unlock_failure_in_finally_does_not_mask_migration_exception(monkeypatch):
    """Regression for the finally-block masking hazard (review-1 L-a /
    review-3 finding 2 / cross-family review LEAD 1, corroborated 3x): if a
    migration fails AND the connection is then dead, the finally block's own
    `pg_advisory_unlock` statement also raising must NOT replace the
    ORIGINAL migration exception -- that would put an unrelated "connection
    already closed" traceback in main()'s logger.exception output instead of
    the real failure an operator needs to see. Simulates a dead-connection
    unlock failure directly (raising whenever the unlock statement text is
    executed) while a real migration hook fails first, and asserts the
    migration's own exception is what propagates."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations.types import Migration, MigrationContext

    def _boom(ctx: MigrationContext) -> None:
        raise RuntimeError("boom from the migration itself")

    fake_migrations = [
        Migration(version=900301, description="fails", py=_boom, name="m900301_fails"),
    ]
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", fake_migrations)

    original_execute = psycopg.Connection.execute

    def _flaky_execute(self, query, *args, **kwargs):
        if isinstance(query, str) and "pg_advisory_unlock" in query:
            raise RuntimeError("simulated: connection already closed")
        return original_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", _flaky_execute)

    dsn, db_name = create_throwaway_db("jobcannon_mig_unlock_fail")
    try:
        with pytest.raises(RuntimeError, match="boom from the migration itself"):
            migrate_mod.run_migrations(dsn)
    finally:
        monkeypatch.undo()  # restore real execute() before drop_throwaway_db needs it
        drop_throwaway_db(db_name)

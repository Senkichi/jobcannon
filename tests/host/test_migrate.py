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
    """Single-parse-site guard for the JC_MIGRATE_ALLOW_NEWER_DB escape
    hatch (issue #196 H1) -- both the pre-deploy CLI and the worker boot path
    call this same helper, so its parsing rules are exactly what both paths
    honor."""
    from jobcannon.db.migrate import allow_newer_db_from_env

    monkeypatch.setenv("JC_MIGRATE_ALLOW_NEWER_DB", value)
    assert allow_newer_db_from_env() is expected


def test_allow_newer_db_from_env_defaults_false_when_unset(monkeypatch):
    from jobcannon.db.migrate import allow_newer_db_from_env

    monkeypatch.delenv("JC_MIGRATE_ALLOW_NEWER_DB", raising=False)
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
        env.pop("JC_MIGRATE_ALLOW_NEWER_DB", None)

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
        env["JC_MIGRATE_ALLOW_NEWER_DB"] = "1"

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


def test_contract_step_migration_logs_a_loud_notice_when_applied(monkeypatch, caplog):
    """Issue #199: a contract_step=True migration must log a loud (WARNING)
    one-line notice naming its version + name when it actually applies --
    docs/deploy-runbook.md's "Migration deploy-safety guard" subsection
    points an operator at this exact line before trusting
    JC_MIGRATE_ALLOW_NEWER_DB for a rollback across it. A non-contract
    migration must NOT log it (regression guard against the notice firing
    unconditionally)."""
    import logging

    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations.types import Migration

    fake_migrations = [
        Migration(
            version=900401,
            description="plain",
            sql=["CREATE TABLE t_plain (id int)"],
            name="m900401_plain",
        ),
        Migration(
            version=900402,
            description="contract",
            sql=["CREATE TABLE t_contract (id int)"],
            name="m900402_contract",
            contract_step=True,
        ),
    ]
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", fake_migrations)

    dsn, db_name = create_throwaway_db("jobcannon_mig_contract_step")
    try:
        with caplog.at_level(logging.WARNING, logger="jobcannon.db.migrate"):
            migrate_mod.run_migrations(dsn)

        assert "CONTRACT-STEP migration 900402" in caplog.text
        assert "m900402_contract" in caplog.text
        assert "CONTRACT-STEP migration 900401" not in caplog.text
    finally:
        drop_throwaway_db(db_name)


def test_autocommit_migration_applies_and_ledgers_after_success(monkeypatch):
    """Issue #219: an autocommit=True migration's CONCURRENTLY statement
    must actually build a valid index outside the ledger transaction, and
    the ledger row must land only AFTER it succeeds."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations.types import Migration

    fake_migrations = [
        Migration(
            version=900501,
            description="table for the index",
            sql=["CREATE TABLE t_ac_ok (id bigserial PRIMARY KEY, val int)"],
            name="m900501_ac_table",
        ),
        Migration(
            version=900502,
            description="concurrent index build",
            sql=["CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_t_ac_ok_val ON t_ac_ok(val)"],
            name="m900502_ac_index",
            autocommit=True,
        ),
    ]
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", fake_migrations)

    dsn, db_name = create_throwaway_db("jobcannon_mig_autocommit_ok")
    try:
        migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            versions = {
                r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            assert {900501, 900502} <= versions

            valid = conn.execute(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'idx_t_ac_ok_val'"
            ).fetchone()
            assert valid is not None, "the CONCURRENTLY index was never built"
            assert valid[0] is True, "the index built but was left INVALID"
    finally:
        drop_throwaway_db(db_name)


def test_autocommit_migration_failure_leaves_no_ledger_row_and_retries_cleanly(monkeypatch):
    """Issue #219's retry hazard: a CREATE UNIQUE INDEX CONCURRENTLY build
    that fails partway (here: real duplicate data tripping the uniqueness
    check, the actual failure mode Postgres produces) leaves an INVALID
    catalog entry behind instead of rolling back. Proves the full recovery
    story: (1) the failed run raises, no ledger row for that version, and
    the index exists but indisvalid=false; (2) after the underlying data
    problem is fixed, a retry's _drop_invalid_indexes sweep drops that
    leftover BEFORE re-running the statement, so the retry succeeds cleanly
    and the ledger row lands with indisvalid=true."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations.types import Migration

    fake_migrations = [
        Migration(
            version=900511,
            description="table with a duplicate value",
            sql=[
                "CREATE TABLE t_ac_retry (id bigserial PRIMARY KEY, val int)",
                "INSERT INTO t_ac_retry (val) VALUES (1), (1)",
            ],
            name="m900511_ac_dup_table",
        ),
        Migration(
            version=900512,
            description="unique concurrent index build over duplicate data",
            sql=[
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_t_ac_retry_val "
                "ON t_ac_retry(val)"
            ],
            name="m900512_ac_unique_index",
            autocommit=True,
        ),
    ]
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", fake_migrations)

    dsn, db_name = create_throwaway_db("jobcannon_mig_autocommit_retry")
    try:
        with pytest.raises(psycopg.errors.UniqueViolation):
            migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            versions = {
                r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            assert 900511 in versions
            assert 900512 not in versions, (
                "the failed autocommit migration was ledgered anyway -- it must not be"
            )

            valid = conn.execute(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'idx_t_ac_retry_val'"
            ).fetchone()
            assert valid is not None, "the failed CONCURRENTLY build left no catalog entry at all"
            assert valid[0] is False, "the leftover index should be INVALID after the failure"

        # Fix the underlying data problem, then retry.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM t_ac_retry WHERE ctid = (SELECT ctid FROM t_ac_retry LIMIT 1)"
            )

        migrate_mod.run_migrations(dsn)  # must not raise this time

        with psycopg.connect(dsn) as conn:
            versions = {
                r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            assert 900512 in versions, "retry did not ledger the migration after recovering"

            valid = conn.execute(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'idx_t_ac_retry_val'"
            ).fetchone()
            assert valid is not None
            assert valid[0] is True, "the rebuilt index should be valid after the retry"
    finally:
        drop_throwaway_db(db_name)


def test_autocommit_migration_reuses_the_same_locked_connection(monkeypatch):
    """The advisory lock run_migrations() holds is session-scoped
    (pg_advisory_lock) -- it only covers an autocommit migration's
    statements if they run on the SAME connection/session the lock was
    acquired on, not a second one. Proves _apply_autocommit_migration never
    opens a second connection: patches psycopg.connect to record every call
    made against this test's own throwaway DSN and asserts there's exactly
    one, across a migrations list that mixes an ordinary and an autocommit
    migration."""
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations.types import Migration

    fake_migrations = [
        Migration(
            version=900521,
            description="ordinary",
            sql=["CREATE TABLE t_ac_conn (id int)"],
            name="m900521_plain",
        ),
        Migration(
            version=900522,
            description="autocommit",
            sql=["CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_t_ac_conn_id ON t_ac_conn(id)"],
            name="m900522_autocommit",
            autocommit=True,
        ),
    ]
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", fake_migrations)

    dsn, db_name = create_throwaway_db("jobcannon_mig_autocommit_conn")
    try:
        connect_calls = []
        original_connect = psycopg.connect

        def _counting_connect(conninfo, *args, **kwargs):
            if conninfo == dsn:
                connect_calls.append(conninfo)
            return original_connect(conninfo, *args, **kwargs)

        monkeypatch.setattr(psycopg, "connect", _counting_connect)

        migrate_mod.run_migrations(dsn)

        assert len(connect_calls) == 1, (
            f"run_migrations opened {len(connect_calls)} connection(s) against the "
            f"target DB -- an autocommit migration must run on the SAME session the "
            f"advisory lock is held on, not a second connection"
        )

        monkeypatch.undo()  # restore real psycopg.connect before the finally's cleanup call
        with psycopg.connect(dsn) as conn:
            versions = {
                r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
        assert {900521, 900522} <= versions
    finally:
        drop_throwaway_db(db_name)


def test_lock_step_migration_logs_a_loud_notice_when_applied(monkeypatch, caplog):
    """Issue #219 counterpart to
    test_contract_step_migration_logs_a_loud_notice_when_applied above: a
    lock_step=True migration must log a loud (WARNING) one-line notice
    naming its version + name when it actually applies. A non-lock-step
    migration must NOT log it."""
    import logging

    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations.types import Migration

    fake_migrations = [
        Migration(
            version=900531,
            description="plain",
            sql=["CREATE TABLE t_lock_plain (id int)"],
            name="m900531_plain",
        ),
        Migration(
            version=900532,
            description="lock step",
            sql=["CREATE TABLE t_lock_step (id int)", "CREATE INDEX ON t_lock_step(id)"],
            name="m900532_lock_step",
            lock_step=True,
        ),
    ]
    monkeypatch.setattr(migrate_mod, "MIGRATIONS", fake_migrations)

    dsn, db_name = create_throwaway_db("jobcannon_mig_lock_step")
    try:
        with caplog.at_level(logging.WARNING, logger="jobcannon.db.migrate"):
            migrate_mod.run_migrations(dsn)

        assert "LOCK-STEP migration 900532" in caplog.text
        assert "m900532_lock_step" in caplog.text
        assert "LOCK-STEP migration 900531" not in caplog.text
    finally:
        drop_throwaway_db(db_name)


def test_autocommit_migration_with_py_hook_raises_before_touching_the_connection():
    """Defense-in-depth beyond the static scanner (Rule 4): if autocommit=True
    and py are both set (the scanner should have already rejected this at CI
    time, but _apply_autocommit_migration must never silently skip the hook
    if that guard is ever bypassed), it must fail loudly rather than quietly
    never calling the hook."""
    from jobcannon.db.migrate import _apply_autocommit_migration
    from jobcannon.db.migrations.types import Migration

    migration = Migration(
        version=900541,
        description="autocommit with a stray py hook",
        sql=["CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON t(x)"],
        py=lambda ctx: None,
        name="m900541_autocommit_py",
        autocommit=True,
    )
    with pytest.raises(ValueError, match="py hook"):
        _apply_autocommit_migration(conn=None, migration=migration)


def test_main_import_time_failure_still_logs_the_failure_line(monkeypatch, caplog):
    """Regression for cross-family review LEAD 3: main()'s
    `from jobcannon.host.config import load_host_config` now sits INSIDE the
    try block (not before it), so a failure resolving load_host_config --
    an import-time error in that leaf module, or the config loader itself
    raising, as simulated here -- still produces the
    "pre-deploy migration run failed" log line docs/deploy-runbook.md points
    operators toward, and main() still returns 1, instead of an uncaught
    traceback with no diagnostic line. Patches
    jobcannon.host.config.load_host_config itself (the leaf module main()
    imports from) rather than mocking main()'s whole body, so this exercises
    the real import + call path, not a stand-in for it."""
    import logging

    import jobcannon.db.migrate as migrate_mod
    import jobcannon.host.config as host_config_mod

    def _boom():
        raise ImportError("simulated: config resolution failed")

    monkeypatch.setattr(host_config_mod, "load_host_config", _boom)

    with caplog.at_level(logging.ERROR, logger="jobcannon.db.migrate"):
        assert migrate_mod.main() == 1

    assert "pre-deploy migration run failed" in caplog.text

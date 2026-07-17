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

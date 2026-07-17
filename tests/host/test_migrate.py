import psycopg
import pytest

from tests.host.conftest import ADMIN_DSN, requires_postgres

pytestmark = requires_postgres


def _fresh_db(name_suffix: str) -> tuple[str, str]:
    import uuid

    db_name = f"jobcannon_mig_{name_suffix}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{db_name}"')
    base, _, _ = ADMIN_DSN.rpartition("/")
    return f"{base}/{db_name}", db_name


def _drop_db(db_name: str) -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (db_name,),
        )
        admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')


def test_run_migrations_is_idempotent_and_ledgered():
    from jobcannon.db.migrate import run_migrations

    dsn, db_name = _fresh_db("idem")
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
        _drop_db(db_name)


def test_unknown_applied_version_raises_newer_than_code():
    from jobcannon.db.migrate import DatabaseNewerThanCodeError, run_migrations

    dsn, db_name = _fresh_db("orphan")
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
        _drop_db(db_name)

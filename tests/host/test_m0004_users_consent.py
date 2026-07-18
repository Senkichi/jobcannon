import psycopg
import pytest

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres

_EXPECTED = {
    "analytics_consent": "boolean",
    "analytics_consent_updated_at": "timestamp with time zone",
}


def test_users_consent_columns_exist(db_conn):
    rows = db_conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'users'"
    ).fetchall()
    actual = {r["column_name"]: r["data_type"] for r in rows}
    for col, dtype in _EXPECTED.items():
        assert actual.get(col) == dtype, f"{col}: expected {dtype}, got {actual.get(col)}"


def test_analytics_consent_not_nullable(db_conn):
    with pytest.raises(psycopg.errors.NotNullViolation):
        db_conn.execute(
            "INSERT INTO users (id, email, analytics_consent) VALUES ('nn_user', 'a@example.org', NULL)"
        )


def test_analytics_consent_defaults_to_false(db_conn):
    db_conn.execute(
        "INSERT INTO users (id, email) VALUES ('default_consent_user', 'a@example.org')"
    )
    row = db_conn.execute(
        "SELECT analytics_consent FROM users WHERE id = 'default_consent_user'"
    ).fetchone()
    assert row["analytics_consent"] is False

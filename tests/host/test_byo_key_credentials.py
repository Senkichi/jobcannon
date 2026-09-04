"""jobcannon.db._byo_key_credentials -- single reader/writer for
byo_key_credentials (L-0036 PR-1), plus the RLS policy m0020 adds.

Not a port: no private-repo equivalent exists (BYO-key hosted credentials
are new). Exercises both the CRUD surface and the tenant-isolation RLS
policy itself, since a bug in either the module or the migration produces
the same visible symptom (a query returns rows it should not, or none).
"""

from __future__ import annotations

import uuid

from jobcannon.db._byo_key_credentials import (
    deactivate_credential,
    get_active_providers,
    get_credential,
    touch_last_used,
    upsert_credential,
)

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))


def test_upsert_then_get_round_trips(db_conn):
    _seed_user(db_conn, "byo-u1")

    upsert_credential(db_conn, "byo-u1", "gemini", b"ciphertext-1")

    row = get_credential(db_conn, "byo-u1", "gemini")
    assert row is not None
    assert row["provider"] == "gemini"
    assert row["encrypted_key"] == b"ciphertext-1"
    assert row["is_active"] is True
    assert row["last_used_at"] is None


def test_get_credential_absent_row_returns_none(db_conn):
    _seed_user(db_conn, "byo-u2")

    assert get_credential(db_conn, "byo-u2", "gemini") is None


def test_upsert_reactivates_a_deactivated_credential(db_conn):
    _seed_user(db_conn, "byo-u3")
    upsert_credential(db_conn, "byo-u3", "groq", b"key-a")
    deactivate_credential(db_conn, "byo-u3", "groq")
    assert get_credential(db_conn, "byo-u3", "groq")["is_active"] is False

    upsert_credential(db_conn, "byo-u3", "groq", b"key-b")

    row = get_credential(db_conn, "byo-u3", "groq")
    assert row["is_active"] is True
    assert row["encrypted_key"] == b"key-b"


def test_deactivate_missing_row_returns_false(db_conn):
    _seed_user(db_conn, "byo-u4")

    assert deactivate_credential(db_conn, "byo-u4", "cerebras") is False


def test_get_active_providers_excludes_inactive(db_conn):
    _seed_user(db_conn, "byo-u5")
    upsert_credential(db_conn, "byo-u5", "gemini", b"k1")
    upsert_credential(db_conn, "byo-u5", "groq", b"k2")
    deactivate_credential(db_conn, "byo-u5", "groq")

    providers = get_active_providers(db_conn, "byo-u5")

    assert providers == ["gemini"]


def test_get_active_providers_empty_for_unknown_user(db_conn):
    _seed_user(db_conn, "byo-u6")

    assert get_active_providers(db_conn, "byo-u6") == []


def test_touch_last_used_stamps_timestamp(db_conn):
    _seed_user(db_conn, "byo-u7")
    upsert_credential(db_conn, "byo-u7", "cerebras", b"k")
    assert get_credential(db_conn, "byo-u7", "cerebras")["last_used_at"] is None

    touch_last_used(db_conn, "byo-u7", "cerebras")

    assert get_credential(db_conn, "byo-u7", "cerebras")["last_used_at"] is not None


def test_touch_last_used_missing_row_is_a_silent_no_op(db_conn):
    _seed_user(db_conn, "byo-u8")

    touch_last_used(db_conn, "byo-u8", "gemini")  # must not raise

    assert get_credential(db_conn, "byo-u8", "gemini") is None


# --- RLS policy (m0020) ---


def _impersonate_nonsuperuser_reader(db_conn) -> str:
    """db_conn connects as the Postgres superuser (POSTGRES_ADMIN_DSN), and
    superusers/BYPASSRLS roles always bypass RLS regardless of FORCE -- a
    bare SELECT under db_conn's own role would pass even with a broken
    policy. Same convention as
    tests/host/test_schema.py::test_byo_key_credentials_force_rls_denies_owner_insert:
    create a throwaway NOLOGIN role (NOBYPASSRLS by default), grant it SELECT,
    and SET ROLE to it so the policy is actually exercised. CREATE ROLE is
    transactional, so db_conn's own per-test ROLLBACK undoes it -- callers
    still RESET ROLE in a finally so later statements in the same test body
    (before rollback) aren't left running as the throwaway role.
    """
    role = f"byo_rls_test_reader_{uuid.uuid4().hex[:8]}"
    db_conn.execute(f"CREATE ROLE {role} NOLOGIN")
    db_conn.execute(f"GRANT SELECT ON byo_key_credentials TO {role}")
    db_conn.execute(f"SET ROLE {role}")
    return role


def test_rls_blocks_cross_tenant_read_via_bare_select(db_conn):
    """m0020's tenant_isolation policy: a raw SELECT scoped to a DIFFERENT
    app.user_id session var than the row's owner sees zero rows, even
    though the row exists -- proving the policy is doing the filtering,
    not just this module's own WHERE user_id = %s clauses."""
    _seed_user(db_conn, "byo-owner")
    upsert_credential(db_conn, "byo-owner", "gemini", b"secret")

    _impersonate_nonsuperuser_reader(db_conn)
    try:
        db_conn.execute("SELECT set_config('app.user_id', %s, true)", ("byo-owner",))
        owner_rows = db_conn.execute("SELECT provider FROM byo_key_credentials").fetchall()
        assert [r["provider"] for r in owner_rows] == ["gemini"]

        db_conn.execute("SELECT set_config('app.user_id', %s, true)", ("someone-else",))
        stranger_rows = db_conn.execute("SELECT provider FROM byo_key_credentials").fetchall()
        assert stranger_rows == []
    finally:
        db_conn.execute("RESET ROLE")


def test_rls_blocks_read_with_no_session_var_set(db_conn):
    """current_setting('app.user_id', true) is NULL when never set, and
    `user_id = NULL` is never true -- default-deny even after m0020's
    policy exists, matching m0001's original FORCE RLS intent."""
    _seed_user(db_conn, "byo-owner2")
    upsert_credential(db_conn, "byo-owner2", "gemini", b"secret")

    _impersonate_nonsuperuser_reader(db_conn)
    try:
        # upsert_credential's own _set_tenant call is LOCAL to its own
        # internal statement, not the ambient test transaction as a whole --
        # but to keep this test's intent unambiguous (no caller has asked
        # for any tenant), explicitly reset rather than relying on that
        # scoping detail.
        db_conn.execute("RESET app.user_id")

        rows = db_conn.execute("SELECT provider FROM byo_key_credentials").fetchall()
        assert rows == []
    finally:
        db_conn.execute("RESET ROLE")

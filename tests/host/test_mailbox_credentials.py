"""jobcannon.db._mailbox_credentials -- single reader/writer for
mailbox_credentials (L-0115 PR-1/PR-3), plus the RLS policy m0025 adds.

Not a port: no private-repo equivalent exists (job-cannon private is
single-user and keeps its one IMAP mailbox in a gitignored config.yaml).
Exercises both the CRUD surface and the tenant-isolation RLS policy itself,
mirroring tests/host/test_byo_key_credentials.py's template exactly -- same
convention, same risk (a bug in either the module or the migration produces
the same visible symptom: a query returns rows it should not, or none).
"""

from __future__ import annotations

import uuid

from jobcannon.db._mailbox_credentials import (
    advance_uid_highwater,
    deactivate_credential,
    get_active_for_user,
    set_mailbox_credential,
    touch_last_used,
)

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))


def _set_default(conn, user_id, **overrides):
    kwargs = dict(
        imap_host="imap.example.org",
        imap_port=993,
        auth_type="app_password",
        folder="INBOX",
        encrypted_secret=b"ciphertext-1",
        username_hint="j***@example.org",
    )
    kwargs.update(overrides)
    set_mailbox_credential(conn, user_id, **kwargs)


def test_set_then_get_round_trips(db_conn):
    _seed_user(db_conn, "mbx-u1")

    _set_default(db_conn, "mbx-u1")

    row = get_active_for_user(db_conn, "mbx-u1")
    assert row is not None
    assert row["imap_host"] == "imap.example.org"
    assert row["imap_port"] == 993
    assert row["auth_type"] == "app_password"
    assert row["folder"] == "INBOX"
    assert row["encrypted_secret"] == b"ciphertext-1"
    assert row["username_hint"] == "j***@example.org"
    assert row["uid_highwater"] == 0
    assert row["uid_validity"] == 0
    assert row["last_used_at"] is None


def test_get_active_for_user_absent_row_returns_none(db_conn):
    _seed_user(db_conn, "mbx-u2")

    assert get_active_for_user(db_conn, "mbx-u2") is None


def test_set_mailbox_credential_reactivates_and_resets_progress(db_conn):
    _seed_user(db_conn, "mbx-u3")
    _set_default(db_conn, "mbx-u3")
    advance_uid_highwater(db_conn, "mbx-u3", uid_highwater=42, uid_validity=7)
    deactivate_credential(db_conn, "mbx-u3")
    assert get_active_for_user(db_conn, "mbx-u3") is None

    _set_default(db_conn, "mbx-u3", encrypted_secret=b"ciphertext-2")

    row = get_active_for_user(db_conn, "mbx-u3")
    assert row is not None
    assert row["encrypted_secret"] == b"ciphertext-2"
    # Progress resets: a new credential invalidates any prior fetch progress.
    assert row["uid_highwater"] == 0
    assert row["uid_validity"] == 0


def test_deactivate_missing_row_returns_false(db_conn):
    _seed_user(db_conn, "mbx-u4")

    assert deactivate_credential(db_conn, "mbx-u4") is False


def test_touch_last_used_stamps_timestamp(db_conn):
    _seed_user(db_conn, "mbx-u5")
    _set_default(db_conn, "mbx-u5")
    assert get_active_for_user(db_conn, "mbx-u5")["last_used_at"] is None

    touch_last_used(db_conn, "mbx-u5")

    assert get_active_for_user(db_conn, "mbx-u5")["last_used_at"] is not None


def test_touch_last_used_missing_row_is_a_silent_no_op(db_conn):
    _seed_user(db_conn, "mbx-u6")

    touch_last_used(db_conn, "mbx-u6")  # must not raise

    assert get_active_for_user(db_conn, "mbx-u6") is None


def test_advance_uid_highwater_sets_both_columns_together(db_conn):
    _seed_user(db_conn, "mbx-u7")
    _set_default(db_conn, "mbx-u7")

    advance_uid_highwater(db_conn, "mbx-u7", uid_highwater=100, uid_validity=55)

    row = get_active_for_user(db_conn, "mbx-u7")
    assert row["uid_highwater"] == 100
    assert row["uid_validity"] == 55


def test_advance_uid_highwater_on_same_epoch_still_writes_both(db_conn):
    """No partial-update variant exists -- even a same-epoch continuation
    (uid_validity unchanged) re-sets both columns together."""
    _seed_user(db_conn, "mbx-u8")
    _set_default(db_conn, "mbx-u8")
    advance_uid_highwater(db_conn, "mbx-u8", uid_highwater=10, uid_validity=99)

    advance_uid_highwater(db_conn, "mbx-u8", uid_highwater=20, uid_validity=99)

    row = get_active_for_user(db_conn, "mbx-u8")
    assert row["uid_highwater"] == 20
    assert row["uid_validity"] == 99


# --- RLS policy (m0025) ---


def _impersonate_nonsuperuser_reader(db_conn) -> str:
    """Same convention as test_byo_key_credentials.py's helper of the same
    name: superusers/BYPASSRLS roles always bypass RLS, so a bare SELECT
    under db_conn's own role would pass even with a broken policy. Create a
    throwaway NOLOGIN role, grant it SELECT, and SET ROLE to it so m0025's
    tenant_isolation policy is actually exercised. CREATE ROLE is
    transactional, so db_conn's own per-test ROLLBACK undoes it -- callers
    still RESET ROLE in a finally.
    """
    role = f"mbx_rls_test_reader_{uuid.uuid4().hex[:8]}"
    db_conn.execute(f"CREATE ROLE {role} NOLOGIN")
    db_conn.execute(f"GRANT SELECT ON mailbox_credentials TO {role}")
    db_conn.execute(f"SET ROLE {role}")
    return role


def test_rls_blocks_cross_tenant_read_via_bare_select(db_conn):
    _seed_user(db_conn, "mbx-owner")
    _set_default(db_conn, "mbx-owner")

    _impersonate_nonsuperuser_reader(db_conn)
    try:
        db_conn.execute("SELECT set_config('app.user_id', %s, true)", ("mbx-owner",))
        owner_rows = db_conn.execute("SELECT imap_host FROM mailbox_credentials").fetchall()
        assert [r["imap_host"] for r in owner_rows] == ["imap.example.org"]

        db_conn.execute("SELECT set_config('app.user_id', %s, true)", ("someone-else",))
        stranger_rows = db_conn.execute("SELECT imap_host FROM mailbox_credentials").fetchall()
        assert stranger_rows == []
    finally:
        db_conn.execute("RESET ROLE")


def test_rls_blocks_read_with_no_session_var_set(db_conn):
    _seed_user(db_conn, "mbx-owner2")
    _set_default(db_conn, "mbx-owner2")

    _impersonate_nonsuperuser_reader(db_conn)
    try:
        db_conn.execute("RESET app.user_id")

        rows = db_conn.execute("SELECT imap_host FROM mailbox_credentials").fetchall()
        assert rows == []
    finally:
        db_conn.execute("RESET ROLE")

"""jobcannon/db/_users.py — the `users` table's single writer.

`profiles.user_id` is a FK to `users(id)` with no ON CONFLICT fallback
(tests/host/test_profiles_dal.py::test_upsert_profile_fails_loud_on_missing_user),
so `mint_anon_user` exists specifically to close that race: insert the
parent row first, then the caller can `upsert_profile()` against the
returned id in the same transaction without failing loud."""

from __future__ import annotations


def test_mint_anon_user_returns_prefixed_id_and_row_exists(db_conn):
    from jobcannon.db._users import ANON_ID_PREFIX, mint_anon_user

    user_id = mint_anon_user(db_conn)

    assert user_id.startswith(ANON_ID_PREFIX)
    row = db_conn.execute("SELECT plan_tier FROM users WHERE id = %s", (user_id,)).fetchone()
    assert row is not None
    assert row["plan_tier"] == "free"


def test_mint_anon_user_ids_are_unique_across_calls(db_conn):
    from jobcannon.db._users import mint_anon_user

    first = mint_anon_user(db_conn)
    second = mint_anon_user(db_conn)

    assert first != second
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE id IN (%s, %s)", (first, second)
    ).fetchone()["n"]
    assert n == 2


def test_ensure_user_is_idempotent_and_preserves_email(db_conn):
    from jobcannon.db._users import ensure_user

    ensure_user(db_conn, "u_ensure", email="known@example.org")
    # A later call with an unresolvable email (None) must not clobber the
    # already-known one — mirrors an at-least-once Clerk redelivery whose
    # primary_email_address_id no longer resolves.
    ensure_user(db_conn, "u_ensure", email=None)

    row = db_conn.execute("SELECT email FROM users WHERE id = %s", ("u_ensure",)).fetchone()
    assert row["email"] == "known@example.org"

    n = db_conn.execute("SELECT COUNT(*) AS n FROM users WHERE id = %s", ("u_ensure",)).fetchone()[
        "n"
    ]
    assert n == 1


def test_upsert_profile_succeeds_immediately_after_mint_anon_user(db_conn):
    from jobcannon.db._profiles import get_profile, upsert_profile
    from jobcannon.db._users import mint_anon_user

    user_id = mint_anon_user(db_conn)
    upsert_profile(db_conn, user_id, skills=["sql"], workplace_type=None)

    row = get_profile(db_conn, user_id)
    assert row is not None
    assert row["skills"] == ["sql"]


def test_anon_prefix_does_not_collide_with_guest_sentinel(db_conn):
    from jobcannon.db._profiles import GUEST_USER_ID
    from jobcannon.db._users import ANON_ID_PREFIX, is_anon_id, mint_anon_user

    assert not GUEST_USER_ID.startswith(ANON_ID_PREFIX)
    assert not is_anon_id(GUEST_USER_ID)
    assert not is_anon_id("user_abc")  # Clerk-issued ids never carry ANON_ID_PREFIX

    anon_id = mint_anon_user(db_conn)
    assert is_anon_id(anon_id)
    assert anon_id != GUEST_USER_ID

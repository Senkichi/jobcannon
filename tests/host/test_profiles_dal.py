"""profiles DAL roundtrip: single-writer upsert_profile + get_profile
(1B Wave 3 PR 11 — profiles had no writer anywhere on merged main; this is
the first one, single-writer by construction). profiles.user_id is PK
REFERENCES users(id) (m0001), so every test seeds a users row first."""

from __future__ import annotations


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def test_get_profile_returns_none_for_missing_user(db_conn):
    from jobcannon.db._profiles import get_profile

    assert get_profile(db_conn, "nobody") is None


def test_upsert_profile_then_get_profile_roundtrip(db_conn):
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u1")
    upsert_profile(db_conn, "u1", skills=["sql", "python"], seniority_level="senior")

    row = get_profile(db_conn, "u1")
    assert row["skills"] == ["sql", "python"]
    assert row["seniority_level"] == "senior"


def test_upsert_profile_updates_in_place_and_preserves_unspecified_fields(db_conn):
    """Single current row (OD-5): a second upsert updates in place (no second
    row) and COALESCEs unspecified columns to their previous values — the
    second call only touches seniority_level, so skills must survive."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u2")
    upsert_profile(db_conn, "u2", skills=["sql", "python"], seniority_level="senior")
    upsert_profile(db_conn, "u2", seniority_level="staff")

    row = get_profile(db_conn, "u2")
    assert row["seniority_level"] == "staff"
    assert row["skills"] == ["sql", "python"]


def test_seed_guest_demo_is_idempotent(db_conn):
    """scripts/seed_guest_demo.py's seed(conn): running it twice must leave
    exactly one users row and one current profile row (OD-5), unchanged by
    the second call (users insert is ON CONFLICT DO NOTHING; upsert_profile
    is itself an upsert)."""
    from jobcannon.db._profiles import GUEST_USER_ID, get_profile
    from scripts import seed_guest_demo

    seed_guest_demo.seed(db_conn)
    first = get_profile(db_conn, GUEST_USER_ID)
    assert first is not None
    assert first["seniority_level"] == "senior"

    seed_guest_demo.seed(db_conn)
    second = get_profile(db_conn, GUEST_USER_ID)
    assert second == first

    user_count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE id = %s", (GUEST_USER_ID,)
    ).fetchone()["n"]
    assert user_count == 1

    profile_count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM profiles WHERE user_id = %s", (GUEST_USER_ID,)
    ).fetchone()["n"]
    assert profile_count == 1

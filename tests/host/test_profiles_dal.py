"""profiles DAL roundtrip: single-writer upsert_profile + get_profile
(1B Wave 3 PR 11 — profiles had no writer anywhere on merged main; this is
the first one, single-writer by construction). profiles.user_id is PK
REFERENCES users(id) (m0001), so every test seeds a users row first."""

from __future__ import annotations

import psycopg.errors
import pytest


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def test_get_profile_returns_none_for_missing_user(db_conn):
    from jobcannon.db._profiles import get_profile

    assert get_profile(db_conn, "nobody") is None


def test_upsert_profile_fails_loud_on_missing_user(db_conn):
    """profiles.user_id REFERENCES users(id) (m0001) with no ON CONFLICT
    fallback for a missing parent row: upsert_profile must fail loud
    (ForeignKeyViolation), not silently no-op. Locks the fail-loud contract
    scripts/seed_guest_demo.py's insert-user-first ordering depends on —
    reversing that order would previously have failed just as loudly but
    without a regression test pinning it."""
    from jobcannon.db._profiles import upsert_profile

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        upsert_profile(db_conn, "nonexistent-user", skills=["x"])


def test_upsert_profile_then_get_profile_roundtrip(db_conn):
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u1")
    upsert_profile(db_conn, "u1", skills=["sql", "python"], seniority_level="senior")

    row = get_profile(db_conn, "u1")
    assert row["skills"] == ["sql", "python"]
    assert row["seniority_level"] == "senior"


def test_upsert_profile_updates_in_place_and_preserves_unspecified_fields(db_conn):
    """Single current row: a second upsert updates in place (no second
    row) and COALESCEs unspecified columns to their previous values — the
    second call only touches seniority_level, so skills must survive."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u2")
    upsert_profile(db_conn, "u2", skills=["sql", "python"], seniority_level="senior")
    upsert_profile(db_conn, "u2", seniority_level="staff")

    row = get_profile(db_conn, "u2")
    assert row["seniority_level"] == "staff"
    assert row["skills"] == ["sql", "python"]


def test_upsert_profile_then_get_profile_roundtrips_comp_floor_usd(db_conn):
    """#28 item 2: comp_floor_usd (m0008) roundtrips through the same
    single-writer seam every other profiles column already does."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u3")
    upsert_profile(db_conn, "u3", comp_floor_usd=120000)

    row = get_profile(db_conn, "u3")
    assert row["comp_floor_usd"] == 120000


def test_upsert_profile_omitted_comp_floor_usd_defaults_null(db_conn):
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u4")
    upsert_profile(db_conn, "u4", seniority_level="mid")

    row = get_profile(db_conn, "u4")
    assert row["comp_floor_usd"] is None


def test_upsert_profile_second_call_without_comp_floor_usd_preserves_it(db_conn):
    """Same COALESCE-preservation contract every other column gets: an
    omitted comp_floor_usd on a later call must not clobber an
    already-set floor with NULL."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u5")
    upsert_profile(db_conn, "u5", comp_floor_usd=95000)
    upsert_profile(db_conn, "u5", seniority_level="staff")

    row = get_profile(db_conn, "u5")
    assert row["seniority_level"] == "staff"
    assert row["comp_floor_usd"] == 95000


def test_seed_guest_demo_is_idempotent(db_conn):
    """scripts/seed_guest_demo.py's seed(conn): running it twice must leave
    exactly one users row and one current profile row, unchanged by
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

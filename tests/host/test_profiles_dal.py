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
        upsert_profile(db_conn, "nonexistent-user", skills=["x"], workplace_type=None)


def test_upsert_profile_requires_workplace_type_explicitly(db_conn):
    """workplace_type is a plain-overwrite column (not COALESCE-preserve —
    see jobcannon/db/_profiles.py's docstring), so it has no default: an
    omitting caller must get a TypeError at the call site, not a silent
    NULL the next time this row is touched. This test is the structural
    guard — if a default is ever re-added, this goes red."""
    from jobcannon.db._profiles import upsert_profile

    _seed_user(db_conn, "u0")
    with pytest.raises(TypeError):
        upsert_profile(db_conn, "u0", skills=["x"])


def test_upsert_profile_then_get_profile_roundtrip(db_conn):
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u1")
    upsert_profile(
        db_conn, "u1", skills=["sql", "python"], seniority_level="senior", workplace_type=None
    )

    row = get_profile(db_conn, "u1")
    assert row["skills"] == ["sql", "python"]
    assert row["seniority_level"] == "senior"


def test_upsert_profile_updates_in_place_and_preserves_unspecified_fields(db_conn):
    """Single current row: a second upsert updates in place (no second
    row) and COALESCEs unspecified columns to their previous values — the
    second call only touches seniority_level, so skills must survive."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u2")
    upsert_profile(
        db_conn, "u2", skills=["sql", "python"], seniority_level="senior", workplace_type=None
    )
    upsert_profile(db_conn, "u2", seniority_level="staff", workplace_type=None)

    row = get_profile(db_conn, "u2")
    assert row["seniority_level"] == "staff"
    assert row["skills"] == ["sql", "python"]


def test_upsert_profile_then_get_profile_roundtrips_comp_floor_usd(db_conn):
    """#28 item 2: comp_floor_usd (m0008) roundtrips through the same
    single-writer seam every other profiles column already does."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u3")
    upsert_profile(db_conn, "u3", comp_floor_usd=120000, workplace_type=None)

    row = get_profile(db_conn, "u3")
    assert row["comp_floor_usd"] == 120000


def test_upsert_profile_omitted_comp_floor_usd_defaults_null(db_conn):
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u4")
    upsert_profile(db_conn, "u4", seniority_level="mid", workplace_type=None)

    row = get_profile(db_conn, "u4")
    assert row["comp_floor_usd"] is None


def test_upsert_profile_second_call_without_comp_floor_usd_preserves_it(db_conn):
    """Same COALESCE-preservation contract every other column gets: an
    omitted comp_floor_usd on a later call must not clobber an
    already-set floor with NULL."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u5")
    upsert_profile(db_conn, "u5", comp_floor_usd=95000, workplace_type=None)
    upsert_profile(db_conn, "u5", seniority_level="staff", workplace_type=None)

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


def test_upsert_profile_second_call_omitting_target_companies_preserves_it(db_conn):
    """target_companies is COALESCE-preserve-when-omitted (None), same as
    every other jsonb column except workplace_type — a later call that
    doesn't mention companies must not clobber a prior selection."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u6")
    upsert_profile(db_conn, "u6", target_companies=["Acme Corp"], workplace_type=None)
    upsert_profile(db_conn, "u6", seniority_level="staff", workplace_type=None)

    row = get_profile(db_conn, "u6")
    assert row["seniority_level"] == "staff"
    assert row["target_companies"] == ["Acme Corp"]


def test_upsert_profile_second_call_with_empty_target_companies_clears_it(db_conn):
    """An explicit empty list is a real, non-NULL jsonb value (`Jsonb([])`),
    so COALESCE picks it over the prior selection — this is what lets a
    picker resubmission actually clear a saved company filter."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u7")
    upsert_profile(db_conn, "u7", target_companies=["Acme Corp"], workplace_type=None)
    upsert_profile(db_conn, "u7", target_companies=[], workplace_type=None)

    row = get_profile(db_conn, "u7")
    assert row["target_companies"] == []


def test_upsert_profile_explicit_workplace_type_none_overwrites_a_prior_value(db_conn):
    """workplace_type is plain-overwrite (not COALESCE), so a caller that
    explicitly passes workplace_type=None resets a prior non-null value to
    NULL — this is the mechanism a feed 'Any' selection or a picker reset
    depends on to actually widen back to no preference."""
    from jobcannon.db._profiles import get_profile, upsert_profile

    _seed_user(db_conn, "u8")
    upsert_profile(db_conn, "u8", workplace_type="REMOTE")
    upsert_profile(db_conn, "u8", seniority_level="staff", workplace_type=None)

    row = get_profile(db_conn, "u8")
    assert row["seniority_level"] == "staff"
    assert row["workplace_type"] is None

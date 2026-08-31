"""replace_profile (jobcannon/db/_profiles.py) — the profile editor's
plain-overwrite snapshot writer (Spec 2, plan Deviation 1). Rollback-isolated
`db_conn` fixture, same shape as tests/host/test_profiles_dal.py."""

from __future__ import annotations

from decimal import Decimal

import psycopg.errors
import pytest


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def _full_snapshot(**overrides):
    snapshot = {
        "skills": ["python", "sql"],
        "experience_summary": "Twelve years of backend work.",
        "target_titles": ["Staff Engineer"],
        "target_locations": ["Seattle, WA", "Remote"],
        "seniority_level": "staff",
        "years_of_experience": 12.5,
        "comp_floor_usd": 180000,
        "target_companies": ["Acme"],
        "workplace_type": "REMOTE",
    }
    snapshot.update(overrides)
    return snapshot


def test_replace_profile_roundtrips_every_column(db_conn):
    from jobcannon.db._profiles import get_profile, replace_profile

    _seed_user(db_conn, "rp-roundtrip")
    replace_profile(db_conn, "rp-roundtrip", **_full_snapshot())

    row = get_profile(db_conn, "rp-roundtrip")
    assert row["skills"] == ["python", "sql"]
    assert row["experience_summary"] == "Twelve years of backend work."
    assert row["target_titles"] == ["Staff Engineer"]
    assert row["target_locations"] == ["Seattle, WA", "Remote"]
    assert row["seniority_level"] == "staff"
    assert row["years_of_experience"] == Decimal("12.5")
    assert row["comp_floor_usd"] == 180000
    assert row["target_companies"] == ["Acme"]
    assert row["workplace_type"] == "REMOTE"
    assert row["updated_at"] is not None


def test_replace_profile_clears_scalars_and_lists_upsert_would_preserve(db_conn):
    """The reason this function exists: upsert_profile's COALESCE keeps a
    previously-stored scalar when the caller passes None. A complete-snapshot
    editor must be able to blank every field, so replace_profile writes
    NULL for None and [] for an empty list, overwriting whatever was there."""
    from jobcannon.db._profiles import get_profile, replace_profile, upsert_profile

    _seed_user(db_conn, "rp-clear")
    upsert_profile(
        db_conn,
        "rp-clear",
        skills=["python"],
        experience_summary="old summary",
        target_titles=["Old Title"],
        target_locations=["Old Town"],
        seniority_level="senior",
        years_of_experience=9,
        comp_floor_usd=150000,
        target_companies=["OldCo"],
        workplace_type="HYBRID",
    )

    replace_profile(
        db_conn,
        "rp-clear",
        skills=[],
        experience_summary=None,
        target_titles=[],
        target_locations=[],
        seniority_level=None,
        years_of_experience=None,
        comp_floor_usd=None,
        target_companies=[],
        workplace_type=None,
    )

    row = get_profile(db_conn, "rp-clear")
    assert row["skills"] == []
    assert row["experience_summary"] is None
    assert row["target_titles"] == []
    assert row["target_locations"] == []
    assert row["seniority_level"] is None
    assert row["years_of_experience"] is None
    assert row["comp_floor_usd"] is None
    assert row["target_companies"] == []
    assert row["workplace_type"] is None


def test_replace_profile_second_call_overwrites_not_merges(db_conn):
    from jobcannon.db._profiles import get_profile, replace_profile

    _seed_user(db_conn, "rp-twice")
    replace_profile(db_conn, "rp-twice", **_full_snapshot())
    replace_profile(
        db_conn,
        "rp-twice",
        **_full_snapshot(target_titles=["Principal Engineer"], years_of_experience=13),
    )

    row = get_profile(db_conn, "rp-twice")
    assert row["target_titles"] == ["Principal Engineer"]
    assert row["years_of_experience"] == Decimal("13")
    # Untouched keys in the second snapshot still arrived as the same
    # literal values, so they read back unchanged — one row, PK user_id.
    count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM profiles WHERE user_id = %s", ("rp-twice",)
    ).fetchone()["n"]
    assert count == 1


def test_replace_profile_rejects_an_incomplete_snapshot():
    """Every kwarg is required: an omitted field is a TypeError at the call
    site, before any SQL runs — the required-kwarg contract upsert_profile
    applies only to workplace_type, generalized to the whole row. Pure
    Python, no database needed (conn is never touched)."""
    from jobcannon.db._profiles import replace_profile

    snapshot = _full_snapshot()
    del snapshot["comp_floor_usd"]
    with pytest.raises(TypeError):
        replace_profile(object(), "rp-incomplete", **snapshot)


def test_replace_profile_requires_an_existing_user(db_conn):
    """profiles.user_id REFERENCES users(id): no users row, no profile.
    Savepoint-scoped so the aborted statement doesn't poison the fixture's
    outer transaction."""
    from jobcannon.db._profiles import replace_profile

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db_conn.transaction():
            replace_profile(db_conn, "rp-no-such-user", **_full_snapshot())

"""Host-side candidate-context resolver: resolve + build.

The load-bearing test is multi-tenancy: two users with IDENTICAL profile
column values must resolve independently — mutating one and re-resolving
the OTHER must show no bleed-through. It fails if resolution is ever
memoized on profile/config shape instead of user_id (the rejected
process-global-cache design)."""

from __future__ import annotations

import pytest


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


# Keys deliberately match the profiles column names (m0001) so the same
# mapping exercises both upsert_profile kwargs and build_candidate_context.
PROFILE_FIELDS = {
    "skills": ["python", "sql"],
    "experience_summary": "Data platform and pipeline work.",
    "target_titles": ["Data Engineer"],
    "target_locations": ["Remote"],
    "seniority_level": "senior",
    "years_of_experience": 8,
}


def test_resolve_returns_nonempty_string_for_seeded_profile(db_conn):
    from jobcannon.db._profiles import upsert_profile
    from jobcannon.host.candidate_context import resolve_candidate_context

    _seed_user(db_conn, "ctx-user")
    upsert_profile(db_conn, "ctx-user", **PROFILE_FIELDS)

    context = resolve_candidate_context(db_conn, "ctx-user")
    assert isinstance(context, str)
    assert context.strip()
    assert "senior" in context
    assert "Data Engineer" in context


def test_resolve_raises_typed_error_naming_the_user_for_missing_profile(db_conn):
    from jobcannon.host.candidate_context import (
        ProfileNotFoundError,
        resolve_candidate_context,
    )

    with pytest.raises(ProfileNotFoundError) as exc_info:
        resolve_candidate_context(db_conn, "no-such-user")
    # Typed (not AssertionError, not bare ValueError) and carries a message
    # naming the user.
    assert not isinstance(exc_info.value, (AssertionError, ValueError))
    assert "no-such-user" in str(exc_info.value)
    assert exc_info.value.user_id == "no-such-user"


def test_two_tenants_with_identical_profiles_resolve_independently(db_conn):
    """Seed two users with identical column values, mutate one, re-resolve
    the OTHER. Must fail if a config/content-keyed global cache is ever
    reintroduced: identical inputs would share one cache slot, and tenant
    B's mutation would either leak into A or be masked by A's stale entry."""
    from jobcannon.db._profiles import upsert_profile
    from jobcannon.host.candidate_context import resolve_candidate_context

    _seed_user(db_conn, "tenant-a")
    _seed_user(db_conn, "tenant-b")
    upsert_profile(db_conn, "tenant-a", **PROFILE_FIELDS)
    upsert_profile(db_conn, "tenant-b", **PROFILE_FIELDS)

    before_a = resolve_candidate_context(db_conn, "tenant-a")
    before_b = resolve_candidate_context(db_conn, "tenant-b")
    assert before_a == before_b  # identical inputs render identically

    upsert_profile(db_conn, "tenant-b", seniority_level="staff", target_locations=["Onsite NYC"])

    after_b = resolve_candidate_context(db_conn, "tenant-b")
    after_a = resolve_candidate_context(db_conn, "tenant-a")
    assert after_b != before_b
    assert "staff" in after_b
    assert "Onsite NYC" in after_b
    assert after_a == before_a  # tenant A never observes B's mutation
    assert "staff" not in after_a
    assert "Onsite NYC" not in after_a


def test_build_candidate_context_is_pure_plain_dict_no_conn():
    from jobcannon.host.candidate_context import build_candidate_context

    context = build_candidate_context(dict(PROFILE_FIELDS))
    assert "python, sql" in context
    assert "Years of experience: 8" in context
    assert "Data platform and pipeline work." in context


def test_build_candidate_context_nonempty_for_empty_mapping():
    """_build_system_prompt raises on empty context, so build must return a
    non-empty string even when every field is absent."""
    from jobcannon.host.candidate_context import build_candidate_context

    context = build_candidate_context({})
    assert context.strip()
    assert "Not specified" in context


def test_build_candidate_context_none_and_empty_values_render_placeholders():
    from jobcannon.host.candidate_context import build_candidate_context

    context = build_candidate_context(
        {
            "skills": [],
            "experience_summary": "   ",
            "target_titles": None,
            "target_locations": ["", "  "],
            "seniority_level": None,
            "years_of_experience": None,
        }
    )
    assert context.count("Not specified") == 6

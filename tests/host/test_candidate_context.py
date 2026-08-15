"""Host-side candidate-context resolver: resolve + build.

The load-bearing test is multi-tenancy, and it seeds DISTINCT content per
tenant on purpose: identity of output then proves identity of tenant. A
first draft seeded identical profiles and asserted the two contexts were
equal — which cannot distinguish "B got B's data" from "B got A's data",
and let an injected process-global cache keyed on profile shape pass every
test. Distinct seeds, cross-negatives, and a collision tenant sharing
seniority_level AND target_locations kill that class: any cache keyed on
those session-held fields (either one, or the pair) collides two tenants
here and serves one tenant's context to the other.

That content-shaped defense still has a hole: a composite cache key built
from one mutation-touched field plus one high-cardinality field (e.g.
"seniority_level,skills") would pass every content assertion above while
genuinely leaking context, because no two seeded tenants collide on that
particular pairing. The call-count assertions below are key-agnostic — they
pin both `get_profile` (the DB read) and `build_candidate_context` (the
render) to exactly one call per `resolve_candidate_context` invocation, so
a hit on ANY cache — whether it short-circuits before the fetch or memoizes
after it — under-counts one of the two and goes red, without needing to
know the key scheme or enumerate collision combinations."""

from __future__ import annotations

from unittest.mock import patch

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

# Every value distinct from PROFILE_FIELDS: cross-negative assertions need
# tokens that can only have come from one tenant's row.
TENANT_B_FIELDS = {
    "skills": ["golang", "kubernetes"],
    "experience_summary": "Infrastructure and reliability work.",
    "target_titles": ["Platform Engineer"],
    "target_locations": ["Onsite NYC"],
    "seniority_level": "staff",
    "years_of_experience": 12,
}

# Collides with PROFILE_FIELDS on seniority_level AND target_locations while
# differing in every token-bearing field: a cache keyed on either of those
# fields, or on both together, collides these two tenants and hands one of
# them the other's context.
TENANT_C_FIELDS = {
    "skills": ["rust"],
    "experience_summary": "Embedded systems work.",
    "target_titles": ["Firmware Engineer"],
    "target_locations": ["Remote"],
    "seniority_level": "senior",
    "years_of_experience": 5,
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


def test_tenants_resolve_their_own_content_never_each_others(db_conn):
    """Three tenants with distinct content; C shares seniority_level with A.

    Cross-negatives prove each context carries only its own tenant's tokens —
    a shape-keyed or field-keyed process-global cache serves one tenant's
    context to another and fails one of these assertions. Then mutate B and
    re-resolve A: A must never observe B's mutation.

    Wrapped in `get_profile`/`build_candidate_context` call-count assertions,
    key-agnostic to whatever composite the content checks above don't happen
    to collide on: every `resolve_candidate_context` call — including the
    repeat calls for tenant-a and tenant-b after the mutation — must cost
    exactly one call to each. A cache hit on any of those repeats, whether it
    skips the fetch or just memoizes the render, under-counts one of the two
    and fails before the content assertions even run."""
    from jobcannon.db._profiles import get_profile, upsert_profile
    from jobcannon.host.candidate_context import (
        build_candidate_context,
        resolve_candidate_context,
    )

    for user_id, fields in [
        ("tenant-a", PROFILE_FIELDS),
        ("tenant-b", TENANT_B_FIELDS),
        ("tenant-c", TENANT_C_FIELDS),
    ]:
        _seed_user(db_conn, user_id)
        upsert_profile(db_conn, user_id, **fields)

    with (
        patch(
            "jobcannon.host.candidate_context.get_profile", wraps=get_profile
        ) as mock_get_profile,
        patch(
            "jobcannon.host.candidate_context.build_candidate_context",
            wraps=build_candidate_context,
        ) as mock_build_context,
    ):
        ctx_a = resolve_candidate_context(db_conn, "tenant-a")
        assert mock_get_profile.call_count == 1
        assert mock_build_context.call_count == 1
        ctx_b = resolve_candidate_context(db_conn, "tenant-b")
        assert mock_get_profile.call_count == 2
        assert mock_build_context.call_count == 2
        ctx_c = resolve_candidate_context(db_conn, "tenant-c")
        assert mock_get_profile.call_count == 3
        assert mock_build_context.call_count == 3

        assert ctx_a != ctx_b
        assert "python" in ctx_a
        assert "python" not in ctx_b
        assert "golang" in ctx_b
        assert "golang" not in ctx_a

        # C collides with A on seniority_level AND target_locations: a memo
        # keyed on either field, or on the pair, hands C A's context.
        assert ctx_c != ctx_a
        assert "rust" in ctx_c
        assert "python" not in ctx_c
        assert "Data Engineer" not in ctx_c
        assert "Firmware Engineer" in ctx_c

        # Mutation independence: B's update must never surface in A. Both
        # repeat resolves (tenant-b, tenant-a) must each cost a fresh
        # get_profile call — a cache keyed on user_id alone would serve
        # stale content here without ever going stale on the wrong tenant,
        # which the content assertions can't distinguish from a real fetch.
        upsert_profile(
            db_conn, "tenant-b", seniority_level="principal", target_locations=["Chicago"]
        )
        after_b = resolve_candidate_context(db_conn, "tenant-b")
        assert mock_get_profile.call_count == 4
        assert mock_build_context.call_count == 4
        after_a = resolve_candidate_context(db_conn, "tenant-a")
        assert mock_get_profile.call_count == 5
        assert mock_build_context.call_count == 5

    assert "principal" in after_b
    assert "Chicago" in after_b
    assert after_a == ctx_a
    assert "principal" not in after_a
    assert "Chicago" not in after_a


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

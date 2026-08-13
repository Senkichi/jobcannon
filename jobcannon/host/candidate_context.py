"""Host-side candidate-context resolver.

``resolve_candidate_context(conn, user_id)`` renders the non-empty
candidate-context string ``engine/job_scorer._build_system_prompt``
requires, from the tenant's ``profiles`` row (the only backing store —
``jobcannon.db._profiles`` remains the single reader/writer of that
table; this module goes through ``get_profile``).

Multi-tenancy invariant: resolution is keyed on ``user_id`` per call and
holds NO process-global state. A cache keyed on anything config- or
content-shaped would serve one user's career context to another user's
scoring call whenever two tenants share profile shape; if a cache is ever
added it must be ``user_id``-keyed and bounded.

``build_candidate_context(profile)`` is pure — Mapping in, string out, no
DB handle, no I/O — and returns a non-empty string for ANY input,
including an empty mapping ("Not specified" placeholders), because
``_build_system_prompt`` raises on empty context.

Not wired to any scoring call site: the engine's scoring entry point has
no hosted caller today; wiring belongs to the hosted scoring path, not
this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jobcannon.db._profiles import get_profile

_NOT_SPECIFIED = "Not specified"


class ProfileNotFoundError(LookupError):
    """No ``profiles`` row exists for the given ``user_id``."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        super().__init__(
            f"No profile row for user_id {user_id!r} — seed one via "
            "jobcannon.db._profiles.upsert_profile before scoring."
        )


def _get(profile: Mapping, key: str) -> Any:
    # Row mappings are contractually string-key subscriptable (dict_row and
    # the pooled hybrid_row); .get() is not part of that contract.
    try:
        return profile[key]
    except KeyError:
        return None


def _fmt_scalar(value: Any) -> str:
    if value is None:
        return _NOT_SPECIFIED
    text = str(value).strip()
    return text or _NOT_SPECIFIED


def _fmt_list(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(items) if items else _NOT_SPECIFIED
    return _fmt_scalar(value)


def _fmt_years(value: Any) -> str:
    # numeric column arrives as Decimal; render 8.0 as "8", 7.5 as "7.5".
    if value is None:
        return _NOT_SPECIFIED
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return _fmt_scalar(value)


def build_candidate_context(profile: Mapping) -> str:
    """Render a candidate-context string from a profiles-shaped mapping.

    Pure: accepts any Mapping (a DB row or a plain dict); missing keys and
    None values render as "Not specified". Non-empty by construction.
    """
    return "\n".join(
        [
            "Candidate profile:",
            f"- Seniority level: {_fmt_scalar(_get(profile, 'seniority_level'))}",
            f"- Years of experience: {_fmt_years(_get(profile, 'years_of_experience'))}",
            f"- Skills: {_fmt_list(_get(profile, 'skills'))}",
            f"- Target titles: {_fmt_list(_get(profile, 'target_titles'))}",
            f"- Target locations: {_fmt_list(_get(profile, 'target_locations'))}",
            f"- Experience summary: {_fmt_scalar(_get(profile, 'experience_summary'))}",
        ]
    )


def resolve_candidate_context(conn: Any, user_id: str) -> str:
    """Load ``user_id``'s profile row and render its candidate context.

    Raises :class:`ProfileNotFoundError` when no profiles row exists.
    """
    row = get_profile(conn, user_id)
    if row is None:
        raise ProfileNotFoundError(user_id)
    return build_candidate_context(row)

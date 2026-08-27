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
    """#28 item 3: unreachable via `upsert_profile` today (its `list |
    None` type hints and `Jsonb(...)` wrapping only ever produce a real
    list or None), but nothing stops a jsonb column from holding something
    else once a row is written any other way, so this validates at the
    boundary rather than trusting the shape:

    - a JSON `null` inside the array (`item is None`) used to survive
      `str(item).strip()` as the literal string "None" and render as if it
      were a real skill/title/location; now dropped like an empty string.
    - a jsonb object or nested array as a list ELEMENT would `str()` to a
      Python repr (e.g. "{'level': 'senior'}"); dropped the same way.
    - a jsonb object as the WHOLE column value (a "list-shaped" column
      that isn't actually list-shaped) used to fall through to
      `_fmt_scalar` and emit that same repr; now renders "Not specified"
      instead of a repr no model should see.
    """
    if isinstance(value, (list, tuple)):
        items = [
            str(item).strip()
            for item in value
            if item is not None and not isinstance(item, (dict, list, tuple)) and str(item).strip()
        ]
        return ", ".join(items) if items else _NOT_SPECIFIED
    if isinstance(value, dict):
        return _NOT_SPECIFIED
    return _fmt_scalar(value)


def _fmt_years(value: Any) -> str:
    # numeric column arrives as Decimal; render 8.0 as "8", 7.5 as "7.5".
    if value is None:
        return _NOT_SPECIFIED
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return _fmt_scalar(value)


def _fmt_comp_floor(value: Any) -> str:
    """#28 item 2: `comp_floor_usd` (integer, nullable — m0008) renders as a
    thousands-separated dollar figure (e.g. "$120,000") for comp_fit
    anchoring, or "Not specified" when the tenant hasn't set a floor. A
    tenant that hasn't told us their floor must never anchor comp_fit
    against a fabricated number (see m0008's migration docstring)."""
    if value is None:
        return _NOT_SPECIFIED
    try:
        return f"${int(value):,}"
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
            f"- Compensation floor: {_fmt_comp_floor(_get(profile, 'comp_floor_usd'))}",
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

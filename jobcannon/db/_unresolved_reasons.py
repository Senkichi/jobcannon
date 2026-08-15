"""Single point of enforcement for mutating ``postings.unresolved_reasons``.

Postgres port of the private repo's ``append_reason`` / ``remove_reasons``
pair. ``postings.unresolved_reasons`` is a ``jsonb NOT NULL DEFAULT '[]'``
array column (see the ``CREATE TABLE postings`` block in
``jobcannon/db/migrations/m0001_initial_schema.py``) with no CHECK constraint
enforcing array shape long-term. Unlike the private original, which stores
this column as SQLite TEXT and therefore serializes/deserializes JSON
strings, psycopg round-trips ``jsonb`` as a native Python value on read (see
``_jobs.py``'s ``existing["unresolved_reasons"] or []`` / ``Jsonb(...)`` on
write) — so these helpers take and return ``list[str]`` directly rather than
JSON text.

Exports:
    append_reason: Return unresolved_reasons with a reason code appended.
    remove_reasons: Return unresolved_reasons with reason codes removed.
"""

from __future__ import annotations


def append_reason(existing: list[str] | None, reason: str) -> list[str]:
    """Return *existing* with *reason* appended (deduped).

    Tolerant of a malformed/non-list value: falls back to treating the row
    as if it had no prior reasons rather than raising, since the column
    carries no shape guarantee beyond NOT NULL DEFAULT '[]'.
    """
    reasons = list(existing) if isinstance(existing, list) else []
    if reason not in reasons:
        reasons.append(reason)
    return reasons


def remove_reasons(existing: list[str] | None, reasons_to_remove: list[str]) -> list[str]:
    """Return *existing* with all *reasons_to_remove* removed.

    Tolerant of a malformed/non-list value: falls back to treating the row
    as if it had no prior reasons rather than raising. Removing a reason
    that is not present is a no-op.
    """
    reasons = list(existing) if isinstance(existing, list) else []
    to_remove = set(reasons_to_remove)
    return [r for r in reasons if r not in to_remove]

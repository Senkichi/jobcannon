"""Reference implementation for ``postings.unresolved_reasons`` semantics.

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

NOT the single point of enforcement (#217, PR #232): ``jobcannon/db/_jd_full.py``'s
``set_jd_full`` and ``_record_jd_content_reject`` mutate this column
themselves via atomic SQL expressions (jsonb set-difference / ``@>``
containment append) evaluated against the row's LIVE value at
UPDATE-execution time, rather than calling into these Python helpers —
a prior SELECT-then-call-these-helpers-then-UPDATE shape was a lost-update
window under a concurrent writer to the same column, which #217 closed by
moving the logic into SQL. These two functions remain the documented
semantic reference the SQL is written to match exactly (dedupe-on-append,
set-difference-on-remove, malformed/non-array-value tolerance) and are
exercised directly by their own test file
(``tests/host/test_unresolved_reasons.py``) as that spec, but no production
write path calls them — grep confirms zero remaining production callers.

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

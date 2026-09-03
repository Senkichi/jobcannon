# PORTED from job_finder/json_utils.py @ 8905c4b1177c51df5ac7630f0efdfac51e44a4a6 (private job-cannon). Ledger L-0006.
"""JSON deserialization utilities shared across persistence and web layers."""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    """Return the current UTC time as a naive ISO 8601 string.

    Produces timestamps like '2026-03-23T14:30:00' (no timezone suffix).
    All database timestamps should use this function so the codebase stores
    a consistent UTC baseline rather than mixing local time and UTC.
    """
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def to_naive_utc_iso(dt: datetime) -> str:
    """Serialize a datetime to the canonical naive-UTC ISO storage format.

    tz-aware input is converted to UTC and stripped; naive input is assumed
    to already be UTC (the store-UTC-render-local convention) and serialized
    as-is. Every datetime headed for a DB TEXT column should pass through
    here so aware values from source feeds (Greenhouse ``-04:00`` offsets,
    email ``Z`` suffixes) never leak tz suffixes into storage (#361).
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.isoformat()


def normalize_iso_string_to_naive_utc(value: str) -> str:
    """Normalize an ISO 8601 timestamp *string* to naive-UTC storage format.

    Companion to ``to_naive_utc_iso`` for write paths that already hold a
    stringified timestamp (e.g. a liveness-check ``checked_at`` value
    threaded through several callers) rather than a ``datetime`` object.
    tz-aware strings (trailing ``Z`` or an explicit ``+HH:MM`` / ``-HH:MM``
    offset) are parsed, converted to UTC, and re-serialized without the
    offset — the same guarantee ``to_naive_utc_iso`` gives datetime callers
    (#1226: a pre-#361 caller once fed an aware string into
    ``persist_job_expiry_state``, and since ``expiry_checked_at`` is copied
    into ``last_seen`` on a 'live' verdict, that leaked the aware suffix
    into a store-UTC-naive column).

    Naive strings pass through unchanged. Unparseable values also pass
    through unchanged — this is a write-boundary normalizer, not a
    validator, so a malformed string still reaches storage as-is for the
    caller to debug rather than silently vanishing.
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        # AttributeError: a None (or other non-str) slipping through must
        # follow the same pass-through contract, not crash the write path.
        return value
    if dt.tzinfo is None:
        return value
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat()


def local_today() -> str:
    """Return the user-local current calendar day as YYYY-MM-DD.

    Uses the same local-midnight basis as local_day_utc_window().
    Per the store-UTC-render-local convention, this is the single
    source of truth for local-day math — do NOT hand-roll elsewhere.
    """
    local_now = datetime.now().astimezone()
    return local_now.strftime("%Y-%m-%d")


def local_day_utc_window() -> tuple[str, str]:
    """Return (start_utc_iso, end_utc_iso) bounding the user-local current calendar day.

    Both bounds are naive ISO 8601 UTC strings (same format as timestamps
    written by utc_now_iso), suitable for ``WHERE timestamp >= ? AND
    timestamp < ?`` clauses in scoring_costs queries.

    Using local midnight rather than UTC midnight means "today's spend"
    and "today's quota" align with the user's clock, not UTC — so a
    budget cap set to $5/day resets at midnight the user sees, not 5 pm PT.
    """
    local_now = datetime.now().astimezone()  # aware, system timezone
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_tomorrow = local_midnight + timedelta(days=1)
    start_utc = local_midnight.astimezone(UTC).replace(tzinfo=None).isoformat()
    end_utc = local_tomorrow.astimezone(UTC).replace(tzinfo=None).isoformat()
    return start_utc, end_utc


def format_local_iso(value: str | None) -> str | None:
    """Convert a stored (naive-UTC, or tz-aware) ISO timestamp to a local-aware ISO string.

    Single source of truth for the *read* side of the store-UTC/render-local
    convention (``to_naive_utc_iso`` / ``normalize_iso_string_to_naive_utc``
    are the write side): a naive value is assumed to already be UTC — the
    storage convention every DB write and JSON marker in this codebase
    follows — and converted to this machine's local timezone; a tz-aware
    value converts directly. Promoted from a private duplicate that lived in
    ``healthcheck.py`` (#1982) once a second caller (``supervisor.py``'s
    doctor output, #1981) needed the identical conversion — matching this
    module's role as the existing canonical home for store-UTC/render-local
    helpers (``local_today``, ``local_day_utc_window``).

    Returns ``None`` when *value* is missing or unparseable, so callers can
    distinguish "no timestamp" from "timestamp at X" rather than emitting a
    misleading placeholder.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().isoformat()


def safe_json_load(value: str | None, default: Any = None) -> Any:
    """Safely deserialize a JSON string from a SQLite TEXT column.

    Returns default on None, empty string, non-string input, or
    JSONDecodeError/TypeError. The caller controls the default type
    ([] for arrays, {} for objects, None for optional fields).

    Args:
        value: Raw value from SQLite TEXT column. May be None, "", or
               a valid JSON string.
        default: Value to return when deserialization fails. Default is None.

    Returns:
        Deserialized Python object, or default on any failure.
    """
    if not value:
        return default
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        # PORT-SEAM: codeql py/clear-text-logging-sensitive-data -- this is a
        # generic shared helper (source_registry.py, data_enricher.py's
        # salary_observations reload, and any future caller); log the length
        # only, never the raw column content, since some caller's column may
        # hold data CodeQL (or a future reviewer) can't rule out as sensitive.
        logger.debug(
            "safe_json_load: failed to parse %d-char value, returning default",
            len(value),  # PORT-SEAM: codeql py/clear-text-logging-sensitive-data seam
        )
        return default

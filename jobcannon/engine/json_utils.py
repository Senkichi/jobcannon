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
        logger.debug(
            "safe_json_load: failed to parse %r, returning default",
            value[:80] if len(value) > 80 else value,
        )
        return default

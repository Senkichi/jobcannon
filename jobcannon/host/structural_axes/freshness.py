"""Freshness structural axis (0-1): live evidence outranks the clock.

A row already flagged stale/expired by the freshness detectors overrides age
entirely — evidence of staleness beats a fresh-looking date on the row (e.g.
a re-posted / recycled listing). Otherwise the age bucket is computed off
``posted_date`` when its precision is trustworthy ('exact'/'approximate'),
falling back to ``last_seen`` (proxy precision, or no posted_date at all).
With no usable date whatsoever, a flat 0.3 is returned rather than guessing.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def _coerce_datetime(value: object) -> datetime | None:
    """Best-effort parse of a date/datetime/ISO-string/None into an aware UTC datetime.

    Handles the shapes this axis actually sees: a `datetime.date` (the
    `posted_date` column), a `datetime.datetime` (the `last_seen` timestamptz
    column, always tz-aware as returned by psycopg), or an ISO-format string
    (defensive — some callers may pass serialized rows). Anything else, or a
    string that doesn't parse, yields None so the caller can fall back.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _age_bucket(age_days: float) -> float:
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.7
    if age_days <= 90:
        return 0.4
    return 0.2


def score_freshness(
    posted_date: object,
    posted_date_precision: str | None,
    last_seen: object,
    is_stale: bool,
    expiry_status: str | None,
) -> dict:
    if is_stale or expiry_status == "expired":
        return {"value": 0.1, "method": "rules_v1"}

    anchor = None
    if posted_date_precision in ("exact", "approximate"):
        anchor = _coerce_datetime(posted_date)
    if anchor is None:
        anchor = _coerce_datetime(last_seen)
    if anchor is None:
        return {"value": 0.3, "method": "rules_v1"}

    age_days = (datetime.now(timezone.utc) - anchor).total_seconds() / 86400
    return {"value": _age_bucket(age_days), "method": "rules_v1"}

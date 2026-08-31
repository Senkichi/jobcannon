"""format_salary — compact, honest salary line for the feed card's primary
tier (spec §1). Pure and DB-free, beside jobcannon/web/why.py and
apply_url.py; rendered as the precomputed `entry.salary_display` value
built once in jobcannon.web.feed_entries.build_entry (NOT a registered
Jinja filter — see the plan's deviation note 1).

Sentinel spellings are schema-derived and case-sensitive
(jobcannon/db/migrations/m0001_initial_schema.py): `salary_currency` is
NOT NULL with uppercase 'UNKNOWN' in its CHECK list; `salary_period` is
NOT NULL with lowercase 'unknown'. Currency renders '$' for USD, the bare
ISO code as prefix for any other known currency (no symbol table to
hand-maintain), and nothing for 'UNKNOWN'. psycopg returns the `numeric`
salary columns as Decimal — everything goes through Decimal so no float
artifacts can surface.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

_PERIOD_SUFFIX = {"annual": "/yr", "hourly": "/hr", "monthly": "/mo"}


def _compact_amount(value: Any) -> str:
    number = Decimal(str(value))
    if number >= 1000 and number % 100 == 0:
        # A multiple of 100 has at most one decimal digit in k-form.
        thousands = number / 1000
        if thousands == thousands.to_integral_value():
            return f"{int(thousands)}k"
        return f"{thousands:.1f}k"
    if number == number.to_integral_value():
        return f"{int(number):,}"
    return format(number.normalize(), "f")


def format_salary(row: Any) -> str | None:
    """Compact salary line for one posting row, or None when there is no
    salary data at all (the card then renders no salary line — never a
    placeholder). Requires `salary_min`, `salary_max`, `salary_currency`,
    and `salary_period` by string key — present in every postings
    projection this app renders (jobcannon/db/_feed.py's _SELECT_COLUMNS
    and the detail route's SELECT *)."""
    salary_min = row["salary_min"]
    salary_max = row["salary_max"]
    if salary_min is None and salary_max is None:
        return None

    currency = row["salary_currency"]
    if currency == "USD":
        prefix = "$"
    elif currency and currency != "UNKNOWN":
        prefix = f"{currency} "
    else:
        prefix = ""

    suffix = _PERIOD_SUFFIX.get(row["salary_period"], "")

    if salary_min is not None and salary_max is not None:
        if Decimal(str(salary_min)) == Decimal(str(salary_max)):
            core = _compact_amount(salary_min)
        else:
            core = f"{_compact_amount(salary_min)}–{_compact_amount(salary_max)}"
        return f"{prefix}{core}{suffix}"
    if salary_min is not None:
        return f"from {prefix}{_compact_amount(salary_min)}{suffix}"
    return f"up to {prefix}{_compact_amount(salary_max)}{suffix}"

"""format_salary (jobcannon/web/salary_fmt.py) — pure, DB-free compact salary
rendering for the card's primary tier (spec §1). Sentinels are case-sensitive
by schema (m0001): currency uppercase 'UNKNOWN', period lowercase 'unknown'.
No Postgres needed."""

from decimal import Decimal

import pytest

from jobcannon.web.salary_fmt import format_salary


def _row(**overrides):
    row = {
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "USD",
        "salary_period": "annual",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"salary_min": 150000, "salary_max": 200000}, "$150k–200k/yr"),
        ({"salary_min": 150000}, "from $150k/yr"),
        ({"salary_max": 200000}, "up to $200k/yr"),
        ({"salary_min": 150000, "salary_max": 150000}, "$150k/yr"),
        (
            {"salary_min": 80000, "salary_max": 100000, "salary_currency": "EUR"},
            "EUR 80k–100k/yr",
        ),
        (
            {"salary_min": 150000, "salary_max": 200000, "salary_currency": "UNKNOWN"},
            "150k–200k/yr",
        ),
        ({"salary_min": 150000, "salary_period": "unknown"}, "from $150k"),
        ({"salary_min": 60, "salary_max": 80, "salary_period": "hourly"}, "$60–80/hr"),
        (
            {"salary_min": Decimal("52.5"), "salary_period": "hourly"},
            "from $52.5/hr",
        ),
        ({"salary_min": 8000, "salary_max": 9500, "salary_period": "monthly"}, "$8k–9.5k/mo"),
        ({"salary_min": 147500}, "from $147.5k/yr"),
        ({"salary_min": 147550}, "from $147,550/yr"),
        (
            {"salary_min": Decimal("150000"), "salary_max": Decimal("200000")},
            "$150k–200k/yr",
        ),
    ],
)
def test_format_salary_cases(overrides, expected):
    assert format_salary(_row(**overrides)) == expected


def test_no_salary_data_returns_none():
    assert format_salary(_row()) is None


def test_lowercase_unknown_currency_is_not_the_sentinel():
    # The schema CHECK list makes this unrepresentable in the DB, but the
    # function must not treat the wrong-case spelling as the sentinel: it
    # renders as a literal currency prefix ("unknown "), not the empty
    # prefix UNKNOWN gets — same "from " min-only shape as every other
    # single-bound case in this file (e.g. "from $150k/yr").
    assert (
        format_salary(_row(salary_min=150000, salary_currency="unknown"))
        == "from unknown 150k/yr"
    )

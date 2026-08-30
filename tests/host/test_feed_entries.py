"""build_entry / dedupe_location (jobcannon/web/feed_entries.py) — pure,
DB-free. Task 8 extends this file with select_chips tests. No Postgres
needed."""

import pytest

from jobcannon.web.feed_entries import build_entry, dedupe_location


@pytest.mark.parametrize(
    ("location", "workplace_type", "expected"),
    [
        # Location only restates the workplace type -> drop it, keep badge.
        ("Remote", "REMOTE", (None, True)),
        ("remote", "REMOTE", (None, True)),
        # Location says MORE than the workplace type -> badge is redundant.
        ("Remote (US)", "REMOTE", ("Remote (US)", False)),
        # Independent facts -> show both.
        ("San Francisco, CA", "HYBRID", ("San Francisco, CA", True)),
        # No workplace type -> location as-is, no badge.
        ("Austin, TX", None, ("Austin, TX", False)),
        (None, None, (None, False)),
        # No location -> badge carries the fact alone.
        (None, "ONSITE", (None, True)),
        ("", "ONSITE", (None, True)),
    ],
)
def test_dedupe_location(location, workplace_type, expected):
    assert dedupe_location(location, workplace_type) == expected


def _row(**overrides):
    row = {
        "id": 7,
        "title": "Staff Engineer",
        "company": "Acme",
        "location": "Remote",
        "workplace_type": "REMOTE",
        "salary_min": 150000,
        "salary_max": 200000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "structural_axes": None,
        "posted_date_precision": None,
        "saved": None,
        "applied": None,
        "source_urls": ["https://jobs.example/7"],
        "sightings": [],
    }
    row.update(overrides)
    return row


def test_build_entry_carries_display_fields():
    entry = build_entry(_row(), {})
    assert entry["salary_display"] == "$150k–200k/yr"
    assert entry["display_location"] is None
    assert entry["show_workplace_badge"] is True
    assert entry["saved"] is False
    assert entry["applied"] is False
    assert entry["apply_url"] == "https://jobs.example/7"


def test_build_entry_no_salary_renders_none():
    entry = build_entry(_row(salary_min=None, salary_max=None), {})
    assert entry["salary_display"] is None

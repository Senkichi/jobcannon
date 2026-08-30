"""build_entry / dedupe_location (jobcannon/web/feed_entries.py) — pure,
DB-free. Task 8 extends this file with select_chips tests. No Postgres
needed."""

import pytest

from jobcannon.web.feed_entries import build_entry, dedupe_location, select_chips


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


def test_select_chips_priority_and_cap():
    kinds = {
        "jd_quality": "detailed job description",
        "seniority": "senior role",
        "freshness": "posted in the last week",
        "overlap": "title matches your selections: python",
    }
    chips = select_chips(kinds)
    assert [c["label"] for c in chips] == [
        "title matches your selections: python",
        "posted in the last week",
        "senior role",
    ]  # priority order regardless of dict order; jd_quality capped off
    assert [c["highlight"] for c in chips] == [True, False, False]


def test_select_chips_freshness_leads_when_no_overlap():
    chips = select_chips({"freshness": "posted in the last week", "seniority": "senior role"})
    assert chips[0] == {"label": "posted in the last week", "highlight": True}
    assert chips[1]["highlight"] is False


def test_select_chips_never_highlights_boilerplate_kinds():
    # Even when seniority/jd_quality lead, green stays off (spec §2).
    chips = select_chips({"seniority": "senior role", "jd_quality": "detailed job description"})
    assert [c["highlight"] for c in chips] == [False, False]


def test_select_chips_skips_none_and_handles_empty():
    assert select_chips({}) == []
    assert select_chips({"overlap": None, "freshness": "posted in the last week"}) == [
        {"label": "posted in the last week", "highlight": True}
    ]


def test_build_entry_chips_are_selected_dicts():
    row = _row(title="Senior Python Engineer")  # this file's existing row helper
    entry = build_entry(row, {"titles": ["Python Engineer"]})
    assert entry["chips"], "expected at least the overlap chip"
    for chip in entry["chips"]:
        assert set(chip) == {"label", "highlight"}
    assert sum(1 for chip in entry["chips"] if chip["highlight"]) <= 1
    assert entry["chips"][0]["label"].startswith("title matches")

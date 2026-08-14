"""pick_apply_url / apply_destination_for_row (jobcannon/web/apply_url.py) —
pure, DB-free. No Postgres needed: every input here is a plain dict standing
in for a list_feed_postings row, so this module runs with no
requires_postgres marker (matches tests/host/test_why.py)."""

from __future__ import annotations

from jobcannon.web.apply_url import apply_destination_for_row, pick_apply_url


def test_pick_apply_url_prefers_source_urls_over_sightings():
    row = {
        "source_urls": ["https://boards.greenhouse.io/acme/jobs/1"],
        "sightings": [{"source_url": "https://fallback.example.com/x"}],
    }
    assert pick_apply_url(row) == "https://boards.greenhouse.io/acme/jobs/1"


def test_pick_apply_url_falls_back_to_sightings_when_source_urls_empty():
    row = {
        "source_urls": [],
        "sightings": [{"source_url": "https://fallback.example.com/x"}],
    }
    assert pick_apply_url(row) == "https://fallback.example.com/x"


def test_pick_apply_url_returns_none_when_neither_yields_anything():
    assert pick_apply_url({"source_urls": [], "sightings": []}) is None
    assert pick_apply_url({}) is None


def test_apply_destination_strips_the_port():
    row = {"source_urls": ["https://boards.greenhouse.io:443/acme/jobs/1"]}
    assert apply_destination_for_row(row) == "boards.greenhouse.io"


def test_apply_destination_strips_userinfo():
    row = {"source_urls": ["https://user:pw@jobs.example.com/a?q=1"]}
    assert apply_destination_for_row(row) == "jobs.example.com"


def test_apply_destination_degrades_to_none_on_a_malformed_url():
    # urlsplit itself raises ValueError on an unparseable bracketed-IPv6 host.
    row = {"source_urls": ["https://[oops/x"]}
    assert apply_destination_for_row(row) is None


def test_apply_destination_is_bounded_to_the_payload_string_cap():
    from jobcannon.db.events_schema import _MAX_STR

    long_host = "a" * 250 + ".example.com"
    row = {"source_urls": [f"https://{long_host}/apply"]}
    destination = apply_destination_for_row(row)
    assert destination is not None
    assert len(destination) == _MAX_STR


def test_apply_destination_none_when_no_usable_url():
    assert apply_destination_for_row({"source_urls": [], "sightings": []}) is None

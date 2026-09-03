"""Tests for ``jobcannon.engine.location_parser`` and ``location_canonical``.

Implements the SPEC anchor corpus from
``.planning/SPEC-location-parsing.md`` (Test Plan / Unit section). Every
anchor string in that table MUST pass; failures here block the SPEC's
Commit A merge.

Layer 1 (scanner integration) and migration tests live in separate test
files added by Commits B and C.
"""

from __future__ import annotations

import json

import pytest

from jobcannon.engine.location_canonical import (
    JobLocation,
    dedupe_locations,
    from_json,
    to_json,
)
from jobcannon.engine.location_parser import parse_locations

# ─── Anchor corpus (SPEC must-pass) ──────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # (city, region_code, country_code, workplace_type, unresolved)
        # Tuples are (city, region_code, country_code, workplace_type, unresolved).
        # The SPEC's table is the source of truth.
        (
            "San Francisco, CA",
            [("San Francisco", "CA", "US", "UNSPECIFIED", False)],
        ),
        (
            "San Francisco, CA / Remote",
            [("San Francisco", "CA", "US", "REMOTE", False)],
        ),
        (
            "Remote - US",
            [(None, None, "US", "REMOTE", False)],
        ),
        (
            "Remote, EMEA",
            [(None, None, None, "REMOTE", True)],
        ),
        (
            "Multiple Locations",
            [],
        ),
        (
            "New York, NY; San Francisco, CA",
            [
                ("New York City", "NY", "US", "UNSPECIFIED", False),
                ("San Francisco", "CA", "US", "UNSPECIFIED", False),
            ],
        ),
        (
            "Bengaluru, KA, India",
            [("Bengaluru", "KA", "IN", "UNSPECIFIED", False)],
        ),
        (
            "London, UK or Remote",
            [
                ("London", None, "GB", "UNSPECIFIED", False),
                (None, None, None, "REMOTE", True),
            ],
        ),
        (
            "Hybrid - Toronto, ON, Canada",
            [("Toronto", "ON", "CA", "HYBRID", False)],
        ),
        (
            "",
            [],
        ),
    ],
)
def test_anchor_corpus(raw: str, expected: list[tuple]) -> None:
    """The SPEC's anchor corpus must pass byte-for-byte.

    Maintenance note: this list is the contract; do NOT update an entry's
    expected value to match a parser change without first updating
    .planning/SPEC-location-parsing.md.
    """
    locations = parse_locations(raw)
    got = [
        (loc.city, loc.region_code, loc.country_code, loc.workplace_type, loc.unresolved)
        for loc in locations
    ]
    assert got == expected, f"input={raw!r}"


@pytest.mark.parametrize("placeholder", ["TBD", "Unknown", "N/A", "tbd", "-", "various"])
def test_placeholder_inputs_drop(placeholder: str) -> None:
    """Pure placeholders short-circuit to empty list — no unresolved entry."""
    assert parse_locations(placeholder) == []


def test_none_input() -> None:
    assert parse_locations(None) == []


# ─── List-input form ─────────────────────────────────────────────────


def test_list_input_processes_each_entry() -> None:
    """List form (matches ``jobs.locations_raw`` JSON array) parses each entry."""
    locations = parse_locations(["San Francisco, CA", "Toronto, ON, Canada"])
    cities = [loc.city for loc in locations]
    assert cities == ["San Francisco", "Toronto"]


def test_list_input_with_placeholders_drops_them() -> None:
    locations = parse_locations(["San Francisco, CA", "TBD", "Multiple Locations"])
    assert len(locations) == 1
    assert locations[0].city == "San Francisco"


def test_list_input_empty() -> None:
    assert parse_locations([]) == []
    assert parse_locations([""]) == []


# ─── Country aliases ─────────────────────────────────────────────────


def test_uk_anchors_to_gb() -> None:
    """The UK→GB alias dodges pycountry's UK→Uganda mapping."""
    locations = parse_locations("Manchester, UK")
    assert len(locations) == 1
    assert locations[0].country_code == "GB"
    assert locations[0].city == "Manchester"


def test_usa_anchors_to_us() -> None:
    locations = parse_locations("Seattle, WA, USA")
    assert len(locations) == 1
    assert locations[0].country_code == "US"
    assert locations[0].region_code == "WA"


def test_emea_does_not_anchor() -> None:
    """EMEA is a region, not a country — must NOT silently map."""
    locations = parse_locations("Remote, EMEA")
    assert len(locations) == 1
    assert locations[0].country_code is None
    assert locations[0].unresolved is True
    assert locations[0].workplace_type == "REMOTE"


# ─── Ambiguous codes ─────────────────────────────────────────────────


def test_springfield_ambiguous_no_state_resolves_to_largest_us() -> None:
    """8 Springfields exist in US; SPEC Q1 picks the largest by population.

    SPEC Q1 resolution (2026-05-27): country-anchored fallback, then
    population-weighted within that country. Replaces the prior default
    (``city=None``) which discarded usable signal.

    Largest US Springfield is Springfield, MO (pop ~167k per geonamescache).
    """
    locations = parse_locations("Springfield, USA")
    assert len(locations) == 1
    assert locations[0].country_code == "US"
    assert locations[0].city == "Springfield"
    assert locations[0].region_code == "MO"


def test_springfield_with_state_resolves() -> None:
    """Adding a state scope picks the right Springfield."""
    locations = parse_locations("Springfield, IL")
    assert len(locations) == 1
    assert locations[0].country_code == "US"
    assert locations[0].region_code == "IL"
    assert locations[0].city == "Springfield"


def test_springfield_bare_no_anchors_us_default() -> None:
    """SPEC Q1: ``Springfield`` alone (no country signal) → US default.

    User-confirmed (2026-05-27): when the location string carries no
    country anchor at all, default to US since the corpus is
    US-dominant. Resolves to the largest US Springfield (Springfield, MO).
    """
    locations = parse_locations("Springfield")
    assert len(locations) == 1
    assert locations[0].country_code == "US"
    assert locations[0].city == "Springfield"
    assert locations[0].region_code == "MO"


def test_paris_bare_us_default_resolves_to_paris_tx() -> None:
    """SPEC Q1: bare ``Paris`` → Paris, TX (US default policy trade-off).

    Documented in the Q1 commit message — the US-default fallback can
    surprise on globally-famous names with small US namesakes. Mitigation
    is to supply a country anchor in the input string (``Paris, France``).
    """
    locations = parse_locations("Paris")
    assert len(locations) == 1
    assert locations[0].country_code == "US"
    assert locations[0].region_code == "TX"
    assert locations[0].city == "Paris"


def test_paris_with_country_anchor_resolves_to_paris_france() -> None:
    """When the input names the country, the US default does NOT apply."""
    locations = parse_locations("Paris, France")
    assert len(locations) == 1
    assert locations[0].country_code == "FR"
    assert locations[0].city == "Paris"


def test_ca_as_trailing_state_anchors_us() -> None:
    """'San Francisco, CA' must anchor country=US, region=CA — NOT Canada."""
    locations = parse_locations("San Francisco, CA")
    assert len(locations) == 1
    assert locations[0].country_code == "US"
    assert locations[0].region_code == "CA"


def test_ca_as_full_country_when_named() -> None:
    """When the country name is 'Canada', parse it as Canada, not US-CA."""
    locations = parse_locations("Toronto, Canada")
    assert len(locations) == 1
    assert locations[0].country_code == "CA"
    assert locations[0].city == "Toronto"


# ─── Workplace-type detection ────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected_workplace"),
    [
        ("San Francisco, CA / Remote", "REMOTE"),
        ("San Francisco, CA / Hybrid", "HYBRID"),
        ("San Francisco, CA / On-Site", "ONSITE"),
        ("San Francisco, CA / Onsite", "ONSITE"),
        ("San Francisco, CA / On site", "ONSITE"),
        ("Hybrid - Toronto, ON, Canada", "HYBRID"),
        ("Remote - DE", "REMOTE"),
    ],
)
def test_workplace_token_detection(raw: str, expected_workplace: str) -> None:
    locations = parse_locations(raw)
    assert len(locations) == 1
    assert locations[0].workplace_type == expected_workplace


def test_trailing_slash_workplace_promotes_to_single_entry() -> None:
    """The trailing '/ Remote' form collapses to one row, not two.

    Distinct from the 'or Remote' form (test_or_remote_keeps_two_entries
    below), which keeps the entries separate.
    """
    locations = parse_locations("San Francisco, CA / Remote")
    assert len(locations) == 1
    assert locations[0].city == "San Francisco"
    assert locations[0].workplace_type == "REMOTE"


def test_or_remote_keeps_two_entries() -> None:
    """The 'or Remote' alternative separator keeps both entries distinct."""
    locations = parse_locations("London, UK or Remote")
    assert len(locations) == 2
    assert locations[0].workplace_type == "UNSPECIFIED"
    assert locations[1].workplace_type == "REMOTE"
    assert locations[1].unresolved is True


# ─── Multi-location handling ─────────────────────────────────────────


def test_semicolon_separator_splits() -> None:
    locations = parse_locations("New York, NY; San Francisco, CA")
    assert len(locations) == 2


def test_pipe_separator_splits() -> None:
    locations = parse_locations("New York, NY | San Francisco, CA")
    assert len(locations) == 2


def test_dedup_collapses_identical_entries() -> None:
    """Same (country, region, city, workplace) → single output."""
    locations = parse_locations("San Francisco, CA; San Francisco, CA")
    assert len(locations) == 1


# ─── Unresolved fallback behavior ────────────────────────────────────


def test_garbage_input_yields_unresolved_with_raw() -> None:
    """Total parse failure preserves the raw string for display fallback."""
    locations = parse_locations("zzqqzz, foobarbaz")
    assert len(locations) == 1
    assert locations[0].unresolved is True
    assert "zzqqzz" in locations[0].raw


def test_parser_never_raises() -> None:
    """Unicode, mojibake, and odd punctuation never crash the parser."""
    for weird in [
        "São Paulo, Brazil",
        "Москва, RU",
        "?!@#$%^&*()",
        "  ,  ,  ,  ",
        "Remote / Remote / Remote",
    ]:
        parse_locations(weird)


# ─── JobLocation dataclass invariants ────────────────────────────────


def test_joblocation_is_frozen() -> None:
    loc = JobLocation(
        city="SF",
        region=None,
        region_code=None,
        country=None,
        country_code=None,
        workplace_type="UNSPECIFIED",
        raw="SF",
        unresolved=False,
    )
    with pytest.raises((AttributeError, TypeError)):
        loc.city = "Other"  # type: ignore[misc]


def test_joblocation_rejects_invalid_workplace_type() -> None:
    with pytest.raises(ValueError, match="invalid workplace_type"):
        JobLocation(
            city=None,
            region=None,
            region_code=None,
            country=None,
            country_code=None,
            workplace_type="bogus",  # type: ignore[arg-type]
            raw="",
            unresolved=True,
        )


def test_unresolved_from_raw_helper() -> None:
    loc = JobLocation.unresolved_from_raw("weird input", workplace_type="HYBRID")
    assert loc.unresolved is True
    assert loc.raw == "weird input"
    assert loc.workplace_type == "HYBRID"
    assert loc.city is None
    assert loc.country_code is None


# ─── JSON round-trip ─────────────────────────────────────────────────


def test_to_json_empty() -> None:
    assert to_json([]) == "[]"


def test_to_json_single_location_round_trips() -> None:
    original = parse_locations("San Francisco, CA")
    payload = to_json(original)
    restored = from_json(payload)
    assert restored == original


def test_to_json_multi_location_round_trips() -> None:
    original = parse_locations("New York, NY; San Francisco, CA")
    payload = to_json(original)
    # The serialized form must be valid JSON.
    parsed = json.loads(payload)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    restored = from_json(payload)
    assert restored == original


def test_from_json_tolerates_none_and_empty() -> None:
    assert from_json(None) == []
    assert from_json("") == []
    assert from_json("[]") == []


def test_from_json_tolerates_unknown_fields() -> None:
    """Forward-compat: extra fields silently dropped, not rejected."""
    payload = json.dumps(
        [
            {
                "city": "SF",
                "region": None,
                "region_code": None,
                "country": None,
                "country_code": None,
                "workplace_type": "UNSPECIFIED",
                "raw": "SF",
                "unresolved": False,
                "future_field": "this should be ignored",
            }
        ]
    )
    restored = from_json(payload)
    assert len(restored) == 1
    assert restored[0].city == "SF"


def test_from_json_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="expected JSON array"):
        from_json('{"not": "a list"}')


# ─── Dedupe helper ──────────────────────────────────────────────────


def test_dedupe_preserves_first_seen_order() -> None:
    locations = [
        JobLocation(
            city="SF",
            region=None,
            region_code="CA",
            country=None,
            country_code="US",
            workplace_type="UNSPECIFIED",
            raw="A",
            unresolved=False,
        ),
        JobLocation(
            city="SF",
            region=None,
            region_code="CA",
            country=None,
            country_code="US",
            workplace_type="UNSPECIFIED",
            raw="B",  # different raw, same key
            unresolved=False,
        ),
        JobLocation(
            city="NY",
            region=None,
            region_code="NY",
            country=None,
            country_code="US",
            workplace_type="UNSPECIFIED",
            raw="C",
            unresolved=False,
        ),
    ]
    out = dedupe_locations(locations)
    assert len(out) == 2
    assert out[0].raw == "A"
    assert out[1].city == "NY"


def test_dedupe_workplace_distinguishes() -> None:
    """Same city different workplace_type → two distinct entries."""
    locations = [
        JobLocation(
            city="SF",
            region=None,
            region_code="CA",
            country=None,
            country_code="US",
            workplace_type="REMOTE",
            raw="A",
            unresolved=False,
        ),
        JobLocation(
            city="SF",
            region=None,
            region_code="CA",
            country=None,
            country_code="US",
            workplace_type="HYBRID",
            raw="B",
            unresolved=False,
        ),
    ]
    assert len(dedupe_locations(locations)) == 2


# ─── SPEC Q3: jd_full body hashtag fallback ──────────────────────────


def test_jd_full_li_remote_promotes_unspecified() -> None:
    """``#LI-Remote`` in JD body promotes an UNSPECIFIED location's workplace."""
    locations = parse_locations(
        "Toronto, ON",
        jd_full="About the role: senior engineer. #LI-Remote tag at bottom.",
    )
    assert len(locations) == 1
    assert locations[0].city == "Toronto"
    assert locations[0].country_code == "CA"
    assert locations[0].workplace_type == "REMOTE"


def test_jd_full_li_hybrid_promotes_unspecified() -> None:
    """``#LI-Hybrid`` promotes UNSPECIFIED → HYBRID."""
    locations = parse_locations(
        "London, UK",
        jd_full="Description. #LI-Hybrid",
    )
    assert len(locations) == 1
    assert locations[0].country_code == "GB"
    assert locations[0].workplace_type == "HYBRID"


def test_jd_full_li_onsite_promotes_unspecified() -> None:
    """``#LI-Onsite`` promotes UNSPECIFIED → ONSITE."""
    locations = parse_locations(
        "Berlin, Germany",
        jd_full="Body content. #LI-Onsite",
    )
    assert len(locations) == 1
    assert locations[0].country_code == "DE"
    assert locations[0].workplace_type == "ONSITE"


def test_raw_workplace_token_wins_over_body_tag() -> None:
    """An explicit token in ``raw`` outranks any body-tag signal.

    Precedence: per-segment token > trailing-slash promotion > body tag.
    """
    locations = parse_locations(
        "Remote, US",
        jd_full="#LI-Hybrid scattered in body should NOT override REMOTE.",
    )
    assert len(locations) == 1
    assert locations[0].workplace_type == "REMOTE"


def test_jd_full_none_no_promotion() -> None:
    """``jd_full=None`` leaves UNSPECIFIED entries untouched."""
    locations = parse_locations("Toronto, ON")
    assert len(locations) == 1
    assert locations[0].workplace_type == "UNSPECIFIED"


def test_jd_full_no_tag_no_promotion() -> None:
    """Generic prose without LI-hashtags does NOT promote workplace_type.

    The bare word ``remote`` in body prose (e.g. "remote possibility")
    is a known false-positive surface; only the ``#LI-*`` forms are
    matched.
    """
    locations = parse_locations(
        "Paris, France",
        jd_full=(
            "We have a remote possibility for hybrid working. Generic prose "
            "with bare workplace words should NOT change the workplace_type."
        ),
    )
    assert len(locations) == 1
    assert locations[0].workplace_type == "UNSPECIFIED"


def test_jd_full_precedence_remote_over_hybrid() -> None:
    """Body containing both ``#LI-Remote`` and ``#LI-Hybrid`` resolves REMOTE."""
    locations = parse_locations(
        "Sydney, Australia",
        jd_full="#LI-Hybrid earlier, #LI-Remote later. REMOTE wins.",
    )
    assert len(locations) == 1
    assert locations[0].workplace_type == "REMOTE"


def test_jd_full_promotes_all_unspecified_entries() -> None:
    """Multi-location: every UNSPECIFIED entry gets promoted, resolved keep theirs."""
    locations = parse_locations(
        "New York, NY; San Francisco, CA",
        jd_full="#LI-Hybrid",
    )
    assert len(locations) == 2
    assert all(loc.workplace_type == "HYBRID" for loc in locations)


def test_jd_full_empty_raw_returns_empty() -> None:
    """Body tag alone (no raw location) → still ``[]``.

    Per SPEC Q3, the body tag is a workplace_type fallback for *known*
    locations — it does NOT create entries out of thin air.
    """
    assert parse_locations(None, jd_full="#LI-Remote") == []
    assert parse_locations("", jd_full="#LI-Remote") == []


def test_jd_full_with_hash_li_space_form() -> None:
    """``#LI Remote`` (space variant) also detected — matches raw-token forms."""
    locations = parse_locations(
        "Paris, France",
        jd_full="#LI Remote variation",
    )
    assert len(locations) == 1
    assert locations[0].workplace_type == "REMOTE"


def test_jd_full_does_not_match_in_middle_of_word() -> None:
    """``#LI-Remoteness`` should NOT trigger — ``\\b`` after Remote required."""
    locations = parse_locations(
        "Paris, France",
        jd_full="#LI-Remoteness is not a real tag",
    )
    assert len(locations) == 1
    assert locations[0].workplace_type == "UNSPECIFIED"

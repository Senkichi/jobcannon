"""Tests for issue #336's sender-override dedup — ``_resolve_overrides`` and
the two-phase resolution contract it owns.

``resolve_sender_parsers`` / ``resolve_sender_label`` previously each ran the
identical ``_OVERRIDABLE_SENDERS`` scan inline; the scan is now shared behind
the private ``_resolve_overrides(base_map, config)`` helper. The first group
pins the helper's filtering/ordering contract directly; the second pins the
apply-phase edge cases only the public resolvers can express (override
addresses colliding with other senders' defaults), which is where two-phase
resolution could have diverged from the old mid-loop check.

Not PORTED — the dedup is an engine-side follow-up added after the port
(issue #336); nothing upstream corresponds to it.
"""

import importlib.util

import pytest

from jobcannon.engine import email_senders
from jobcannon.engine.email_parsers.glassdoor_parser import parse_glassdoor_alert
from jobcannon.engine.email_parsers.indeed_parser import parse_indeed_alert
from jobcannon.engine.email_parsers.linkedin_parser import parse_linkedin_alert
from jobcannon.engine.email_senders import (
    SENDER_LABEL,
    SENDER_PARSERS,
    resolve_sender_label,
    resolve_sender_parsers,
)

# --- _resolve_overrides: shared resolution contract ---


def test_resolve_overrides_returns_default_to_override_map():
    overrides = email_senders._resolve_overrides(
        dict(SENDER_PARSERS),
        {"sources": {"imap": {"senders": {"indeed": "i@override.example"}}}},
    )
    assert overrides == {"alert@indeed.com": "i@override.example"}


def test_resolve_overrides_empty_without_config_or_senders():
    base = dict(SENDER_PARSERS)
    assert email_senders._resolve_overrides(base, None) == {}
    assert email_senders._resolve_overrides(base, {}) == {}
    assert email_senders._resolve_overrides(base, {"sources": {"imap": {"enabled": True}}}) == {}


def test_resolve_overrides_drops_invalid_entries():
    config = {
        "sources": {
            "imap": {
                "senders": {
                    "indeed": "i@override.example",  # the one valid entry
                    "glassdoor": "",  # blank
                    "ziprecruiter": "   ",  # whitespace-only
                    "jobright": None,  # non-string
                    "linkedin_jobs": "jobs-noreply@linkedin.com",  # equal to default
                }
            }
        }
    }
    assert email_senders._resolve_overrides(dict(SENDER_PARSERS), config) == {
        "alert@indeed.com": "i@override.example"
    }


def test_resolve_overrides_ignores_unknown_and_non_overridable_keys():
    config = {
        "sources": {
            "imap": {
                "senders": {
                    "not_a_sender": "x@y.example",  # unknown form key
                    "monster": "m@x.example",  # real sender, no override key
                    "greenhouse": "g@x.example",  # real sender, no override key
                }
            }
        }
    }
    assert email_senders._resolve_overrides(dict(SENDER_PARSERS), config) == {}


def test_resolve_overrides_skips_default_absent_from_base_map():
    # ``default in base_map`` is the helper's guarantee that the caller's
    # apply step (pop / index on the same map) can never KeyError.
    base = {k: v for k, v in SENDER_PARSERS.items() if k != "alert@indeed.com"}
    overrides = email_senders._resolve_overrides(
        base, {"sources": {"imap": {"senders": {"indeed": "i@x.example"}}}}
    )
    assert overrides == {}


def test_resolve_overrides_preserves_overridable_sender_order():
    # Apply order is observable — the label resolver reads ``labels[default]``
    # mid-apply — so the returned map must iterate in _OVERRIDABLE_SENDERS
    # (i.e. SENDERS) order, not config order.
    config = {
        "sources": {
            "imap": {
                "senders": {
                    "glassdoor": "g@x.example",
                    "linkedin_alerts": "li@x.example",
                    "indeed": "i@x.example",
                }
            }
        }
    }
    overrides = email_senders._resolve_overrides(dict(SENDER_PARSERS), config)
    assert list(overrides) == [
        "jobalerts-noreply@linkedin.com",
        "noreply@glassdoor.com",
        "alert@indeed.com",
    ]


def test_resolve_overrides_does_not_mutate_base_map():
    base = dict(SENDER_PARSERS)
    snapshot = dict(base)
    email_senders._resolve_overrides(
        base, {"sources": {"imap": {"senders": {"indeed": "i@x.example"}}}}
    )
    assert base == snapshot


@pytest.mark.parametrize(
    ("config", "type_name"),
    [
        ({"sources": {"imap": {"senders": ["not", "a", "dict"]}}}, "list"),
        ({"sources": {"imap": "not-a-dict"}}, "str"),
        ({"sources": "not-a-dict"}, "str"),
    ],
)
def test_resolve_overrides_malformed_config_shape_raises_attribute_error(config, type_name):
    # Malformed intermediate nodes raise AttributeError off the .get chain —
    # identical propagation to the pre-dedup inline scan. The match= pins the
    # error to that chain, not just any AttributeError.
    with pytest.raises(AttributeError, match=rf"'{type_name}' object has no attribute 'get'"):
        email_senders._resolve_overrides(dict(SENDER_PARSERS), config)


# --- Apply-phase contracts through the public resolvers ---
# Equivalence pins: these outcomes are unchanged from the pre-dedup inline
# scan (verified by the differential run in the PR body). They exist so a
# future refactor of the shared helper cannot silently drift the semantics.


def test_multiple_overrides_apply_to_both_maps():
    config = {
        "sources": {
            "imap": {
                "senders": {
                    "glassdoor": "g@override.example",
                    "indeed": "i@override.example",
                }
            }
        }
    }
    parsers = resolve_sender_parsers(config)
    labels = resolve_sender_label(config)

    assert parsers["g@override.example"] is parse_glassdoor_alert
    assert parsers["i@override.example"] is parse_indeed_alert
    assert "noreply@glassdoor.com" not in parsers
    assert "alert@indeed.com" not in parsers
    assert len(parsers) == len(SENDER_PARSERS)
    assert labels["g@override.example"] == "glassdoor"
    assert labels["i@override.example"] == "indeed"


def test_override_colliding_with_another_default_steals_the_address():
    # Degenerate config: glassdoor overridden to linkedin_alerts' default
    # address. The rename is last-write-wins on the shared key — the address
    # ends up routing to the glassdoor parser and glassdoor's own default
    # leaves the map entirely.
    config = {"sources": {"imap": {"senders": {"glassdoor": "jobalerts-noreply@linkedin.com"}}}}
    parsers = resolve_sender_parsers(config)
    labels = resolve_sender_label(config)

    assert parsers["jobalerts-noreply@linkedin.com"] is parse_glassdoor_alert
    assert "noreply@glassdoor.com" not in parsers
    assert len(parsers) == len(SENDER_PARSERS) - 1
    assert labels["jobalerts-noreply@linkedin.com"] == "glassdoor"
    assert labels["noreply@glassdoor.com"] == "glassdoor"
    assert len(labels) == len(SENDER_LABEL)


def test_two_way_override_swap_applies_in_sender_order():
    # Degenerate config: each default overridden to the other's address.
    # linkedin_alerts precedes glassdoor in _OVERRIDABLE_SENDERS, so its pair
    # applies first; glassdoor's apply then pops the just-placed value — the
    # linkedin default keeps its own parser and the glassdoor default address
    # ends up unrouted.
    config = {
        "sources": {
            "imap": {
                "senders": {
                    "glassdoor": "jobalerts-noreply@linkedin.com",
                    "linkedin_alerts": "noreply@glassdoor.com",
                }
            }
        }
    }
    parsers = resolve_sender_parsers(config)
    labels = resolve_sender_label(config)

    assert parsers["jobalerts-noreply@linkedin.com"] is parse_linkedin_alert
    assert "noreply@glassdoor.com" not in parsers
    assert labels["jobalerts-noreply@linkedin.com"] == "linkedin"
    assert labels["noreply@glassdoor.com"] == "linkedin"


def test_non_dict_senders_raises_attribute_error_through_public_resolvers():
    config = {"sources": {"imap": {"senders": ["not", "a", "dict"]}}}
    with pytest.raises(AttributeError):
        resolve_sender_parsers(config)
    with pytest.raises(AttributeError):
        resolve_sender_label(config)


# --- Vestigial engine.sources package stays deleted (issue #336) ---


def test_engine_sources_package_is_not_a_regular_package():
    # jobcannon/engine/sources/__init__.py was an empty L-0042 ledger landing
    # — zero members, zero imports. The port's flat layout keeps email_senders
    # and _pii_scrub as flat modules under jobcannon.engine, so no regular
    # package may reappear (a namespace leftover with no __init__ is fine).
    spec = importlib.util.find_spec("jobcannon.engine.sources")
    assert spec is None or spec.origin is None

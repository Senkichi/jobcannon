# PORTED from tests/test_gmail_sender_overrides.py @ 15657405ba5ed1eda80b6c1d35cbb5dbf51fb2e2 (private job-cannon). Ledger L-0552.
"""Tests for sources.gmail.senders override wiring (audit finding #5).

The Settings page saves ``sources.gmail.senders`` (sender_key → FROM address),
but ingestion historically read only the hardcoded SENDER_PARSERS dict, so the
control was dead. ``resolve_sender_parsers`` / ``resolve_sender_label`` thread
the override into the live address→parser and address→label maps.

The load-bearing invariant: the no-override path (None / {} / no senders) must
return maps equal to the module defaults — byte-for-byte identical behaviour.
"""

from jobcannon.engine.email_parsers.glassdoor_parser import parse_glassdoor_alert
from jobcannon.engine.email_senders import (
    SENDER_LABEL,
    SENDER_PARSERS,
    resolve_sender_label,
    resolve_sender_parsers,
)

# --- Safety invariant: no-override path is identical to the defaults ---


def test_resolve_parsers_none_equals_default():
    assert resolve_sender_parsers(None) == SENDER_PARSERS


def test_resolve_parsers_empty_equals_default():
    assert resolve_sender_parsers({}) == SENDER_PARSERS


def test_resolve_parsers_no_senders_key_equals_default():
    config = {"sources": {"imap": {"enabled": True, "lookback_days": 7}}}  # PORT-SEAM: gmail->imap
    assert resolve_sender_parsers(config) == SENDER_PARSERS


def test_resolve_labels_none_equals_default():
    assert resolve_sender_label(None) == SENDER_LABEL


def test_resolve_labels_empty_equals_default():
    assert resolve_sender_label({}) == SENDER_LABEL


def test_resolve_labels_no_senders_key_equals_default():
    config = {"sources": {"imap": {"enabled": True}}}  # PORT-SEAM: gmail->imap
    assert resolve_sender_label(config) == SENDER_LABEL


def test_resolve_returns_a_copy_not_the_module_dict():
    # Mutating the resolved map must not corrupt the module-level constant.
    parsers = resolve_sender_parsers(None)
    parsers["x@example.com"] = lambda *a, **k: []
    assert "x@example.com" not in SENDER_PARSERS

    labels = resolve_sender_label(None)
    labels["x@example.com"] = "x"
    assert "x@example.com" not in SENDER_LABEL


# --- Override swaps the address in both maps ---


def test_override_swaps_parser_address():
    config = {
        "sources": {"imap": {"senders": {"glassdoor": "alerts@glassdoor.example"}}}
    }  # PORT-SEAM: gmail->imap
    parsers = resolve_sender_parsers(config)

    # Default address is gone; new address now routes to the glassdoor parser.
    assert "noreply@glassdoor.com" not in parsers
    assert parsers["alerts@glassdoor.example"] is parse_glassdoor_alert

    # Non-overridden senders are untouched.
    assert parsers["alert@indeed.com"] is SENDER_PARSERS["alert@indeed.com"]
    # Same total count — a swap (pop + add), not a net addition.
    assert len(parsers) == len(SENDER_PARSERS)


def test_override_maps_new_address_to_canonical_label():
    config = {
        "sources": {"imap": {"senders": {"glassdoor": "alerts@glassdoor.example"}}}
    }  # PORT-SEAM: gmail->imap
    labels = resolve_sender_label(config)

    # New address resolves to the canonical label; default entry is kept too
    # (so autoheal recipes keyed on the canonical label keep working).
    assert labels["alerts@glassdoor.example"] == "glassdoor"
    assert labels["noreply@glassdoor.com"] == "glassdoor"


def test_override_of_linkedin_alerts_key():
    # Both linkedin keys are independently overridable; verify the alerts one.
    config = {
        "sources": {"imap": {"senders": {"linkedin_alerts": "li@override.example"}}}
    }  # PORT-SEAM: gmail->imap
    parsers = resolve_sender_parsers(config)
    labels = resolve_sender_label(config)

    assert "jobalerts-noreply@linkedin.com" not in parsers
    assert "li@override.example" in parsers
    # The other linkedin address (jobs) is left alone.
    assert "jobs-noreply@linkedin.com" in parsers
    assert labels["li@override.example"] == "linkedin"


# --- Blank / whitespace / no-op overrides are ignored ---


def test_blank_override_is_ignored():
    config = {"sources": {"imap": {"senders": {"glassdoor": ""}}}}  # PORT-SEAM: gmail->imap
    assert resolve_sender_parsers(config) == SENDER_PARSERS
    assert resolve_sender_label(config) == SENDER_LABEL


def test_whitespace_override_is_ignored():
    config = {"sources": {"imap": {"senders": {"glassdoor": "   "}}}}  # PORT-SEAM: gmail->imap
    assert resolve_sender_parsers(config) == SENDER_PARSERS
    assert resolve_sender_label(config) == SENDER_LABEL


def test_override_equal_to_default_is_a_noop():
    # Re-saving the default address in Settings must not alter the maps.
    config = {
        "sources": {"imap": {"senders": {"glassdoor": "noreply@glassdoor.com"}}}
    }  # PORT-SEAM: gmail->imap
    assert resolve_sender_parsers(config) == SENDER_PARSERS
    assert resolve_sender_label(config) == SENDER_LABEL


def test_non_string_override_is_ignored():
    config = {"sources": {"imap": {"senders": {"glassdoor": None}}}}  # PORT-SEAM: gmail->imap
    assert resolve_sender_parsers(config) == SENDER_PARSERS
    assert resolve_sender_label(config) == SENDER_LABEL

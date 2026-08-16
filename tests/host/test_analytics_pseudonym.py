"""Unit tests for jobcannon.host.posthog_client.pseudonymize/set_analytics_salt
in isolation from log_event's consent/Postgres-write machinery (that
integration lives in tests/host/test_events.py).

No Postgres needed. tests/host/conftest.py's autouse
_reset_analytics_pseudonym_salt fixture resets the salt to None (unconfigured)
after every test in this directory, so each test here starts from that same
fail-closed baseline and sets whatever salt it needs explicitly.
"""

from __future__ import annotations

from jobcannon.host import posthog_client


def test_pseudonymize_returns_none_without_a_salt_configured():
    posthog_client.set_analytics_salt(None)

    assert posthog_client.pseudonymize("user_1") is None


def test_pseudonymize_same_user_same_salt_same_pseudonym():
    posthog_client.set_analytics_salt("salt-a")

    first = posthog_client.pseudonymize("user_1")
    second = posthog_client.pseudonymize("user_1")

    assert first is not None
    assert first == second


def test_pseudonymize_different_users_different_pseudonym():
    posthog_client.set_analytics_salt("salt-a")

    assert posthog_client.pseudonymize("user_1") != posthog_client.pseudonymize("user_2")


def test_pseudonymize_different_salt_different_pseudonym_for_same_user():
    """A salt rotation changes every pseudonym — expected (and acceptable:
    the doc'd trade-off is analytics continuity WITHIN one salt's lifetime,
    not across a deliberate rotation)."""
    posthog_client.set_analytics_salt("salt-a")
    under_salt_a = posthog_client.pseudonymize("user_1")

    posthog_client.set_analytics_salt("salt-b")
    under_salt_b = posthog_client.pseudonymize("user_1")

    assert under_salt_a != under_salt_b


def test_pseudonymize_never_returns_the_raw_user_id():
    posthog_client.set_analytics_salt("salt-a")

    assert posthog_client.pseudonymize("user_1") != "user_1"


def test_pseudonymize_not_reversible_without_the_salt():
    """Not a cryptographic proof (that's HMAC-SHA256's job) — pins that the
    pseudonym is not simply a bare hash of the id an attacker could
    dictionary-attack without knowing the salt: the SAME id under two
    DIFFERENT salts produces unrelated output, so learning one pseudonym
    reveals nothing about the salt or the id without also knowing the salt."""
    posthog_client.set_analytics_salt("salt-a")
    pseudonym = posthog_client.pseudonymize("user_1")

    posthog_client.set_analytics_salt("a-completely-different-salt")
    assert posthog_client.pseudonymize("user_1") != pseudonym


def test_set_analytics_salt_blank_string_normalizes_to_none():
    """Same absent-not-empty semantics as HostConfig's env reads: a blank
    salt must fail closed exactly like an unset one, not become a
    truthy-but-useless empty-string HMAC key."""
    posthog_client.set_analytics_salt("")

    assert posthog_client.pseudonymize("user_1") is None

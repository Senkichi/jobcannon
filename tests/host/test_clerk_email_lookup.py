"""jobcannon/web/auth.py's `fetch_primary_email` (issue #181) — the
Clerk-Backend-API call jobcannon/web/export.py's identity section is built
from.

No Flask app, no DB: `fetch_primary_email` takes a plain client object and
a user id, so these are pure unit tests against fake `users.get(...)`
doubles — the same "mock at the HTTP boundary" shape
tests/host/test_account_route.py already uses for the sibling Clerk
user-delete call (a fake `.users` object standing in for the real SDK's
`Users` resource), never a mock of `fetch_primary_email` itself. Every fake
user/address/verification below is a bare `types.SimpleNamespace` rather
than a real `clerk_backend_api.models.User` — `fetch_primary_email` reads
them via `getattr`, never `isinstance`, precisely so real SDK model
construction is never required here (jobcannon/web/auth.py's module
docstring: "the SDK is never imported in tests").
"""

from __future__ import annotations

from types import SimpleNamespace

from jobcannon.web.auth import ClerkEmailLookup, fetch_primary_email

USER_ID = "user_email_lookup_1"


def _user(primary_id, addresses):
    return SimpleNamespace(primary_email_address_id=primary_id, email_addresses=addresses)


def _address(id, email_address, status):
    verification = SimpleNamespace(status=status) if status is not None else None
    return SimpleNamespace(id=id, email_address=email_address, verification=verification)


class _FakeUsers:
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error
        self.calls: list[dict] = []

    def get(self, *, user_id, timeout_ms=None, retries=None):
        self.calls.append({"user_id": user_id, "timeout_ms": timeout_ms, "retries": retries})
        if self._error is not None:
            raise self._error
        return self._result


class _FakeClerkClient:
    def __init__(self, users):
        self.users = users


class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"simulated {status_code}")
        self.status_code = status_code


class _TimeoutError(Exception):
    """Named to mirror httpx's *Timeout* exception family — `auth.py`'s
    `_clerk_failure_reason` classifies purely off the exception's class
    name for the no-status-code case, so the name itself is the fixture."""


def test_returns_primary_email_and_verified_true():
    client = _FakeClerkClient(
        _FakeUsers(
            result=_user(
                "addr_1",
                [
                    _address("addr_0", "old@example.com", "verified"),
                    _address("addr_1", "primary@example.com", "verified"),
                ],
            )
        )
    )

    result = fetch_primary_email(client, USER_ID)

    assert result == ClerkEmailLookup("primary@example.com", True, None)


def test_returns_verified_false_for_unverified_primary_address():
    client = _FakeClerkClient(
        _FakeUsers(
            result=_user("addr_1", [_address("addr_1", "unverified@example.com", "unverified")])
        )
    )

    result = fetch_primary_email(client, USER_ID)

    assert result == ClerkEmailLookup("unverified@example.com", False, None)


def test_calls_users_get_with_bounded_timeout_and_no_retries():
    users = _FakeUsers(result=_user("addr_1", [_address("addr_1", "a@example.com", "verified")]))
    client = _FakeClerkClient(users)

    fetch_primary_email(client, USER_ID, timeout_ms=5000)

    assert users.calls == [{"user_id": USER_ID, "timeout_ms": 5000, "retries": None}]


def test_no_clerk_client_configured_is_fail_soft():
    result = fetch_primary_email(None, USER_ID)

    assert result.email is None
    assert result.email_verified is None
    assert result.unavailable_reason == "clerk_client_unavailable"


def test_timeout_is_fail_soft_not_raised():
    client = _FakeClerkClient(_FakeUsers(error=_TimeoutError("read timed out")))

    result = fetch_primary_email(client, USER_ID)

    assert result.email is None
    assert result.email_verified is None
    assert result.unavailable_reason == "clerk_timeout"


def test_404_is_fail_soft_not_raised():
    client = _FakeClerkClient(_FakeUsers(error=_StatusError(404)))

    result = fetch_primary_email(client, USER_ID)

    assert result.email is None
    assert result.email_verified is None
    assert result.unavailable_reason == "clerk_user_not_found"


def test_5xx_is_fail_soft_not_raised():
    client = _FakeClerkClient(_FakeUsers(error=_StatusError(503)))

    result = fetch_primary_email(client, USER_ID)

    assert result.email is None
    assert result.email_verified is None
    assert result.unavailable_reason == "clerk_api_error_503"


def test_unclassified_exception_is_still_fail_soft():
    client = _FakeClerkClient(_FakeUsers(error=RuntimeError("something else broke")))

    result = fetch_primary_email(client, USER_ID)

    assert result.email is None
    assert result.email_verified is None
    assert result.unavailable_reason == "clerk_request_failed"


def test_no_matching_primary_address_is_fail_soft():
    """`primary_email_address_id` set but absent from `email_addresses` (or
    unset with no addresses at all) — a real but odd Clerk account shape,
    not a network failure. Must still degrade, never raise or fabricate an
    address."""
    client = _FakeClerkClient(
        _FakeUsers(result=_user("addr_missing", [_address("addr_1", "a@example.com", "verified")]))
    )

    result = fetch_primary_email(client, USER_ID)

    assert result.email is None
    assert result.email_verified is None
    assert result.unavailable_reason == "no_primary_email_on_account"


def test_no_email_addresses_at_all_is_fail_soft():
    client = _FakeClerkClient(_FakeUsers(result=_user(None, [])))

    result = fetch_primary_email(client, USER_ID)

    assert result.unavailable_reason == "no_primary_email_on_account"

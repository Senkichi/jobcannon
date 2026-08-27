"""Clerk session verification (1B spec §2).

Production path: clerk-backend-api's authenticate_request with jwt_key —
LOCAL RS256 verification, zero network calls per request (Flask's request
object duck-types the SDK's Requestish: it only calls request.headers.get).
Tests inject VERIFY_REQUEST instead — the SDK is never imported in tests.

The four CLERK_* values are read from HostConfig (jobcannon.host.config),
not os.environ directly — the config-surface refactor of issue #47, so
tests/host/test_render_config.py's required-env-var guard can derive the
full set from HostConfig's field metadata instead of a hand-maintained
literal.

`fetch_primary_email` (issue #181) is a second Clerk Backend API call added
here rather than in jobcannon/web/account.py or jobcannon/web/export.py:
this module is already "the one Clerk SDK client construction site" per
build_clerk_client's docstring, and jobcannon/web/account.py's post_delete
reuses that SAME client instance (app.config["CLERK_CLIENT"]) for its own
Clerk call rather than building a second one — the export route does the
same. Duck-types the returned `models.User` (attribute access via getattr,
never `isinstance` against a clerk_backend_api model class) so the fake
`CLERK_CLIENT` test doubles every test in this package already uses (plain
classes/SimpleNamespace, e.g. tests/host/test_account_route.py's
_FakeClerkClient) can stand in without constructing real SDK model
instances — keeping this module's "SDK never imported in tests" property
intact for the new code path too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from jobcannon.host.config import HostConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClerkIdentity:
    user_id: str
    claims: dict


def build_clerk_client(host_config: HostConfig) -> Any:
    """The one Clerk SDK client construction site. `build_clerk_verifier`
    below calls this when it isn't handed an existing client, and
    `jobcannon.web.__init__.create_app` calls it once and shares the result
    between the verifier and `jobcannon.web.account` (the user-delete
    management call) — both hold the same credentialed client instance
    rather than each minting its own."""
    from clerk_backend_api import Clerk

    if not host_config.clerk_secret_key:
        raise RuntimeError("CLERK_SECRET_KEY is required (Clerk backend API secret)")
    return Clerk(bearer_auth=host_config.clerk_secret_key)


def build_clerk_verifier(
    host_config: HostConfig, client: Any | None = None
) -> Callable[[Any], ClerkIdentity | None]:
    from clerk_backend_api.security.types import AuthenticateRequestOptions

    sdk = client if client is not None else build_clerk_client(host_config)
    jwt_key = host_config.clerk_jwt_key
    if not jwt_key:
        # An unset key silently falls back to a per-request JWKS network
        # fetch (breaking the networkless RS256 guarantee this module's
        # docstring promises) whose non-TokenVerificationError exceptions
        # propagate uncaught. Fail fast at startup instead.
        raise RuntimeError(
            "CLERK_JWT_KEY is required (networkless RS256 verification; unset forces "
            "per-request JWKS fetch)"
        )
    # Normalize each configured party to a bare origin: strip whitespace AND
    # a trailing slash, drop anything left empty. The SDK checks the
    # token's `azp` claim (a bare origin, e.g. "https://jobcannon.dev", no
    # trailing slash) for exact membership in this list — an
    # operator-entered value with a trailing slash (Render's
    # CLERK_AUTHORIZED_PARTIES was set to "https://jobcannon.dev/") would
    # otherwise reject every token with TOKEN_INVALID_AUTHORIZED_PARTIES
    # even after #149's __session fix (issue #149 point 3).
    authorized_parties = [
        normalized
        for p in host_config.clerk_authorized_parties.split(",")
        if (normalized := p.strip().rstrip("/"))
    ]
    if not authorized_parties:
        # Unset/blank -> `authorized_parties or None` below -> None -> the SDK
        # skips the azp (cross-origin token-replay) check silently. Fail fast
        # at startup instead of leaving that defense quietly disabled.
        raise RuntimeError(
            "CLERK_AUTHORIZED_PARTIES is required (comma-separated allowed origins; "
            "azp replay defense)"
        )

    def verify(request) -> ClerkIdentity | None:
        state = sdk.authenticate_request(
            request,
            AuthenticateRequestOptions(
                jwt_key=jwt_key,
                authorized_parties=authorized_parties or None,
            ),
        )
        if not state.is_signed_in or not state.payload:
            return None
        return ClerkIdentity(user_id=state.payload["sub"], claims=dict(state.payload))

    return verify


@dataclass(frozen=True)
class ClerkEmailLookup:
    """Result of `fetch_primary_email` — always one of two shapes: a
    resolved `email` (`email_verified` a real bool, `unavailable_reason`
    None), or `email` None with `unavailable_reason` set (never a raised
    exception — see `fetch_primary_email`)."""

    email: str | None
    email_verified: bool | None
    unavailable_reason: str | None


def _clerk_failure_reason(exc: Exception) -> str:
    """Classify a failed `users.get()` call into a stable reason string.
    Duck-types `.status_code` rather than `isinstance`-checking
    clerk_backend_api's `ClerkErrors`/`SDKError` (both expose it as a plain
    int via their shared `ClerkBaseError` base) so this needs no SDK import
    and works identically against a test double's fake exception. Never
    reads `str(exc)`/`exc.args` — only the status code and the exception's
    own class name — so nothing from the request (headers, bearer token)
    can end up in the derived reason or the log line below."""
    status_code = getattr(exc, "status_code", None)
    if status_code == 404:
        return "clerk_user_not_found"
    if status_code is not None:
        return f"clerk_api_error_{status_code}"
    if "timeout" in type(exc).__name__.lower():
        return "clerk_timeout"
    return "clerk_request_failed"


def fetch_primary_email(
    client: Any, user_id: str, *, timeout_ms: int = 5000
) -> ClerkEmailLookup:
    """Look up a Clerk user's primary email address + verification status
    via `client.users.get(user_id=...)` — the SAME credentialed client
    jobcannon.web.account's user-delete call reuses (never a second
    `Clerk(...)` construction). Fail-soft BY DESIGN: this is called on
    jobcannon/web/export.py's request path, so a Clerk outage, timeout, or
    4xx/5xx must degrade to a null email + reason, never raise (which would
    otherwise turn an unrelated-to-Clerk data export into a 500) and never
    retry-storm the request (see `retries=None` below).

    `client is None` covers both "Clerk isn't configured" (TESTING apps
    that never set CLERK_CLIENT, matching jobcannon/web/__init__.py's
    `app.config["CLERK_CLIENT"] = clerk_client` which stays None there) and
    a real deployment would instead fail at boot via build_clerk_client's
    own RuntimeError — this branch exists for the former, not to paper over
    the latter.

    `retries=None` (rather than leaving the SDK's per-call default) is
    load-bearing: clerk_backend_api's default retry policy is exponential
    backoff on 5XX with `max_elapsed_time=3_600_000` ms (one hour) — passing
    only `timeout_ms` would bound a single attempt but not a run of retried
    5xx responses. `retries=None` (a plain value, not the SDK's `UNSET`
    sentinel — see `.get()`'s `if retries == UNSET` branch) short-circuits
    clerk_backend_api's do_request straight to a single attempt, so
    `timeout_ms` is the whole call's bound, matching this function's ~5s
    contract without importing clerk_backend_api.utils.RetryConfig.
    """
    if client is None:
        return ClerkEmailLookup(None, None, "clerk_client_unavailable")

    try:
        user = client.users.get(user_id=user_id, timeout_ms=timeout_ms, retries=None)
    except Exception as exc:
        reason = _clerk_failure_reason(exc)
        logger.warning("Clerk get-user lookup failed for user %s (%s)", user_id, reason)
        return ClerkEmailLookup(None, None, reason)

    primary_id = getattr(user, "primary_email_address_id", None)
    for address in getattr(user, "email_addresses", None) or []:
        if getattr(address, "id", None) != primary_id:
            continue
        verification = getattr(address, "verification", None)
        verified = verification is not None and getattr(verification, "status", None) == "verified"
        return ClerkEmailLookup(getattr(address, "email_address", None), verified, None)
    return ClerkEmailLookup(None, None, "no_primary_email_on_account")

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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from jobcannon.host.config import HostConfig


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

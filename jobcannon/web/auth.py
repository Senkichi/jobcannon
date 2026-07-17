"""Clerk session verification (1B spec §2).

Production path: clerk-backend-api's authenticate_request with jwt_key —
LOCAL RS256 verification, zero network calls per request (Flask's request
object duck-types the SDK's Requestish: it only calls request.headers.get).
Tests inject VERIFY_REQUEST instead — the SDK is never imported in tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ClerkIdentity:
    user_id: str
    claims: dict


def build_clerk_verifier() -> Callable[[Any], ClerkIdentity | None]:
    from clerk_backend_api import Clerk
    from clerk_backend_api.security.types import AuthenticateRequestOptions

    sdk = Clerk(bearer_auth=os.environ["CLERK_SECRET_KEY"])
    jwt_key = os.environ.get("CLERK_JWT_KEY")
    authorized_parties = [
        p for p in os.environ.get("CLERK_AUTHORIZED_PARTIES", "").split(",") if p.strip()
    ]

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

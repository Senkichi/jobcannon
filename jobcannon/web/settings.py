"""jobcannon/web/settings.py — GET /settings and the BYO-key write routes
(issue #332, follow-up to the L-0036 dispatcher/credential-resolver port).

This is the write-path UI the L-0036 port deliberately deferred: nothing else
in the hosted app lets a tenant enter a provider key. The storage/crypto/
resolver layer it sits on already landed — jobcannon/db/_byo_key_credentials
(single reader/writer, RLS-scoped via the transaction-local ``app.user_id``
set inside each of its functions) and jobcannon/host/credentials
(AES-256-GCM envelope, JC_BYO_KEY_KEK).

Authed only — NOT in jobcannon.web.PUBLIC_PATHS, so the before_request gate
guarantees ``g.clerk_user`` and ``g.clerk_user.user_id`` IS the tenant id
keyed into byo_key_credentials (the profile.py/postings_history.py
precedent). A Clerk-issued id is assumed to already have a ``users`` row
(the ``user.created`` webhook and the sign-up handoff both ``ensure_user``) —
the same assumption /profile's save path makes against the identical
``profiles.user_id`` FK; a missing row fails loud (500) rather than being
silently repaired.

Provider list: derived, never hand-maintained — ``provider_catalog.
PROVIDER_KEY_FIELDS`` (roster order) intersected with
``model_provider.HOSTED_ELIGIBLE_PROVIDERS``. A provider gains a settings row
only by satisfying both catalog facts, exactly like
test_provider_roster_lockstep.py's derived-eligibility set; editing the
catalog or the eligibility tuple updates this page with no edit here.

Write path (POST /settings/keys): a submitted key is encrypted via
``credentials.encrypt_api_key`` AND must survive ``model_provider.
validate_api_key`` — a live, minimal round-trip through the provider's real
adapter — before ``upsert_credential`` stores anything. A failed check stores
nothing. Rotation is this same route re-run with a new key:
``upsert_credential``'s ON CONFLICT clause re-activates a deactivated row on
re-upsert (documented on that function), so "rotate" and "reactivate" are
the same deliberate "enter a new key" gesture — no separate reactivate
control exists, by design.

Deactivate (POST /settings/keys/deactivate): ``deactivate_credential``'s
explicit is_active flip. A flag flip, not a row delete — created_at /
last_used_at survive for audit (that function's own docstring).

Plaintext-key hygiene: the submitted key is NEVER logged (no log line below
carries form data), NEVER rendered back into the page (the password input
re-renders empty on every error branch — a validation failure echoes the
provider error, not the key), and is scrubbed out of any provider error text
shown to the tenant (_scrub_key), since some upstreams echo request material
in their error bodies.

Form contract (the profile.py/consent.py shape): plain form POSTs under the
app-wide CSRFProtect (the template carries csrf_token()); success is PRG —
303 back to GET /settings?saved=<provider> / ?deactivated=<provider>, which
renders the confirmation stamp keyed to that provider's row. Validation
failures re-render the page with an error note — never a redirect that
looks like success; a malformed submission shape (an unknown ``provider``
value — there is no legitimate way to produce one from this page) is a 400,
matching account.py's exact-match-confirm precedent. ``KekNotConfiguredError``
is a 502, matching account.py's external-dependency-failure stance: the
server cannot encrypt what it was asked to store, and a 500-looking raw
exception would hide which side broke.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, g, redirect, render_template, request, url_for

from jobcannon.db._byo_key_credentials import (
    deactivate_credential,
    get_credential,
    upsert_credential,
)
from jobcannon.db.pool import connection_factory
from jobcannon.host.credentials import KekNotConfiguredError, encrypt_api_key
from jobcannon.host.model_provider import HOSTED_ELIGIBLE_PROVIDERS, validate_api_key
from jobcannon.host.provider_catalog import PROVIDER_KEY_FIELDS

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__)

# The page's provider rows, in roster order: every catalog provider that is
# BYO-keyable (key_label set) AND hosted-eligible (pure-REST, in the cascade
# -- see test_provider_roster_lockstep.py for why those are the same things).
# Today: gemini, groq, cerebras.
_KEY_PROVIDERS: tuple[tuple[str, str], ...] = tuple(
    (name, label) for name, label in PROVIDER_KEY_FIELDS if name in HOSTED_ELIGIBLE_PROVIDERS
)
_ALLOWED_PROVIDERS: frozenset[str] = frozenset(name for name, _label in _KEY_PROVIDERS)
_LABEL_FOR: dict[str, str] = dict(_KEY_PROVIDERS)

# Generous ceiling on a submitted key's length — real provider keys are far
# shorter; this only exists so a malformed submission can't push megabytes of
# "key" through encryption and a live provider call.
_MAX_KEY_LENGTH = 512


def _read_provider_rows(user_id: str) -> tuple[list[dict[str, Any]], bool]:
    """(rows, ok): one status dict per hosted-eligible BYO provider.

    Reads fail CLOSED (profile.py's posture): a settings page rendered over a
    failed read would show "Not set" on every provider and invite a key
    submission that looks like a first-time setup on top of rows that may
    exist — and a deactivate control that claims no row exists. On any
    failure `ok` is False and the caller renders the unavailable branch.
    """
    try:
        with connection_factory() as conn:
            rows = []
            for name, label in _KEY_PROVIDERS:
                cred = get_credential(conn, user_id, name)
                rows.append(
                    {
                        "provider": name,
                        "label": label,
                        "configured": cred is not None,
                        "is_active": bool(cred and cred["is_active"]),
                        "created_at": cred["created_at"] if cred else None,
                        "last_used_at": cred["last_used_at"] if cred else None,
                    }
                )
            return rows, True
    except Exception:
        logger.warning(
            "settings page read failed for user %s (rendering unavailable)",
            user_id,
            exc_info=True,
        )
        return [], False


def _render(
    user_id: str,
    *,
    error: str | None = None,
    saved: str | None = None,
    deactivated: str | None = None,
    status: int = 200,
) -> tuple[str, int]:
    """Render the settings page. `saved`/`deactivated` are provider NAMES
    (from the PRG query params), mapped to their catalog labels here — an
    unrecognized name renders no stamp rather than echoing raw query text."""
    providers, ok = _read_provider_rows(user_id)
    return (
        render_template(
            "settings.html",
            providers=providers,
            unavailable=not ok,
            error=error,
            saved_label=_LABEL_FOR.get(saved or ""),
            deactivated_label=_LABEL_FOR.get(deactivated or ""),
        ),
        status,
    )


def _scrub_key(text: str, plaintext_key: str) -> str:
    """Remove the submitted key from provider error text before display —
    some upstreams echo request material (occasionally the credential
    itself) back in error bodies, and the page must never render it."""
    if plaintext_key:
        text = text.replace(plaintext_key, "[redacted]")
    return text[:200]


@settings_bp.get("/settings", strict_slashes=False)
def index():
    user_id = g.clerk_user.user_id
    return _render(
        user_id,
        saved=request.args.get("saved"),
        deactivated=request.args.get("deactivated"),
    )


@settings_bp.post("/settings/keys", strict_slashes=False)
def save_key():
    user_id = g.clerk_user.user_id
    provider = request.form.get("provider", "")
    if provider not in _ALLOWED_PROVIDERS:
        return _render(user_id, error="Unknown provider.", status=400)
    label = _LABEL_FOR[provider]

    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        return _render(user_id, error=f"Enter your {label} to save it.")
    if len(api_key) > _MAX_KEY_LENGTH:
        return _render(user_id, error=f"That {label} is too long to be a real key.")

    # Encrypt BEFORE the live check: an unset/misconfigured KEK is a named,
    # deterministic failure (credentials.py's write path fails hard by
    # design) — fail on it without spending a real provider call.
    try:
        encrypted = encrypt_api_key(api_key)
    except KekNotConfiguredError:
        logger.warning("save_key: KEK unavailable, cannot store key for user %s", user_id)
        return _render(
            user_id,
            error="Key storage isn't configured on this deployment — "
            "nothing was saved. Try again later.",
            status=502,
        )

    # The issue's "test this key" gate: a real round-trip through the
    # provider's adapter with the submitted key, BEFORE anything is stored.
    try:
        validate_api_key(provider, api_key)
    except Exception as exc:
        # Provider rejection, network failure, timeout — all land here as
        # "the live check failed"; the key is NOT stored either way. The
        # provider's own message is shown (scrubbed of the submitted key) so
        # an auth failure is distinguishable from a reachability one.
        logger.warning(
            "save_key: live check failed for user %s provider %s: %s",
            user_id,
            provider,
            _scrub_key(str(exc), api_key),
        )
        return _render(
            user_id,
            error=f"That {label} didn't pass a live check — "
            f"{_scrub_key(str(exc), api_key) or 'the provider rejected it'}. "
            "Nothing was saved.",
        )

    # upsert_credential re-activates a deactivated row on conflict — this is
    # the whole rotation/reactivation flow; there is no second code path.
    with connection_factory() as conn:
        upsert_credential(conn, user_id, provider, encrypted)
    return redirect(url_for("settings.index", saved=provider), code=303)


@settings_bp.post("/settings/keys/deactivate", strict_slashes=False)
def deactivate_key():
    user_id = g.clerk_user.user_id
    provider = request.form.get("provider", "")
    if provider not in _ALLOWED_PROVIDERS:
        return _render(user_id, error="Unknown provider.", status=400)
    with connection_factory() as conn:
        updated = deactivate_credential(conn, user_id, provider)
    if not updated:
        return _render(
            user_id,
            error=f"No {_LABEL_FOR[provider]} is on file — nothing to deactivate.",
        )
    return redirect(url_for("settings.index", deactivated=provider), code=303)

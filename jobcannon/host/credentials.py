"""Host-side per-tenant CredentialResolver builder.

NOT a port -- private (single-user, config.yaml-backed API keys) has no
equivalent; this is new hosted infrastructure (L-0036 PR-1).

Encryption: byo_key_credentials.encrypted_key is opaque bytea (m0001);
no encryption scheme existed before this module. Uses AES-256-GCM
(``cryptography``'s AESGCM -- already a direct dependency, not added by this
PR) with a 12-byte random nonce per encryption, envelope = nonce || ciphertext
(GCM tag included in psycopg/cryptography's ciphertext output). The KEK comes
from the JC_BYO_KEY_KEK env var (jobcannon/host/config.py's HostConfig,
declare_on=("web","worker") -- both services build a call_model), base64
(urlsafe) decoded to 32 raw bytes. Plaintext API keys are NEVER logged and
NEVER returned beyond the resolver closure returned by
build_credential_resolver.

Fail-closed, not fail-fast: an unset JC_BYO_KEY_KEK does not raise at
startup or at build_credential_resolver() call time (nothing invokes hosted
scoring yet -- a hard requirement here would break every existing test/dev
run that doesn't set it). Instead every resolve_credential(provider) call
returns None, which the adapter constructor's existing "raise ValueError on
missing credential" contract turns into "provider unavailable" -- the
cascade already treats that as skip-and-advance.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jobcannon.db._byo_key_credentials import get_credential, touch_last_used
from jobcannon.engine.model_types import CredentialResolver

logger = logging.getLogger(__name__)

_NONCE_LEN = 12
_KEK_ENV_VAR = "JC_BYO_KEY_KEK"


class KekNotConfiguredError(RuntimeError):
    """JC_BYO_KEY_KEK is unset or malformed."""


def _kek() -> bytes:
    raw = os.environ.get(_KEK_ENV_VAR)
    if not raw:
        raise KekNotConfiguredError(f"{_KEK_ENV_VAR} is not set")
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise KekNotConfiguredError(f"{_KEK_ENV_VAR} is not valid urlsafe-base64") from exc
    if len(key) != 32:
        raise KekNotConfiguredError(
            f"{_KEK_ENV_VAR} must decode to 32 bytes (AES-256), got {len(key)}"
        )
    return key


def encrypt_api_key(plaintext: str, *, kek: bytes | None = None) -> bytes:
    """Encrypt `plaintext` for storage in byo_key_credentials.encrypted_key.

    Raises KekNotConfiguredError if `kek` is omitted and JC_BYO_KEY_KEK is
    unset -- unlike resolve_credential (read path), the write path (the
    BYO-key settings UI, out of this port's scope -- see this PR's
    Modularity note) has no fail-soft option: it cannot store a key it
    cannot encrypt.
    """
    key = kek if kek is not None else _kek()
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def _decrypt(blob: bytes, *, kek: bytes) -> str:
    aesgcm = AESGCM(kek)
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


def build_credential_resolver(conn: Any, user_id: str) -> CredentialResolver:
    """Return a CredentialResolver bound to `user_id`.

    Arity is `(provider) -> str | None`, bound to ONE tenant by closure
    so no call site can pass the wrong user_id -- the arity itself makes
    cross-tenant leakage unrepresentable. A fresh resolver is expected to be
    built per call_model() invocation (jobcannon.host.model_provider.call_model
    does this); it is never cached across tenants or calls -- see this PR's
    Modularity note.

    A successful resolve stamps last_used_at (best-effort, non-fatal on
    failure to touch).
    """

    def resolve_credential(provider: str) -> str | None:
        try:
            kek = _kek()
        except KekNotConfiguredError:
            logger.warning(
                "%s unset -- BYO-key credentials unavailable for user_id=%s provider=%s",
                _KEK_ENV_VAR,
                user_id,
                provider,
            )
            return None

        row = get_credential(conn, user_id, provider)
        if row is None or not row["is_active"]:
            return None

        try:
            plaintext = _decrypt(row["encrypted_key"], kek=kek)
        except Exception:
            logger.warning(
                "byo_key_credentials decrypt failed for user_id=%s provider=%s "
                "(corrupt row or KEK mismatch) -- treating as no credential",
                user_id,
                provider,
            )
            return None

        try:
            touch_last_used(conn, user_id, provider)
        except Exception:
            logger.warning(
                "byo_key_credentials last_used_at touch failed for user_id=%s "
                "provider=%s (non-fatal)",
                user_id,
                provider,
            )

        return plaintext

    return resolve_credential

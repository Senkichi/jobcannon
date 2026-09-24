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

build_mailbox_resolver (L-0115, design note "design-aggregators-imap.md"
§1.3) reuses this SAME KEK envelope (AES-256-GCM, JC_BYO_KEY_KEK, nonce ||
ciphertext) rather than minting a second key-management scheme for a
second per-tenant secret type -- the mailbox address and app-password are
combined into one JSON plaintext before encryption (one crypto call site
per credential; see encrypt_mailbox_secret). Consent
(jobcannon.db._events.read_mailbox_consent) is checked BEFORE the
credential row, fail-closed even when an active row exists.

Issue #358 (FU-A): both build_*_resolver functions below are thin typed
closures over ONE shared pipeline, _resolve_tenant_credential -- gate ->
KEK check -> row lookup -> decrypt -> best-effort touch_last_used --
parameterized per credential type by a _CredentialKind spec, so the
fail-closed ordering cannot drift between kinds and a third per-tenant
credential type is another spec, not another copy of the pipeline.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Callable
from typing import Any, NamedTuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jobcannon.db import _mailbox_credentials
from jobcannon.db._byo_key_credentials import get_credential, touch_last_used
from jobcannon.db._events import read_mailbox_consent
from jobcannon.engine.model_types import (
    CredentialResolver,
    MailboxCredential,
    MailboxCredentialResolver,
)

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


class _CredentialKind(NamedTuple):
    """One per-tenant credential type's resolver-pipeline spec (FU-A).

    Fields:
        table: Credential table name -- the log-line prefix
            (``byo_key_credentials`` / ``mailbox_credentials``).
        display: Human label for the KEK-unset warning
            (``BYO-key credentials`` / ``mailbox credentials``).
        gate: ``(conn, user_id) -> bool`` precheck run BEFORE the KEK and
            row steps -- mailbox consent for the mailbox kind, None for
            kinds with no gate (BYO-key has no consent concept).
        fetch_row: ``(conn, user_id, *key) -> row | None``.
        build_result: ``(row, kek) -> resolved value`` -- decrypts the row's
            envelope and shapes the value the resolver returns.
        touch: ``(conn, user_id, *key)`` -- the last_used_at stamp.
        row_is_usable: extra row predicate beyond presence -- BYO-key checks
            ``is_active`` in Python because its fetch returns inactive rows
            too; mailbox's fetch filters ``is_active`` in SQL, so the
            always-true default suffices.
    """

    table: str
    display: str
    gate: Callable[[Any, str], bool] | None
    fetch_row: Callable[..., dict | None]
    build_result: Callable[[dict, bytes], Any]
    touch: Callable[..., None]
    row_is_usable: Callable[[dict], bool] = lambda row: True


def _resolve_tenant_credential(
    conn: Any,
    user_id: str,
    kind: _CredentialKind,
    *,
    key: tuple[Any, ...] = (),
    key_desc: str = "",
) -> Any | None:
    """The one resolver pipeline behind every build_*_resolver (FU-A):
    consent-style gate -> KEK check -> row lookup -> decrypt -> best-effort
    touch_last_used.

    Fail-closed throughout: a declined gate, an unset/malformed KEK, an
    absent/unusable row, or a decrypt failure each resolve to None -- never
    an exception -- so an unconfigured host degrades to "credential
    unavailable" instead of crashing. The last_used_at stamp is best-effort,
    non-fatal.

    `key` holds the positional args narrowing the row within the tenant --
    BYO-key is keyed by provider (``key=(provider,)``), mailbox is a
    singleton (``key=()``). `key_desc` is the same values rendered for log
    lines (``" provider=<name>"`` or ``""``).
    """
    if kind.gate is not None and not kind.gate(conn, user_id):
        return None

    try:
        kek = _kek()
    except KekNotConfiguredError:
        logger.warning(
            "%s unset -- %s unavailable for user_id=%s%s",
            _KEK_ENV_VAR,
            kind.display,
            user_id,
            key_desc,
        )
        return None

    row = kind.fetch_row(conn, user_id, *key)
    if row is None or not kind.row_is_usable(row):
        return None

    try:
        result = kind.build_result(row, kek)
    except Exception:
        logger.warning(
            "%s decrypt failed for user_id=%s%s "
            "(corrupt row or KEK mismatch) -- treating as no credential",
            kind.table,
            user_id,
            key_desc,
        )
        return None

    try:
        kind.touch(conn, user_id, *key)
    except Exception:
        logger.warning(
            "%s last_used_at touch failed for user_id=%s%s (non-fatal)",
            kind.table,
            user_id,
            key_desc,
        )

    return result


_BYO_KEY = _CredentialKind(
    table="byo_key_credentials",
    display="BYO-key credentials",
    gate=None,  # no consent gate on LLM API keys -- mailbox_consent is mailbox-only
    fetch_row=get_credential,
    build_result=lambda row, kek: _decrypt(row["encrypted_key"], kek=kek),
    touch=touch_last_used,
    row_is_usable=lambda row: row["is_active"],
)


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
        return _resolve_tenant_credential(
            conn,
            user_id,
            _BYO_KEY,
            key=(provider,),
            key_desc=f" provider={provider}",
        )

    return resolve_credential


def encrypt_mailbox_secret(address: str, secret: str, *, kek: bytes | None = None) -> bytes:
    """Encrypt (address, secret) as ONE ciphertext for
    mailbox_credentials.encrypted_secret.

    The mailbox address itself is PII (design note §1.1); combining it with
    the app-password into a single JSON plaintext before one AES-256-GCM
    call keeps this the ONE crypto call site for a mailbox credential,
    rather than two separately-nonced columns. Raises KekNotConfiguredError
    if `kek` is omitted and JC_BYO_KEY_KEK is unset -- same fail-hard write
    path as encrypt_api_key (no settings UI wired to this yet -- see this
    PR's Modularity note).
    """
    key = kek if kek is not None else _kek()
    plaintext = json.dumps({"address": address, "secret": secret})
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def _decrypt_mailbox_secret(blob: bytes, *, kek: bytes) -> tuple[str, str]:
    plaintext = _decrypt(blob, kek=kek)
    data = json.loads(plaintext)
    return data["address"], data["secret"]


def _mailbox_credential_from_row(row: dict, kek: bytes) -> MailboxCredential:
    address, secret = _decrypt_mailbox_secret(row["encrypted_secret"], kek=kek)
    return MailboxCredential(
        address=address,
        secret=secret,
        imap_host=row["imap_host"],
        imap_port=row["imap_port"],
        folder=row["folder"],
    )


_MAILBOX = _CredentialKind(
    table="mailbox_credentials",
    display="mailbox credentials",
    # Consent gate FIRST, before the KEK/row steps -- fail-closed on absent
    # consent even when an active row exists, so revoking consent makes a
    # tenant intake-ineligible immediately.
    gate=read_mailbox_consent,
    fetch_row=_mailbox_credentials.get_active_for_user,
    build_result=_mailbox_credential_from_row,
    touch=_mailbox_credentials.touch_last_used,
    # row_is_usable default: get_active_for_user filters is_active in SQL.
)


def build_mailbox_resolver(conn: Any, user_id: str) -> MailboxCredentialResolver:
    """Return a MailboxCredentialResolver bound to `user_id`.

    Arity is `() -> MailboxCredential | None` (design note §1.3) -- a
    tenant has at most one mailbox credential, so there is no
    provider-name parameter the way build_credential_resolver has one.
    Order of checks matters: consent is read FIRST, before the credential
    row is even queried -- fail-closed on absent consent even when an
    active row exists, so revoking consent makes a tenant intake-ineligible
    immediately without needing a separate row deactivation. A successful
    resolve stamps last_used_at (best-effort, non-fatal on failure to
    touch) -- mirrors build_credential_resolver.
    """

    def resolve_mailbox_credential() -> MailboxCredential | None:
        return _resolve_tenant_credential(conn, user_id, _MAILBOX)

    return resolve_mailbox_credential

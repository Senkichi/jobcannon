"""jobcannon.host.credentials -- AES-256-GCM envelope + per-tenant
CredentialResolver builder (L-0036 PR-1).

Not a port: no private-repo equivalent exists. Covers the encrypt/decrypt
round trip, the fail-closed-not-fail-fast KEK-unset contract (a hard
requirement -- resolve_credential must return None, never raise, so an
unconfigured host degrades to "no BYO-key providers" instead of crashing),
and the resolver's non-fatal handling of a corrupt/mismatched-key row.
"""

from __future__ import annotations

import base64
import os

import pytest

from jobcannon.db._byo_key_credentials import get_credential, upsert_credential
from jobcannon.host.credentials import (
    KekNotConfiguredError,
    _kek,
    build_credential_resolver,
    encrypt_api_key,
)

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _fresh_kek_b64() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))


def test_encrypt_then_decrypt_round_trips_via_internal_helper(monkeypatch):
    kek_b64 = _fresh_kek_b64()
    monkeypatch.setenv("JC_BYO_KEY_KEK", kek_b64)

    blob = encrypt_api_key("sk-super-secret-key")

    # _decrypt is private; exercise it the same way build_credential_resolver
    # does, through the module's own _kek() to confirm the env var round-trips.
    from jobcannon.host.credentials import _decrypt

    assert _decrypt(blob, kek=_kek()) == "sk-super-secret-key"


def test_encrypt_api_key_raises_when_kek_unset(monkeypatch):
    monkeypatch.delenv("JC_BYO_KEY_KEK", raising=False)

    with pytest.raises(KekNotConfiguredError):
        encrypt_api_key("sk-whatever")


def test_kek_rejects_wrong_length_after_decode(monkeypatch):
    # 16 raw bytes (AES-128 length), not the required 32 (AES-256).
    monkeypatch.setenv("JC_BYO_KEY_KEK", base64.urlsafe_b64encode(os.urandom(16)).decode("ascii"))

    with pytest.raises(KekNotConfiguredError):
        _kek()


def test_kek_rejects_non_base64(monkeypatch):
    monkeypatch.setenv("JC_BYO_KEY_KEK", "not valid base64 !!! @@@")

    with pytest.raises(KekNotConfiguredError):
        _kek()


def test_build_credential_resolver_round_trips_through_db(db_conn, monkeypatch):
    kek_b64 = _fresh_kek_b64()
    monkeypatch.setenv("JC_BYO_KEY_KEK", kek_b64)
    _seed_user(db_conn, "cred-u1")

    blob = encrypt_api_key("sk-tenant-one-key")
    upsert_credential(db_conn, "cred-u1", "gemini", blob)

    resolve = build_credential_resolver(db_conn, "cred-u1")

    assert resolve("gemini") == "sk-tenant-one-key"


def test_resolver_stamps_last_used_at_on_success(db_conn, monkeypatch):
    monkeypatch.setenv("JC_BYO_KEY_KEK", _fresh_kek_b64())
    _seed_user(db_conn, "cred-u2")
    upsert_credential(db_conn, "cred-u2", "groq", encrypt_api_key("sk-groq"))
    assert get_credential(db_conn, "cred-u2", "groq")["last_used_at"] is None

    resolve = build_credential_resolver(db_conn, "cred-u2")
    resolve("groq")

    assert get_credential(db_conn, "cred-u2", "groq")["last_used_at"] is not None


def test_resolver_returns_none_for_absent_credential(db_conn, monkeypatch):
    monkeypatch.setenv("JC_BYO_KEY_KEK", _fresh_kek_b64())
    _seed_user(db_conn, "cred-u3")

    resolve = build_credential_resolver(db_conn, "cred-u3")

    assert resolve("cerebras") is None


def test_resolver_returns_none_when_kek_unset_not_raises(db_conn, monkeypatch):
    monkeypatch.delenv("JC_BYO_KEY_KEK", raising=False)
    _seed_user(db_conn, "cred-u4")
    # A row exists (encrypted under some other key entirely -- doesn't
    # matter, the resolver must never reach decryption without a KEK).
    upsert_credential(db_conn, "cred-u4", "gemini", b"\x00" * 28)

    resolve = build_credential_resolver(db_conn, "cred-u4")

    assert resolve("gemini") is None  # fail-closed, not an exception


def test_resolver_returns_none_on_decrypt_failure_wrong_kek(db_conn, monkeypatch):
    monkeypatch.setenv("JC_BYO_KEY_KEK", _fresh_kek_b64())
    _seed_user(db_conn, "cred-u5")
    upsert_credential(db_conn, "cred-u5", "gemini", encrypt_api_key("sk-under-key-A"))

    # Swap in a different KEK before resolving -- simulates a corrupted row
    # or a rotated KEK the row was never re-encrypted under.
    monkeypatch.setenv("JC_BYO_KEY_KEK", _fresh_kek_b64())
    resolve = build_credential_resolver(db_conn, "cred-u5")

    assert resolve("gemini") is None  # non-fatal, not a raised exception


def test_resolver_returns_none_for_inactive_credential(db_conn, monkeypatch):
    from jobcannon.db._byo_key_credentials import deactivate_credential

    monkeypatch.setenv("JC_BYO_KEY_KEK", _fresh_kek_b64())
    _seed_user(db_conn, "cred-u6")
    upsert_credential(db_conn, "cred-u6", "cerebras", encrypt_api_key("sk-cerebras"))
    deactivate_credential(db_conn, "cred-u6", "cerebras")

    resolve = build_credential_resolver(db_conn, "cred-u6")

    assert resolve("cerebras") is None


def test_resolver_is_bound_to_one_tenant_no_user_id_arity(db_conn, monkeypatch):
    """design §1b: resolve_credential's arity is (provider) only -- confirm
    the Protocol shape structurally (a resolver built for tenant A cannot be
    redirected at tenant B's rows by any argument the caller controls)."""
    monkeypatch.setenv("JC_BYO_KEY_KEK", _fresh_kek_b64())
    _seed_user(db_conn, "cred-u7a")
    _seed_user(db_conn, "cred-u7b")
    upsert_credential(db_conn, "cred-u7a", "gemini", encrypt_api_key("sk-tenant-a"))
    upsert_credential(db_conn, "cred-u7b", "gemini", encrypt_api_key("sk-tenant-b"))

    resolve_a = build_credential_resolver(db_conn, "cred-u7a")
    resolve_b = build_credential_resolver(db_conn, "cred-u7b")

    assert resolve_a("gemini") == "sk-tenant-a"
    assert resolve_b("gemini") == "sk-tenant-b"
    assert resolve_a.__code__.co_argcount == 1  # provider only, no user_id

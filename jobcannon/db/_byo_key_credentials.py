"""Single reader/writer for byo_key_credentials (jobcannon/db/migrations/m0001).

NOT a port -- no private-repo equivalent exists; BYO-key hosted credentials
are new to this host (the private, single-user app used owner-level API
keys from config.yaml, never per-tenant stored credentials). Mirrors the
single-writer pattern the rest of jobcannon/db/_*.py uses (see
_direct_link.py, _profiles.py): one module owns every read and write against
its table.

``byo_key_credentials`` is ``FORCE ROW LEVEL SECURITY`` with zero policies
until ``jobcannon/db/migrations/m0018_byo_key_credentials_rls.py`` runs (see
that migration's docstring) -- default-deny for every role, including the
table owner, until then. That policy's predicate is
``user_id = current_setting('app.user_id', true)``; every function below
sets that session var via ``SELECT set_config('app.user_id', %s, true)``
immediately before touching the table. The third argument (``true``) scopes
the setting to the CURRENT TRANSACTION (Postgres's ``is_local`` flag on
``set_config``) rather than the session, matching this host's pooled
connections (jobcannon/db/pool.py): a session-scoped SET would survive
past this call on a connection later checked out for a different tenant's
request, which is exactly the cross-tenant leak class RLS exists to
prevent. A transaction-scoped SET reverts automatically at COMMIT/ROLLBACK,
so there is nothing to remember to reset.

This is the first RLS-scoped table in the public schema -- there is no
prior ``app.user_id``-style session-var convention to match (grepped: no
other migration defines a policy). This module establishes it.
"""

from __future__ import annotations

from typing import Any

from jobcannon.db.pool import commit_unless_nested


def _set_tenant(raw: Any, user_id: str) -> None:
    raw.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))


def get_active_providers(conn: Any, user_id: str) -> list[str]:
    """Return the tenant's active provider names, unordered.

    Callers intersect this with a hosted-eligibility set (e.g.
    jobcannon.host.model_provider.HOSTED_ELIGIBLE_PROVIDERS) -- this module
    has no opinion on which providers are hosted-eligible.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    rows = raw.execute(
        "SELECT provider FROM byo_key_credentials WHERE user_id = %s AND is_active",
        (user_id,),
    ).fetchall()
    return [row["provider"] for row in rows]


def get_credential(conn: Any, user_id: str, provider: str) -> dict | None:
    """Return the full row for (user_id, provider), or None if absent.

    Keys: provider, encrypted_key (bytes), is_active, created_at,
    last_used_at. Returns rows regardless of is_active -- callers that care
    (e.g. jobcannon.host.credentials.build_credential_resolver) check it
    themselves so an inactive-vs-absent distinction is never lost here.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    row = raw.execute(
        "SELECT provider, encrypted_key, is_active, created_at, last_used_at "
        "FROM byo_key_credentials WHERE user_id = %s AND provider = %s",
        (user_id, provider),
    ).fetchone()
    if row is None:
        return None
    return {
        "provider": row["provider"],
        "encrypted_key": bytes(row["encrypted_key"]),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


def upsert_credential(conn: Any, user_id: str, provider: str, encrypted_key: bytes) -> None:
    """Insert or replace the tenant's encrypted key for `provider`.

    A re-upsert reactivates a previously deactivated credential (is_active
    reset to true) -- entering a new key is an unambiguous signal the
    tenant wants this provider active again. Commits on write.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    raw.execute(
        """
        INSERT INTO byo_key_credentials (user_id, provider, encrypted_key, is_active)
        VALUES (%s, %s, %s, true)
        ON CONFLICT (user_id, provider)
        DO UPDATE SET encrypted_key = EXCLUDED.encrypted_key, is_active = true
        """,
        (user_id, provider, encrypted_key),
    )
    commit_unless_nested(raw)


def deactivate_credential(conn: Any, user_id: str, provider: str) -> bool:
    """Set is_active=false for (user_id, provider). Returns True if a row
    was updated, False if no such row exists. Commits on write.

    Deactivation (not delete) is deliberate: last_used_at / created_at stay
    available for audit, and re-upserting a key later reactivates cleanly
    rather than needing a fresh INSERT to re-satisfy the primary key.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    cur = raw.execute(
        "UPDATE byo_key_credentials SET is_active = false WHERE user_id = %s AND provider = %s",
        (user_id, provider),
    )
    commit_unless_nested(raw)
    return cur.rowcount > 0


def touch_last_used(conn: Any, user_id: str, provider: str) -> None:
    """Stamp last_used_at = now() for (user_id, provider). Best-effort:
    a missing row is a silent no-op (nothing to touch). Commits on write.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    raw.execute(
        "UPDATE byo_key_credentials SET last_used_at = now() WHERE user_id = %s AND provider = %s",
        (user_id, provider),
    )
    commit_unless_nested(raw)

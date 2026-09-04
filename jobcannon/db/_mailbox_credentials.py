"""Single reader/writer for mailbox_credentials (jobcannon/db/migrations/m0025).

NOT a port -- no private-repo equivalent exists; job-cannon (private) is
single-user and keeps its one IMAP mailbox's host/address/app-password in a
gitignored config.yaml, never a per-tenant stored credential. Mirrors
jobcannon/db/_byo_key_credentials.py's single-writer, RLS-scoped shape
exactly (see that module's docstring for the ``app.user_id`` session-var /
transaction-local set_config rationale -- unchanged here, same convention,
established by m0020 and reused as-is by m0025).

Consent is NOT checked by this module. jobcannon.db._events.read_mailbox_consent
is the sanctioned reader for users.mailbox_consent, and
jobcannon.host.credentials.build_mailbox_resolver is the sole caller that
combines the two (consent gate, THEN this module's row) -- keeping that join
out of this module means a caller who genuinely needs the raw row (e.g. a
future settings UI listing "connected mailbox" state regardless of the
current consent toggle) is not forced through a consent check that has
nothing to do with row existence.
"""

from __future__ import annotations

from typing import Any

from jobcannon.db.pool import commit_unless_nested


def _set_tenant(raw: Any, user_id: str) -> None:
    raw.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))


def get_active_for_user(conn: Any, user_id: str) -> dict | None:
    """Return the tenant's active mailbox credential row, or None if absent
    or ``is_active`` is false.

    Keys: imap_host, imap_port, auth_type, folder, encrypted_secret (bytes),
    username_hint, uid_highwater, uid_validity, created_at, last_used_at.
    Unlike _byo_key_credentials.get_credential, this filters is_active in
    SQL rather than returning inactive rows for the caller to check --
    there is exactly one credential per tenant here (PK is user_id alone,
    not (user_id, provider)), so there is no "which provider" distinction
    for an inactive-vs-absent caller to need; both read as None.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    row = raw.execute(
        "SELECT imap_host, imap_port, auth_type, folder, encrypted_secret, "
        "username_hint, uid_highwater, uid_validity, created_at, last_used_at "
        "FROM mailbox_credentials WHERE user_id = %s AND is_active",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "imap_host": row["imap_host"],
        "imap_port": row["imap_port"],
        "auth_type": row["auth_type"],
        "folder": row["folder"],
        "encrypted_secret": bytes(row["encrypted_secret"]),
        "username_hint": row["username_hint"],
        "uid_highwater": row["uid_highwater"],
        "uid_validity": row["uid_validity"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


def set_mailbox_credential(
    conn: Any,
    user_id: str,
    *,
    imap_host: str,
    imap_port: int,
    auth_type: str,
    folder: str,
    encrypted_secret: bytes,
    username_hint: str | None,
) -> None:
    """Insert or replace the tenant's mailbox credential.

    A re-set reactivates a previously deactivated credential (is_active
    reset to true) and resets uid_highwater/uid_validity to 0 -- entering a
    new credential (new mailbox, new app password, or a re-auth after a
    provider-side revoke) invalidates any prior fetch progress; resuming
    from a stale highwater against a mailbox the credential no longer
    describes risks silently skipping mail. Commits on write.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    raw.execute(
        """
        INSERT INTO mailbox_credentials
            (user_id, imap_host, imap_port, auth_type, folder,
             encrypted_secret, username_hint, is_active,
             uid_highwater, uid_validity)
        VALUES (%s, %s, %s, %s, %s, %s, %s, true, 0, 0)
        ON CONFLICT (user_id) DO UPDATE SET
            imap_host = EXCLUDED.imap_host,
            imap_port = EXCLUDED.imap_port,
            auth_type = EXCLUDED.auth_type,
            folder = EXCLUDED.folder,
            encrypted_secret = EXCLUDED.encrypted_secret,
            username_hint = EXCLUDED.username_hint,
            is_active = true,
            uid_highwater = 0,
            uid_validity = 0
        """,
        (user_id, imap_host, imap_port, auth_type, folder, encrypted_secret, username_hint),
    )
    commit_unless_nested(raw)


def deactivate_credential(conn: Any, user_id: str) -> bool:
    """Set is_active=false for the tenant's row. Returns True if a row was
    updated, False if no such row exists. Commits on write. Deactivation
    (not delete) mirrors _byo_key_credentials.deactivate_credential: audit
    columns stay available, and a later set_mailbox_credential reactivates
    cleanly."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    cur = raw.execute(
        "UPDATE mailbox_credentials SET is_active = false WHERE user_id = %s",
        (user_id,),
    )
    commit_unless_nested(raw)
    return cur.rowcount > 0


def touch_last_used(conn: Any, user_id: str) -> None:
    """Stamp last_used_at = now() for the tenant's row. Best-effort: a
    missing row is a silent no-op. Commits on write."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    raw.execute(
        "UPDATE mailbox_credentials SET last_used_at = now() WHERE user_id = %s",
        (user_id,),
    )
    commit_unless_nested(raw)


def advance_uid_highwater(
    conn: Any, user_id: str, *, uid_highwater: int, uid_validity: int
) -> None:
    """Persist this run's ending UID watermark + the folder's UIDVALIDITY it
    was computed against. Called ONLY after a run's parsed jobs have been
    durably handed off (host/ingestion/imap_intake.py calls this last, after
    capture.py's writes commit) -- a crash or exception between fetching
    mail and this call leaves uid_highwater at its prior value, so the next
    run re-fetches and re-parses the same UID range rather than silently
    skipping it (at-least-once, never at-most-once, for this watermark).

    Always sets BOTH columns together, even on a same-epoch continuation
    (uid_validity unchanged) -- there is no partial-update variant, so a
    caller can never advance one without the other and leave them
    inconsistent. Commits on write.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    _set_tenant(raw, user_id)
    raw.execute(
        "UPDATE mailbox_credentials SET uid_highwater = %s, uid_validity = %s WHERE user_id = %s",
        (uid_highwater, uid_validity, user_id),
    )
    commit_unless_nested(raw)

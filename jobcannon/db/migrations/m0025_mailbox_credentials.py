"""Migration 25 -- mailbox_credentials + users.mailbox_consent.

New infrastructure for the IMAP intake port (ledger L-0115), not itself a
port of a private-repo migration -- job-cannon (private) is single-user and
keeps IMAP credentials in a gitignored config.yaml, so there is no per-user
credential table to translate. This migration adopts byo_key_credentials'
shape (m0001) and RLS convention (m0020) wholesale: same PK-on-user_id,
ENABLE + FORCE ROW LEVEL SECURITY, zero policies at CREATE time, one
tenant_isolation FOR ALL policy added in the same migration (no separate
RLS-only follow-up migration -- that split in m0020 existed only because
byo_key_credentials' table predated its own RLS migration; a brand-new
table has no such history to preserve).

Design note: design-aggregators-imap.md §1.1-1.2.

Shape notes:
- username_hint is a MASKED display value only (e.g. "j***@gmail.com") --
  never the plaintext mailbox address. The address itself is PII and is
  combined with the app-password secret into ONE encrypted_secret
  ciphertext (single crypto call site; see host/credentials.py
  build_mailbox_resolver), never stored in a separate plaintext column.
- uid_highwater replaces the private ImapSource's IMAP \\Seen flag
  write-back: this port's mailbox access is read-only (BODY.PEEK[], no
  flag mutation ever sent to the user's real mailbox), so progress is
  tracked server-side-free via a per-credential UID watermark instead.
- uid_validity anchors uid_highwater to an IMAPVALIDITY epoch (RFC 3501
  §2.3.1.1): UIDs are only monotonic *within* one UIDVALIDITY value for a
  folder. If the mailbox provider recreates the folder (UIDVALIDITY
  changes), a stale highwater compared against the new epoch's UIDs would
  silently skip mail -- host/ingestion/imap_intake.py resets
  uid_highwater to 0 whenever the folder's live UIDVALIDITY diverges from
  the stored value, before computing this run's search range.

users.mailbox_consent mirrors users.analytics_consent (m0004): an O(1)
current-state column read once by host/credentials.py before ANY
mailbox_credentials row is treated as usable (fail-closed even when a row
exists) -- see jobcannon/db/_events.py record_consent's mailbox branch,
which updates this column and the consent_recorded audit event in one
transaction, same pattern as the analytics branch.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=25,
    description="mailbox_credentials table + RLS + users.mailbox_consent",
    sql=[
        """
        CREATE TABLE mailbox_credentials (
            user_id          text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            imap_host        text NOT NULL,
            imap_port        int NOT NULL DEFAULT 993,
            auth_type        text NOT NULL CHECK (auth_type IN ('app_password', 'oauth')),
            folder           text NOT NULL DEFAULT 'INBOX',
            encrypted_secret bytea NOT NULL,
            username_hint    text,
            is_active        boolean NOT NULL DEFAULT true,
            uid_highwater    bigint NOT NULL DEFAULT 0,
            uid_validity     bigint NOT NULL DEFAULT 0,
            created_at       timestamptz NOT NULL DEFAULT now(),
            last_used_at     timestamptz,
            PRIMARY KEY (user_id)
        )
        """,
        "ALTER TABLE mailbox_credentials ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE mailbox_credentials FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS tenant_isolation ON mailbox_credentials",
        """
        CREATE POLICY tenant_isolation ON mailbox_credentials
        FOR ALL
        USING (user_id = current_setting('app.user_id', true))
        WITH CHECK (user_id = current_setting('app.user_id', true))
        """,
        "ALTER TABLE users ADD COLUMN mailbox_consent boolean NOT NULL DEFAULT false",
        "ALTER TABLE users ADD COLUMN mailbox_consent_updated_at timestamptz",
    ],
)

"""Migration 4 — users.analytics_consent (current opt-in state).

log_event does 'one boolean read before writing anywhere'. The events log is
append-only, so current consent needs an O(1) column rather than a scan of
consent_recorded history. record_consent updates this column AND inserts the
consent_recorded audit event in one transaction.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=4,
    description="users.analytics_consent + analytics_consent_updated_at",
    sql=[
        "ALTER TABLE users ADD COLUMN analytics_consent boolean NOT NULL DEFAULT false",
        "ALTER TABLE users ADD COLUMN analytics_consent_updated_at timestamptz",
    ],
)

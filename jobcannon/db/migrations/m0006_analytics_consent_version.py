"""Migration 6 — users.analytics_consent_version (issue: consent version is
recorded in the events audit trail but never enforced on read).

record_consent (jobcannon/db/_events.py) has always accepted a
consent_version argument and written it into the consent_recorded event
payload, but nothing read a user's stored version back — bumping
jobcannon.web.consent.CONSENT_VERSION after adding a new tracked event type
silently let every prior grant keep authorizing it. This migration gives the
CURRENT decision its own O(1) column on users, mirroring the
analytics_consent / analytics_consent_updated_at pair m0004 added for the
same reason: current-state reads should not have to scan the append-only
events log. record_consent now writes this column in the SAME UPDATE (and
therefore the same transaction) it already uses for analytics_consent.

No DEFAULT and no NOT NULL: every row that predates this migration lands
NULL here, which read_consent_state / read_consent_choice_made
(jobcannon/db/_events.py) treat as "not this version" whenever
analytics_consent is true — the same fail-closed direction v1 already took
for a whole-cloth non-consenting default. A NULL-version row granted before
version tracking existed must not silently keep authorizing whatever
CONSENT_VERSION denotes today.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=6,
    description="users.analytics_consent_version (enforce consent version on read)",
    sql=[
        "ALTER TABLE users ADD COLUMN analytics_consent_version text",
    ],
)

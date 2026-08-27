"""Migration 10 — rewrite `user_signed_up` payloads: referrer_url -> referrer_host
(issue #153: the payload key name read as "this is a URL" when the value it
holds has always been hostname-only).

jobcannon/db/events_schema.py's _ALLOWED_KEYS["user_signed_up"] and
jobcannon/web/handoff.py's payload dict (the event's only writer) are renamed
in the same PR that adds this migration — the code and the stored data must
agree on the key name going forward. This migration is the one-time catch-up
for rows written before the rename: every existing `user_signed_up` row whose
payload still carries the old `referrer_url` key gets it replaced with
`referrer_host`, same value, no other keys touched.

Scoped to event_type = 'user_signed_up' (the only event type events_schema.py
ever allowed this key for) AND `payload ? 'referrer_url'` (the jsonb
"has key" operator) so rows that already lack the key — including any
inserted between deploy and migration run by newly-deployed code that already
writes `referrer_host` — are left untouched rather than gaining a spurious
`referrer_host: null`.

`(payload - 'referrer_url')` (jsonb "delete key" operator) drops the old key;
`jsonb_build_object('referrer_host', payload->'referrer_url')` wraps the
existing value under the new key; `||` (jsonb concatenation) merges that back
over the other payload keys (channel, wave, signup_method) which pass through
unchanged. No value transformation: the string that was already
hostname-only (jobcannon/web/anon_session.py's _referrer_host) is carried
over byte-for-byte.

Privacy Policy §3.3 already describes this field as "the hostname of the
site that referred you" — the legal text was correct before this migration
and needs no change; only the internal payload key name was misleading.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=10,
    description="events.payload: rewrite user_signed_up referrer_url key to referrer_host",
    sql=[
        "UPDATE events "
        "SET payload = (payload - 'referrer_url') "
        "|| jsonb_build_object('referrer_host', payload -> 'referrer_url') "
        "WHERE event_type = 'user_signed_up' AND payload ? 'referrer_url'",
    ],
)

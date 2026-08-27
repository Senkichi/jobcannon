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

Deploy order: run this migration AFTER jobcannon-web has finished rolling
out the renamed writer (jobcannon/web/handoff.py), not before or
concurrently. The web and worker services (render.yaml) deploy
independently with no ordering guarantee; the ledger blocks this migration
from ever running twice, so any `user_signed_up` row written by an
old (pre-rename) web worker after this migration has already committed
keeps the old `referrer_url` key permanently. Impact of getting the order
wrong is benign either way — the value is hostname-only either key name,
`has_signed_up_event` dedups on event_type not the payload key, and any
straggler rows are a cosmetic naming leftover, not a data-integrity or
privacy defect — but deploy-code-first avoids creating stragglers at all.

Inverted-order safety (issue #199): Render's pre-deploy step now always
runs this migration BEFORE jobcannon-web's renamed writer goes live,
inverting the "Deploy order: ... AFTER" note above, which predates that
guarantee (docs/deploy-runbook.md §3, "Migration/writer ordering also
inverted"). This UPDATE is idempotent under that inverted ordering: its
WHERE clause (`payload ? 'referrer_url'`) only ever matches rows that
still carry the old key, so running it before the new writer exists
simply processes whatever old-format rows already exist at that moment —
it does not depend on the new writer's output existing yet, and re-running
the same UPDATE (if the ledger's once-only guarantee were ever bypassed)
would match and touch zero additional rows once the corpus is caught up.
The only consequence of the inverted order is the benign straggler case
already documented above (old-format rows written by a not-yet-replaced
writer in the gap between this migration committing and web's cutover),
never an error or data-integrity issue.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

# See "Inverted-order safety:" above -- required alongside this flag by
# tests/test_migration_deploy_safety.py (issue #199).
inverted_order_safe = True

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

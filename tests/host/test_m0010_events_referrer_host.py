"""jobcannon/db/migrations/m0010_events_referrer_host.py — the one-time
data rewrite for issue #153: `user_signed_up` payloads written before the
rename still carry the old `referrer_url` key; this migration replaces it
with `referrer_host` in place, same value, no other keys or rows touched.

Same shape as tests/host/test_m0006_analytics_consent_version.py's
test_migration_applies_to_a_users_table_with_pre_existing_rows: monkeypatch
MIGRATIONS to run everything up to (not including) m0010 against a throwaway
database, seed rows with raw SQL (events_schema.validate_payload would now
reject the old key, so a real writer can't produce this fixture — it has to
be inserted directly, simulating data that predates the rename), then run
the rest of MIGRATIONS and assert the rewrite.

Rows seeded, one of each shape the migration's WHERE clause has to
distinguish:
- a `user_signed_up` row with the OLD key -> must be rewritten
- a `user_signed_up` row that already has the NEW key (as newly-deployed
  code would write between deploy and migration run) -> must be left alone,
  proving the `payload ? 'referrer_url'` guard doesn't clobber it
- a non-`user_signed_up` row whose payload incidentally also has a
  `referrer_url` key -> must be left alone, proving the migration is scoped
  by `event_type = 'user_signed_up'` and not just by key presence
"""

from __future__ import annotations

import json

import psycopg
from psycopg.rows import dict_row

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres


def test_migration_rewrites_referrer_url_to_referrer_host(monkeypatch):
    import jobcannon.db.migrate as migrate_mod
    from jobcannon.db.migrations import MIGRATIONS

    dsn, db_name = create_throwaway_db("jobcannon_mig_m0010")
    try:
        pre_m0010 = [m for m in MIGRATIONS if m.version < 10]
        monkeypatch.setattr(migrate_mod, "MIGRATIONS", pre_m0010)
        migrate_mod.run_migrations(dsn)

        with psycopg.connect(dsn) as conn:
            conn.execute("INSERT INTO users (id) VALUES ('m0010_user') ON CONFLICT (id) DO NOTHING")
            conn.execute(
                "INSERT INTO events (user_id, event_type, payload) "
                "VALUES (%s, 'user_signed_up', %s)",
                (
                    "m0010_user",
                    json.dumps(
                        {
                            "channel": "direct",
                            "wave": "0",
                            "signup_method": "clerk",
                            "referrer_url": "example.com",
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO events (user_id, event_type, payload) "
                "VALUES (%s, 'user_signed_up', %s)",
                (
                    "m0010_user",
                    json.dumps(
                        {
                            "channel": "direct",
                            "wave": "0",
                            "signup_method": "clerk",
                            "referrer_host": "already-new.example.com",
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO events (user_id, event_type, payload) "
                "VALUES (%s, 'posting_saved', %s)",
                (
                    "m0010_user",
                    json.dumps({"referrer_url": "should-not-be-touched.example.com"}),
                ),
            )
            conn.commit()

        monkeypatch.setattr(migrate_mod, "MIGRATIONS", MIGRATIONS)
        migrate_mod.run_migrations(dsn)  # must not raise; must rewrite the old-key row

        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT event_type, payload FROM events WHERE user_id = 'm0010_user' ORDER BY id"
            ).fetchall()

        assert len(rows) == 3

        rewritten = rows[0]
        assert rewritten["event_type"] == "user_signed_up"
        assert rewritten["payload"]["referrer_host"] == "example.com"
        assert "referrer_url" not in rewritten["payload"]
        # Other keys pass through unchanged.
        assert rewritten["payload"]["channel"] == "direct"
        assert rewritten["payload"]["wave"] == "0"
        assert rewritten["payload"]["signup_method"] == "clerk"

        already_new = rows[1]
        assert already_new["event_type"] == "user_signed_up"
        assert already_new["payload"]["referrer_host"] == "already-new.example.com"

        untouched = rows[2]
        assert untouched["event_type"] == "posting_saved"
        assert untouched["payload"]["referrer_url"] == "should-not-be-touched.example.com"
        assert "referrer_host" not in untouched["payload"]
    finally:
        drop_throwaway_db(db_name)


def test_migration_is_idempotent_against_already_rewritten_rows():
    """A second run (test_migrate.py's idempotency contract for every
    migration) must not error and must not double-wrap the value — the
    WHERE clause excludes rows that no longer have `referrer_url`."""
    from jobcannon.db.migrate import run_migrations

    dsn, db_name = create_throwaway_db("jobcannon_mig_m0010_idem")
    try:
        run_migrations(dsn)  # includes m0010 against an empty events table
        run_migrations(dsn)  # second full run must be a no-op, not an error

        with psycopg.connect(dsn) as conn:
            count = conn.execute("SELECT count(*) FROM schema_migrations WHERE version = 10")
            assert count.fetchone()[0] == 1
    finally:
        drop_throwaway_db(db_name)

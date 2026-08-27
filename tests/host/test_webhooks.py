"""Webhook receiver tests against real Postgres (Svix signature over the raw
body + idempotent user upsert/delete).

These tests do real, durable commits (INSERT/DELETE on `users`), so — like
tests/host/test_scan_services_contract.py's `wired_services` fixture — the
`app` fixture below opens its OWN throwaway database rather than sharing the
session-scoped `postgres_test_dsn` every other tests/host/ module reads
inside a rollback-isolated transaction. Sharing it here would leak a
committed `users` row into the DB other test modules assume is empty.
"""

import base64
import hashlib
import hmac
import json
import time

import psycopg
import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="
WRONG_SECRET = "whsec_d3Jvbmdzd3Jvbmdzd3Jvbmdzd3Jvbmc="

pytestmark = requires_postgres


def _sign_with(secret: str, payload: bytes, msg_id: str = "msg_1", ts: int | None = None) -> dict:
    ts = ts or int(time.time())
    key = base64.b64decode(secret.removeprefix("whsec_"))
    to_sign = f"{msg_id}.{ts}.".encode() + payload
    sig = base64.b64encode(hmac.new(key, to_sign, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}"}


def _sign(payload: bytes, msg_id: str = "msg_1", ts: int | None = None) -> dict:
    return _sign_with(SECRET, payload, msg_id=msg_id, ts=ts)


def _user_created(user_id="user_abc", email="a@example.org"):
    return {
        "type": "user.created",
        "object": "event",
        "data": {
            "id": user_id,
            "primary_email_address_id": "idn_1",
            "email_addresses": [
                {"id": "idn_2", "email_address": "secondary@example.org"},
                {"id": "idn_1", "email_address": email},
            ],
        },
    }


def _user_deleted(user_id):
    return {
        "type": "user.deleted",
        "object": "event",
        "data": {"id": user_id, "object": "user", "deleted": True},
    }


def _user_updated_unresolvable_primary(user_id):
    """user.updated whose primary_email_address_id matches no entry in
    email_addresses[] -> _primary_email() returns None."""
    return {
        "type": "user.updated",
        "object": "event",
        "data": {
            "id": user_id,
            "primary_email_address_id": "idn_missing",
            "email_addresses": [
                {"id": "idn_1", "email_address": "other@example.org"},
            ],
        },
    }


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_webhooks")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        flask_app = create_app(
            config={
                "TESTING": True,
                "VERIFY_REQUEST": lambda r: None,
                "WEBHOOK_SECRET": SECRET,
            }
        )
        flask_app.config["_TEST_DSN"] = dsn
        yield flask_app
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def test_bad_signature_400(app):
    payload = json.dumps(_user_created()).encode()
    resp = app.test_client().post(
        "/webhooks/clerk",
        data=payload,
        headers={
            "svix-id": "msg_1",
            "svix-timestamp": str(int(time.time())),
            "svix-signature": "v1,bogus",
        },
    )
    assert resp.status_code == 400


def test_user_created_upserts_with_primary_email(app):
    payload = json.dumps(_user_created()).encode()
    resp = app.test_client().post("/webhooks/clerk", data=payload, headers=_sign(payload))
    assert resp.status_code == 200
    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        row = conn.execute("SELECT email FROM users WHERE id = 'user_abc'").fetchone()
    assert row[0] == "a@example.org"  # primary via primary_email_address_id, NOT array order


def test_duplicate_delivery_is_idempotent(app):
    payload = json.dumps(_user_created()).encode()
    client = app.test_client()
    assert client.post("/webhooks/clerk", data=payload, headers=_sign(payload)).status_code == 200
    assert (
        client.post(
            "/webhooks/clerk", data=payload, headers=_sign(payload, msg_id="msg_2")
        ).status_code
        == 200
    )
    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        n = conn.execute("SELECT count(*) FROM users WHERE id = 'user_abc'").fetchone()[0]
    assert n == 1


def test_user_deleted_removes_row(app):
    created = json.dumps(_user_created()).encode()
    client = app.test_client()
    client.post("/webhooks/clerk", data=created, headers=_sign(created))
    deleted = json.dumps(
        {
            "type": "user.deleted",
            "object": "event",
            "data": {"id": "user_abc", "object": "user", "deleted": True},
        }
    ).encode()
    assert (
        client.post(
            "/webhooks/clerk", data=deleted, headers=_sign(deleted, msg_id="msg_3")
        ).status_code
        == 200
    )
    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        n = conn.execute("SELECT count(*) FROM users WHERE id = 'user_abc'").fetchone()[0]
    assert n == 0


def test_user_deleted_cascades_to_all_child_tables(app):
    """C-1 end-to-end: user.deleted must erase every per-user child row —
    profiles/feed_state/watchlists/pipeline_status/byo_key_credentials AND
    events (the C-1-compliance-critical table per this module's docstring),
    not just the users row itself. Converts the docstring's FK-cascade claim
    into an enforced regression test."""
    user_id = "user_c1"
    created = json.dumps(_user_created(user_id=user_id)).encode()
    client = app.test_client()
    assert client.post("/webhooks/clerk", data=created, headers=_sign(created)).status_code == 200

    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES ('C1 Co') RETURNING id"
        ).fetchone()[0]
        posting_id = conn.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES ('c1|posting', %s, 'Engineer', 'C1 Co') RETURNING id",
            (company_id,),
        ).fetchone()[0]
        conn.execute("INSERT INTO profiles (user_id) VALUES (%s)", (user_id,))
        conn.execute(
            "INSERT INTO feed_state (user_id, posting_id) VALUES (%s, %s)",
            (user_id, posting_id),
        )
        conn.execute(
            "INSERT INTO watchlists (user_id, posting_id) VALUES (%s, %s)",
            (user_id, posting_id),
        )
        conn.execute(
            "INSERT INTO pipeline_status (user_id, posting_id, status) VALUES (%s, %s, 'applied')",
            (user_id, posting_id),
        )
        conn.execute(
            "INSERT INTO byo_key_credentials (user_id, provider, encrypted_key) "
            "VALUES (%s, 'openai', %s)",
            (user_id, b"fake-encrypted-key-bytes"),
        )
        conn.execute("INSERT INTO events (user_id, event_type) VALUES (%s, 'view')", (user_id,))
        conn.commit()

    deleted = json.dumps(_user_deleted(user_id)).encode()
    resp = client.post("/webhooks/clerk", data=deleted, headers=_sign(deleted, msg_id="msg_c1_del"))
    assert resp.status_code == 200

    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        for table in (
            "profiles",
            "feed_state",
            "watchlists",
            "pipeline_status",
            "byo_key_credentials",
            "events",
        ):
            n = conn.execute(
                f"SELECT count(*) FROM {table} WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            assert n == 0, f"{table} row survived cascade delete for {user_id}"


def test_user_deleted_enqueues_posthog_purge_with_pseudonym_when_salt_configured(app):
    """#135: the user.deleted cascade (jobcannon.host.user_deletion.
    cascade_delete_user) must defer purge_posthog_person with the user's
    PSEUDONYM, never the raw Clerk id -- and only when analytics
    pseudonymization is configured (posthog_client.pseudonymize's
    fail-closed contract).

    The salt-UNSET case is NOT covered for free by test_user_deleted_
    removes_row/test_user_deleted_cascades_to_all_child_tables above
    (review-3 finding 2/3: this docstring previously claimed it was, which
    was wrong on two counts). First, cascade_delete_user returns right
    after delete_user() when pseudonymize() comes back None -- salt-unset
    never reaches configure_task/.defer() at all, so there is no "stray
    real .defer() attempt" for those tests to incidentally catch. Second,
    even a genuine defer failure wouldn't fail a test loudly any more:
    cascade_delete_user's own except-Exception branch (issue #135/#136
    HIGH-1 fix) logs and swallows it by design, precisely so a PostHog
    outage can never turn a successful deletion into a request failure.
    Salt-unset behavior has its own direct assertion instead --
    test_user_deleted_skips_posthog_purge_when_salt_unset below."""
    from procrastinate import testing

    from jobcannon.host import posthog_client, task_app
    from jobcannon.host.user_deletion import PURGE_POSTHOG_PERSON_TASK

    posthog_client.set_analytics_salt("webhook-test-salt")
    try:
        user_id = "user_posthog_purge"
        created = json.dumps(_user_created(user_id=user_id)).encode()
        client = app.test_client()
        assert (
            client.post("/webhooks/clerk", data=created, headers=_sign(created)).status_code == 200
        )
        expected_pseudonym = posthog_client.pseudonymize(user_id)

        deleted = json.dumps(_user_deleted(user_id)).encode()
        with task_app.app.replace_connector(testing.InMemoryConnector()) as procrastinate_app:
            resp = client.post(
                "/webhooks/clerk", data=deleted, headers=_sign(deleted, msg_id="msg_purge_del")
            )
            assert resp.status_code == 200
            jobs = list(procrastinate_app.connector.jobs.values())

        purge_jobs = [j for j in jobs if j["task_name"] == PURGE_POSTHOG_PERSON_TASK]
        assert len(purge_jobs) == 1
        assert purge_jobs[0]["queue_name"] == "maintenance"
        assert purge_jobs[0]["args"]["distinct_id"] == expected_pseudonym
        assert purge_jobs[0]["args"]["distinct_id"] != user_id

        with psycopg.connect(app.config["_TEST_DSN"]) as conn:
            n = conn.execute("SELECT count(*) FROM users WHERE id = %s", (user_id,)).fetchone()[0]
        assert n == 0
    finally:
        posthog_client.set_analytics_salt(None)


def test_user_deleted_skips_posthog_purge_when_salt_unset(app):
    """The direct counterpart to test_user_deleted_enqueues_posthog_purge_
    with_pseudonym_when_salt_configured above -- and the test that docstring
    used to (wrongly, per review-3 finding 2/3) claim was covered "for free"
    by the plain delete tests. Explicitly unsets the salt (rather than
    relying on it being unset by default) so this assertion can't silently
    start passing for the wrong reason if some other test in the same
    process left a salt configured. Swaps in InMemoryConnector purely so a
    regression that DID start deferring here would be caught locally
    instead of raising AppNotOpen against this fixture's throwaway DB (which
    never applies procrastinate's own schema -- see the `app` fixture
    above)."""
    from procrastinate import testing

    from jobcannon.host import posthog_client, task_app

    posthog_client.set_analytics_salt(None)
    user_id = "user_posthog_purge_no_salt"
    created = json.dumps(_user_created(user_id=user_id)).encode()
    client = app.test_client()
    assert client.post("/webhooks/clerk", data=created, headers=_sign(created)).status_code == 200

    deleted = json.dumps(_user_deleted(user_id)).encode()
    with task_app.app.replace_connector(testing.InMemoryConnector()) as procrastinate_app:
        resp = client.post(
            "/webhooks/clerk", data=deleted, headers=_sign(deleted, msg_id="msg_purge_no_salt_del")
        )
        assert resp.status_code == 200
        jobs = list(procrastinate_app.connector.jobs.values())

    assert jobs == []
    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        n = conn.execute("SELECT count(*) FROM users WHERE id = %s", (user_id,)).fetchone()[0]
    assert n == 0


def test_stale_timestamp_replay_rejected_400(app):
    """A correctly-signed payload over a timestamp outside Svix's ~5-minute
    tolerance window must still be rejected — freshness, not just signature
    validity, is required."""
    payload = json.dumps(_user_created(user_id="user_stale")).encode()
    old_ts = int(time.time()) - 600
    resp = app.test_client().post(
        "/webhooks/clerk", data=payload, headers=_sign(payload, ts=old_ts)
    )
    assert resp.status_code == 400
    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        n = conn.execute("SELECT count(*) FROM users WHERE id = 'user_stale'").fetchone()[0]
    assert n == 0


def test_forged_signature_wrong_secret_rejected_400(app):
    """A well-formed whsec_-shaped signature computed with a DIFFERENT
    secret than the app's must be rejected as forged, not merely as a
    malformed-encoding case — and must not create a user row."""
    payload = json.dumps(_user_created(user_id="user_forged")).encode()
    resp = app.test_client().post(
        "/webhooks/clerk",
        data=payload,
        headers=_sign_with(WRONG_SECRET, payload, msg_id="msg_forged"),
    )
    assert resp.status_code == 400
    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        n = conn.execute("SELECT count(*) FROM users WHERE id = 'user_forged'").fetchone()[0]
    assert n == 0


def test_user_updated_preserves_email_when_primary_unresolvable(app):
    """Pairs with F5: if primary_email_address_id resolves to nothing,
    _primary_email() returns None, and the upsert must not NULL out an
    already-known email."""
    user_id = "user_e5"
    created = json.dumps(_user_created(user_id=user_id, email="known@example.org")).encode()
    client = app.test_client()
    assert client.post("/webhooks/clerk", data=created, headers=_sign(created)).status_code == 200

    updated = json.dumps(_user_updated_unresolvable_primary(user_id)).encode()
    resp = client.post("/webhooks/clerk", data=updated, headers=_sign(updated, msg_id="msg_e5_upd"))
    assert resp.status_code == 200

    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        row = conn.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()
    assert row[0] == "known@example.org"

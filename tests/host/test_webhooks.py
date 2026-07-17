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

pytestmark = requires_postgres


def _sign(payload: bytes, msg_id: str = "msg_1", ts: int | None = None) -> dict:
    ts = ts or int(time.time())
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    to_sign = f"{msg_id}.{ts}.".encode() + payload
    sig = base64.b64encode(hmac.new(key, to_sign, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}"}


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

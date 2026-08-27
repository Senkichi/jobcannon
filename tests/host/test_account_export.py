"""jobcannon/web/export.py — GET /account/export, the authed-only,
read-only self-service data-export route.

Own throwaway database, the same shape as tests/host/test_consent_route.py:
this route reads real, durable rows across profiles/watchlists/
pipeline_status/events/users, so it cannot share the session-scoped
`postgres_test_dsn` every rollback-isolated tests/host/ module reads inside
a transaction.

The cross-user-leakage test (`test_export_contains_only_the_requesting_users_
own_rows`) seeds TWO users against the SAME posting id on purpose: if the
route's `WHERE user_id = %s` filter were ever dropped, both users' watchlist/
pipeline_status rows would be indistinguishable by posting_id alone (they'd
all point at the one shared posting), so only a row-COUNT assertion would
catch the regression — an identity-based assertion (e.g. "no foreign
posting_id present") would pass for the wrong reason under that seeding.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

USER_A = "user_export_a"
USER_B = "user_export_b"

# #105: the exact `profiles` columns get_profile() (jobcannon/db/_profiles.py)
# selects and therefore surfaces in this export, union any column a
# maintainer has explicitly decided to withhold. get_profile()'s explicit
# column list (no more `SELECT *`) means a bare migration can no longer
# silently WIDEN this export -- an unlisted column is simply absent from the
# query result, not a decision anyone made. test_profiles_table_columns_are_
# all_classified_for_export below is what makes that decision unavoidable:
# it compares this pair of sets against postgres's live `profiles` schema,
# so a new column must be added to one of them (and, if exported, to
# get_profile()'s SELECT) before the suite goes green again.
_PROFILE_EXPORT_COLUMNS = frozenset(
    {
        "user_id",
        "skills",
        "experience_summary",
        "target_titles",
        "target_locations",
        "seniority_level",
        "years_of_experience",
        "comp_floor_usd",
        # #169/#170 (m0011): the picker's company/workplace-type selections,
        # same category as target_titles/seniority_level above — a user's
        # own stated preferences, no different a data-minimization call than
        # the columns already exported.
        "target_companies",
        "workplace_type",
        "updated_at",
    }
)
_PROFILE_EXCLUDED_FROM_EXPORT: frozenset[str] = frozenset()


@pytest.fixture()
def app():
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_export_route")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        flask_app = create_app(
            config={
                "TESTING": True,
                "VERIFY_REQUEST": lambda r: None,
                "WEBHOOK_SECRET": "whsec_dGVzdA==",
            }
        )
        flask_app.config["_TEST_DSN"] = dsn
        yield flask_app
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)


def _authed(app, user_id):
    from jobcannon.web.auth import ClerkIdentity

    app.config["VERIFY_REQUEST"] = lambda req: ClerkIdentity(
        user_id=user_id, claims={"sub": user_id}
    )


def _seeded_client(app, dsn, user_id):
    """An authed test client whose user row already exists and whose handoff
    has already run (mirrors tests/host/test_consent_route.py's identical
    helper) — every request it makes lands directly on the route under
    test, not on the one-time post-handoff redirect."""
    from jobcannon.web.handoff import _HANDOFF_DONE_KEY

    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
        conn.commit()

    _authed(app, user_id)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    return client


def _shared_posting(dsn) -> int:
    """One company + one posting, shared by both seeded users below — the
    adversarial shape the leakage test needs (see module docstring)."""
    with psycopg.connect(dsn) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES ('Shared Co') RETURNING id"
        ).fetchone()[0]
        posting_id = conn.execute(
            "INSERT INTO postings (dedup_key, company_id, title, company) "
            "VALUES ('shared-dedup', %s, 'Engineer', 'Shared Co') RETURNING id",
            (company_id,),
        ).fetchone()[0]
        conn.commit()
        return posting_id


def _seed_full_account(dsn, user_id, posting_id) -> None:
    """A profile (with a numeric `years_of_experience`, so the Decimal ->
    JSON path is actually exercised), a watchlist entry, a pipeline_status
    row, a consent grant, and one extra plain event — all against the SAME
    `posting_id` every other seeded user in this module also uses."""
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
        conn.execute(
            "INSERT INTO profiles (user_id, experience_summary, years_of_experience, comp_floor_usd) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, f"{user_id} summary", 4.5, 120000),
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
            "INSERT INTO events (user_id, event_type, payload) VALUES (%s, 'consent_recorded', %s)",
            (
                user_id,
                json.dumps(
                    {
                        "consent_type": "analytics",
                        "granted": True,
                        "consent_version": "v1",
                        "consented_at": "2026-08-01T00:00:00.000000Z",
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO events (user_id, event_type, posting_id) VALUES (%s, 'posting_saved', %s)",
            (user_id, posting_id),
        )
        conn.commit()


def test_export_requires_auth(app):
    client = app.test_client()
    assert client.get("/account/export").status_code == 401


def test_export_returns_json_attachment_with_date_stamped_filename(app):
    dsn = app.config["_TEST_DSN"]
    posting_id = _shared_posting(dsn)
    _seed_full_account(dsn, USER_A, posting_id)
    client = _seeded_client(app, dsn, USER_A)

    resp = client.get("/account/export")

    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    disposition = resp.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="jobcannon-account-export-' in disposition
    assert disposition.strip().endswith('.json"')

    doc = resp.get_json()
    assert doc["schema_version"]
    assert doc["generated_at"]
    assert doc["user_id"] == USER_A


def test_export_contains_only_the_requesting_users_own_rows(app):
    """Load-bearing: two users, seeded against the identical posting id, so
    only a row-count assertion (not an identity comparison) can catch a
    dropped user_id filter. See module docstring."""
    dsn = app.config["_TEST_DSN"]
    posting_id = _shared_posting(dsn)
    _seed_full_account(dsn, USER_A, posting_id)
    _seed_full_account(dsn, USER_B, posting_id)
    client = _seeded_client(app, dsn, USER_A)

    doc = client.get("/account/export").get_json()

    assert doc["user_id"] == USER_A
    assert doc["profile"]["experience_summary"] == f"{USER_A} summary"
    assert doc["profile"]["years_of_experience"] == 4.5
    assert doc["profile"]["comp_floor_usd"] == 120000

    assert len(doc["watchlist"]) == 1
    assert doc["watchlist"][0]["posting_id"] == posting_id

    assert len(doc["pipeline_status"]) == 1
    assert doc["pipeline_status"][0]["posting_id"] == posting_id
    assert doc["pipeline_status"][0]["status"] == "applied"

    # Exactly A's two seeded events (consent_recorded + posting_saved) —
    # never B's, even though B's rows reference the identical posting_id.
    assert len(doc["events"]) == 2
    assert {e["event_type"] for e in doc["events"]} == {"consent_recorded", "posting_saved"}


def test_export_consent_record_carries_granted_version_and_timestamp(app):
    dsn = app.config["_TEST_DSN"]
    posting_id = _shared_posting(dsn)
    _seed_full_account(dsn, USER_A, posting_id)
    client = _seeded_client(app, dsn, USER_A)

    doc = client.get("/account/export").get_json()

    consent = doc["consent"]
    assert consent is not None
    assert consent["granted"] is True
    assert consent["consent_version"] == "v1"
    assert consent["consented_at"]


def test_export_document_pins_expected_key_sets(app):
    """Shape-pinning test for #105: every reader this export document is
    built from (`get_profile`, `list_watchlist_entries`,
    `list_pipeline_status_entries`, `read_latest_consent_record`,
    `list_events_for_user`) now uses an explicit column list rather than
    `SELECT *`, so the document's shape can only change via a deliberate
    code edit to one of those readers or to `_build_export_document` itself
    — this test pins that shape exactly (not a subset check) so such an
    edit fails loudly here rather than passing quietly with a field no
    reviewer connected back to this export. It does NOT, by itself, catch a
    `profiles` migration that adds a column nobody wired up anywhere —
    `test_profiles_table_columns_are_all_classified_for_export` below is
    the test that forces that decision.
    """
    dsn = app.config["_TEST_DSN"]
    posting_id = _shared_posting(dsn)
    _seed_full_account(dsn, USER_A, posting_id)
    client = _seeded_client(app, dsn, USER_A)

    doc = client.get("/account/export").get_json()

    assert set(doc.keys()) == {
        "schema_version",
        "generated_at",
        "user_id",
        "identity",
        "profile",
        "watchlist",
        "pipeline_status",
        "consent",
        "events",
    }

    # No CLERK_CLIENT configured (this module's `app` fixture never sets
    # one, matching fetch_primary_email's fail-soft "unconfigured" branch)
    # -- covered for real by tests/host/test_clerk_email_lookup.py and the
    # identity-specific tests below, this just pins the shape.
    assert set(doc["identity"].keys()) == {
        "email",
        "email_verified",
        "source",
        "fetched_at",
        "email_unavailable_reason",
    }
    assert doc["identity"]["source"] == "clerk"
    assert doc["identity"]["email"] is None
    assert doc["identity"]["email_unavailable_reason"] == "clerk_client_unavailable"

    assert set(doc["profile"].keys()) == {
        "user_id",
        "skills",
        "experience_summary",
        "target_titles",
        "target_locations",
        "seniority_level",
        "years_of_experience",
        "comp_floor_usd",
        "target_companies",
        "workplace_type",
        "updated_at",
    }

    assert len(doc["watchlist"]) == 1
    assert set(doc["watchlist"][0].keys()) == {
        "id",
        "posting_id",
        "company_id",
        "notes",
        "created_at",
    }

    assert len(doc["pipeline_status"]) == 1
    assert set(doc["pipeline_status"][0].keys()) == {
        "posting_id",
        "status",
        "status_changed_at",
        "applied_at",
        "notes",
    }

    assert set(doc["consent"].keys()) == {
        "consent_type",
        "granted",
        "consent_version",
        "consented_at",
    }

    assert len(doc["events"]) == 2
    assert set(doc["events"][0].keys()) == {
        "id",
        "event_type",
        "posting_id",
        "feed_position",
        "ranker_version",
        "feed_session_id",
        "interleave_experiment_id",
        "interleave_team",
        "occurred_at",
        "payload",
    }


def test_profiles_table_columns_are_all_classified_for_export(app):
    """Live-schema check for #105: get_profile()'s explicit column list
    closes off `SELECT *`-style widening, but on its own it would let a
    future `profiles` migration add a column that simply never appears
    anywhere -- not exported, not deliberately excluded, just unaddressed.
    This queries postgres's actual `information_schema.columns` for
    `profiles` and requires every column to be accounted for in exactly one
    of the two sets above. Add a column to `profiles` and this test fails
    until a maintainer places it in `_PROFILE_EXPORT_COLUMNS` (and adds it
    to get_profile()'s SELECT) or in `_PROFILE_EXCLUDED_FROM_EXPORT` with a
    reason -- the "add-to-export vs exclude" decision issue #105 asked for.
    """
    dsn = app.config["_TEST_DSN"]
    with psycopg.connect(dsn) as conn:
        live_columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'profiles'"
            ).fetchall()
        }

    assert live_columns == _PROFILE_EXPORT_COLUMNS | _PROFILE_EXCLUDED_FROM_EXPORT


def test_export_for_user_with_no_data_still_produces_valid_document(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn, "user_export_empty")

    resp = client.get("/account/export")

    assert resp.status_code == 200
    doc = resp.get_json()
    assert doc["schema_version"]
    assert doc["generated_at"]
    assert doc["user_id"] == "user_export_empty"
    assert doc["profile"] is None
    assert doc["watchlist"] == []
    assert doc["pipeline_status"] == []
    assert doc["consent"] is None
    assert doc["events"] == []


# --- issue #181: the `identity` section's Clerk lookup ---------------------
#
# Mocked at the HTTP boundary (a fake `.users.get(...)`), never at
# `fetch_primary_email`/`_build_export_document` themselves, so the route's
# real wiring — calling fetch_primary_email before the DB connection opens,
# threading its result into `_build_export_document`, and that function's
# own extraction/fail-soft logic — all runs for real here. Unit coverage of
# fetch_primary_email's own classification branches (timeout/404/5xx/no-
# primary-address/...) lives in tests/host/test_clerk_email_lookup.py; these
# tests are the route-level integration on top of it.


class _FakeUsersGet:
    def __init__(self, *, result=None, error=None):
        self._result = result
        self._error = error

    def get(self, *, user_id, timeout_ms=None, retries=None):
        if self._error is not None:
            raise self._error
        return self._result


class _FakeClerkClient:
    def __init__(self, users):
        self.users = users


class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"simulated Clerk {status_code}")
        self.status_code = status_code


class _TimeoutError(Exception):
    """Named to mirror httpx's *Timeout* exception family that a real Clerk
    timeout raises — auth.py's `_clerk_failure_reason` classifies off the
    exception's class name for the no-status-code case."""


def _clerk_user(primary_id, email_address, verified):
    from types import SimpleNamespace

    return SimpleNamespace(
        primary_email_address_id=primary_id,
        email_addresses=[
            SimpleNamespace(
                id=primary_id,
                email_address=email_address,
                verification=SimpleNamespace(status="verified" if verified else "unverified"),
            )
        ],
    )


def test_export_identity_carries_email_from_clerk(app):
    dsn = app.config["_TEST_DSN"]
    posting_id = _shared_posting(dsn)
    _seed_full_account(dsn, USER_A, posting_id)
    client = _seeded_client(app, dsn, USER_A)
    app.config["CLERK_CLIENT"] = _FakeClerkClient(
        _FakeUsersGet(result=_clerk_user("addr_1", "a@example.com", True))
    )

    doc = client.get("/account/export").get_json()

    assert doc["identity"] == {
        "email": "a@example.com",
        "email_verified": True,
        "source": "clerk",
        "fetched_at": doc["generated_at"],
        "email_unavailable_reason": None,
    }


def test_export_identity_degrades_on_clerk_timeout_without_500(app):
    dsn = app.config["_TEST_DSN"]
    posting_id = _shared_posting(dsn)
    _seed_full_account(dsn, USER_A, posting_id)
    client = _seeded_client(app, dsn, USER_A)
    app.config["CLERK_CLIENT"] = _FakeClerkClient(_FakeUsersGet(error=_TimeoutError("timed out")))

    resp = client.get("/account/export")

    assert resp.status_code == 200
    doc = resp.get_json()
    assert doc["identity"]["email"] is None
    assert doc["identity"]["email_verified"] is None
    assert doc["identity"]["email_unavailable_reason"] == "clerk_timeout"
    # The rest of the document is unaffected by the Clerk failure.
    assert doc["profile"]["experience_summary"] == f"{USER_A} summary"


def test_export_identity_degrades_on_clerk_5xx_without_500(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn, "user_export_5xx")
    app.config["CLERK_CLIENT"] = _FakeClerkClient(_FakeUsersGet(error=_StatusError(503)))

    resp = client.get("/account/export")

    assert resp.status_code == 200
    doc = resp.get_json()
    assert doc["identity"]["email"] is None
    assert doc["identity"]["email_unavailable_reason"] == "clerk_api_error_503"


def test_export_identity_degrades_on_clerk_404_without_500(app):
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn, "user_export_404")
    app.config["CLERK_CLIENT"] = _FakeClerkClient(_FakeUsersGet(error=_StatusError(404)))

    resp = client.get("/account/export")

    assert resp.status_code == 200
    doc = resp.get_json()
    assert doc["identity"]["email"] is None
    assert doc["identity"]["email_unavailable_reason"] == "clerk_user_not_found"


def test_export_identity_degrades_when_clerk_client_is_unconfigured(app):
    """The `app` fixture never sets CLERK_CLIENT — mirrors a TESTING app
    that never built one (jobcannon/web/__init__.py leaves it None), the
    same state test_export_document_pins_expected_key_sets already pins."""
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn, "user_export_no_client")

    doc = client.get("/account/export").get_json()

    assert doc["identity"]["email"] is None
    assert doc["identity"]["email_unavailable_reason"] == "clerk_client_unavailable"


def test_export_document_never_contains_the_configured_clerk_secret(app):
    """Regression guard for issue #181's "never log the key": the export
    route reuses app.config["CLERK_CLIENT"] (an already-built client) and
    never reads HOST_CONFIG.clerk_secret_key directly, so that value must
    never reach the response body. A positive control proves the substring
    search itself works (an absent string proves nothing on its own —
    see the negative-result verification rule): the same search DOES find
    the secret in a document doctored to contain it.
    """
    from jobcannon.host.config import HostConfig

    fake_secret = "sk_test_FAKE_MUST_NEVER_LEAK_9f3ab21c"
    app.config["HOST_CONFIG"] = HostConfig(database_url="", clerk_secret_key=fake_secret)
    dsn = app.config["_TEST_DSN"]
    client = _seeded_client(app, dsn, "user_export_secret_check")
    app.config["CLERK_CLIENT"] = _FakeClerkClient(
        _FakeUsersGet(result=_clerk_user("addr_1", "a@example.com", True))
    )

    resp = client.get("/account/export")

    body = resp.get_data(as_text=True)
    assert fake_secret not in body

    # Positive control: the identical search DOES find the secret when a
    # document actually contains it, so the assertion above isn't passing
    # because the search is broken.
    doctored = body + fake_secret
    assert fake_secret in doctored

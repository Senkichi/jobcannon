"""jobcannon/web/export.py — GET /account/export: the authed-only, read-only
self-service data-export route (closes the "No data-export route for an
authenticated user's own data" issue).

Top-level `identity` section (issue #181): this app deliberately stores no
email server-side (Clerk holds it — see jobcannon/web/auth.py's module
docstring), so the export's only source for the account's own email is a
live Clerk Backend API call, `jobcannon.web.auth.fetch_primary_email`,
reusing app.config["CLERK_CLIENT"] the same way jobcannon/web/account.py's
delete route does. That call happens BEFORE `connection_factory()` opens a
pooled connection below — it can take up to ~5s (its own timeout bound) and
must not hold a DB connection idle for that long. It is fail-soft by
design (see fetch_primary_email's docstring): Clerk being unreachable,
slow, or returning 4xx/5xx degrades `identity.email` to null with
`identity.email_unavailable_reason` set, never a 500.

Ships ONE JSON document, served as a file download (`Content-Disposition:
attachment`, filename carrying a date stamp), joining every per-user row
this product stores under the requesting Clerk user id: profile
(`jobcannon.db._profiles.get_profile`, the existing single reader), watchlist
entries and pipeline status (both read via new narrow functions in
`jobcannon.db._user_actions` — the single WRITER module for both tables per
its own docstring, so a new reader belongs there too, not in
`jobcannon.db._feed`, which only reads them incidentally to build the feed
join), the user's consent record (the payload of their most recent
`consent_recorded` event — `jobcannon.db._events.read_latest_consent_record`,
new), and the user's own `events` rows
(`jobcannon.db._events.list_events_for_user`, new). Both `_events.py`
additions are plain SELECT statements — never a write — so neither trips
`tests/host/test_events_single_writer.py`'s AST guard, which is scoped to
writes only.

Not listed in `jobcannon.web.PUBLIC_PATHS`, so `jobcannon/web/__init__.py`'s
`before_request` gate already 401s an unauthenticated request before this
module is ever reached — no separate auth check needed here.

Read-only handler: no migration, and the handler itself performs no write
(app-level `before_request` hooks may still write — session-id provisioning
and the signup handoff run before any authed route, this one included).
A brand-new user with no profile/watchlist/pipeline/consent/events
rows still gets a valid document — `profile` and `consent` degrade to
`null`, the list sections degrade to `[]` — rather than raising or omitting
a key, so the empty state is a real, always-producible shape.

`generated_at` is read from the database's own clock
(`jobcannon.db._events.db_now_iso`, the same helper `jobcannon/web/consent.py`
already uses for `consented_at`) rather than a Python wall-clock call,
matching this codebase's no-process-clock-in-persisted-or-authoritative-
timestamps convention. The download filename's date stamp is sliced from
that SAME value (`generated_at[:10]`) instead of a second, independent clock
read, so the two can never disagree.
"""

from __future__ import annotations

import datetime
import decimal
import json
from typing import Any

from flask import Blueprint, Response, current_app, g

from jobcannon.db._events import db_now_iso, list_events_for_user, read_latest_consent_record
from jobcannon.db._profiles import get_profile
from jobcannon.db._user_actions import list_pipeline_status_entries, list_watchlist_entries
from jobcannon.db.pool import connection_factory
from jobcannon.web.auth import ClerkEmailLookup, fetch_primary_email

export_bp = Blueprint("export", __name__)

# Bumped whenever the document's top-level shape changes (a key added,
# removed, or renamed) — NOT on every route change. Exported so a future
# consumer of this document (or a test) can import the literal instead of
# retyping it.
SCHEMA_VERSION = "1"


def _row_to_dict(row: Any) -> dict[str, Any]:
    """`HybridRow` (jobcannon/db/rows.py) is a `Sequence`, not a `Mapping` —
    a bare `dict(row)` would try to unpack each element as a 2-tuple and
    raise. `row.keys()` is the STRING-KEY access every DAL module in this
    package already relies on (see e.g. jobcannon/db/_feed.py's module
    docstring), so this works identically for the pooled `HybridRow` and the
    test fixtures' `dict_row` (already a plain dict, for which this is a
    no-op copy)."""
    return {key: row[key] for key in row.keys()}


def _json_default(value: Any) -> Any:
    """`json.dumps`'s `default=` hook for the two non-JSON-native types
    these tables can hand back: `timestamptz` columns (`watchlists.created_at`,
    `pipeline_status.status_changed_at`/`applied_at`, `events.occurred_at`)
    surface as `datetime.datetime`/`datetime.date`, and `profiles`'
    `numeric` `years_of_experience` surfaces as `decimal.Decimal`. `jsonb`
    columns (`skills`, `target_titles`, `payload`, ...) are already plain
    dict/list/str/bool/None by the time psycopg hands them back, so they
    never reach this hook."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _build_export_document(
    conn: Any, user_id: str, email_lookup: ClerkEmailLookup
) -> dict[str, Any]:
    profile = get_profile(conn, user_id)
    generated_at = db_now_iso(conn)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "user_id": user_id,
        "identity": {
            "email": email_lookup.email,
            "email_verified": email_lookup.email_verified,
            "source": "clerk",
            # Same clock read as generated_at, not a second one: the Clerk
            # lookup and the rest of this document are produced by the same
            # request, so there is nothing for a separate wall-clock call to
            # measure that generated_at doesn't already capture.
            "fetched_at": generated_at,
            "email_unavailable_reason": email_lookup.unavailable_reason,
        },
        "profile": _row_to_dict(profile) if profile is not None else None,
        "watchlist": [_row_to_dict(r) for r in list_watchlist_entries(conn, user_id)],
        "pipeline_status": [_row_to_dict(r) for r in list_pipeline_status_entries(conn, user_id)],
        "consent": read_latest_consent_record(conn, user_id),
        "events": [_row_to_dict(r) for r in list_events_for_user(conn, user_id)],
    }


@export_bp.get("/account/export", strict_slashes=False)
def export_account_data():
    user_id = g.clerk_user.user_id
    # Fetched BEFORE the DB connection is checked out below — this call can
    # take up to ~5s (its own timeout bound) and must not hold a pooled
    # connection idle for that long. See fetch_primary_email's docstring.
    email_lookup = fetch_primary_email(current_app.config.get("CLERK_CLIENT"), user_id)
    with connection_factory() as conn:
        document = _build_export_document(conn, user_id, email_lookup)

    body = json.dumps(document, default=_json_default, indent=2)
    date_stamp = document["generated_at"][:10]  # "YYYY-MM-DD" prefix of db_now_iso's output
    filename = f"jobcannon-account-export-{date_stamp}.json"
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

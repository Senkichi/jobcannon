# PORTED from tests/test_jobs_location_filters.py @ 25cdb667987701f4472a4f3c2d180dd211786979 (private job-cannon). Ledger L-0496.
"""Commit D smoke tests: country + workplace_type dropdowns + filter routing.

# PORT-SEAM: only test_get_filtered_jobs_workplace_type_filter is carried.
# Dropped, and why:
# - test_get_distinct_country_codes_* / test_get_distinct_workplace_types_*
#   / test_get_filtered_jobs_country_* / *_combine: this host's
#   jobcannon/db/_feed.py:list_feed_postings has no `country` kwarg and
#   there is no get_distinct_country_codes/get_distinct_workplace_types
#   counterpart anywhere publicly -- the country-code feature was never
#   ported (per this row's own ledger evidence: only _queries.py's
#   workplace_type kwarg is named as the carry target).
# - test_jobs_index_* / test_format_canonical_location_filter_* /
#   *_honesty_callout_*: hit the private app/client Flask fixtures and the
#   /jobs route + format_canonical_location Jinja filter, none of which
#   exist publicly (this row's own reason field calls the /jobs-route
#   assertions "the DIES blueprints/jobs.py layer").
# - test_get_filtered_jobs_workplace_type_invalid_is_ignored: NOT a
#   schema/fixture translation gap like the others above -- a genuine,
#   documented architectural difference. Private's get_filtered_jobs
#   validates workplace_type against a four-value enum allowlist at READ
#   time and silently ignores an out-of-enum value. This host's
#   jobcannon/db/_feed.py:_build_filters applies workplace_type as a bare
#   `p.workplace_type = %s` equality clause with no allowlist check at all
#   -- jobcannon/db/migrations/m0012_profiles_companies_workplace_type.py's
#   own docstring is authoritative on why: "workplace_type is a bare
#   nullable text column, deliberately WITHOUT a CHECK constraint... despite
#   both columns holding the same closed set of uppercase tokens...
#   Validation lives once, in code, at the write boundary
#   (jobcannon/web/onboarding.py's WORKPLACE_TYPES / _WORKPLACE_FILTERS)."
#   So passing an out-of-enum value here does not raise, but it also is not
#   silently ignored the way private's test asserts -- it becomes a normal
#   equality filter that (correctly) matches zero rows, since no row can
#   ever be written with that value. The two behaviors diverge on what the
#   *query itself* claims for garbage input (private: "value ignored, full
#   set returned"; here: "value applied literally, empty set returned"),
#   so this test is dropped rather than adapted to a different assertion
#   that would silently paper over the divergence.
"""

from jobcannon.db._feed import list_feed_postings  # PORT-SEAM: private's __future__ import dropped.

# PORT-SEAM: db_conn/postgres_test_dsn/requires_postgres imported directly
# from tests.host.conftest -- no root tests/conftest.py exists to make
# tests/host/'s fixtures visible outside that subtree.
from tests.host.conftest import db_conn, postgres_test_dsn, requires_postgres  # noqa: F401

pytestmark = requires_postgres  # PORT-SEAM: replaces private's `import pytest` + `from job_finder.db import (get_distinct_country_codes, get_distinct_workplace_types, get_filtered_jobs)` block (all dropped, see module docstring).


def _insert_company(conn, name):
    # PORT-SEAM: companies.id is a real bigserial PK + postings.company_id
    # is a real FK on this host (unlike private's untyped sqlite3 jobs
    # table), so every posting row needs a real companies row first (idiom
    # matches tests/host/test_feed_dal.py's own _seed_company).
    row = conn.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    return row["id"]


def _insert_posting(conn, dedup_key, company_id, workplace_type):
    # PORT-SEAM: private's _seed_job() inserted into jobs(...) with 13
    # columns (locations_structured/primary_country_code/pipeline_status
    # included, none of which matter for this one kept test); this host's
    # postings(...) needs only the columns this test actually exercises,
    # plus the required company_id FK (idiom matches
    # tests/host/test_feed_dal.py's own _seed_posting).
    return conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company, location, workplace_type)"
        " VALUES (%s, %s, 'Senior Engineer', 'Acme', 'San Francisco, CA', %s) RETURNING id",
        (dedup_key, company_id, workplace_type),
    ).fetchone()["id"]


# PORT-SEAM: private's app fixture -> db_conn; get_filtered_jobs -> list_feed_postings.
def test_get_filtered_jobs_workplace_type_filter(db_conn):  # noqa: F811
    """`workplace_type='REMOTE'` returns only REMOTE rows."""
    # PORT-SEAM: private opened its own sqlite3.connect(app.config["DB_PATH"])
    # directly, bypassing whatever fixture handed `app` to the test; this
    # host's db_conn IS the connection, and get_filtered_jobs is renamed
    # list_feed_postings (jobcannon/db/_feed.py).
    conn = db_conn
    company_id = _insert_company(conn, "wt-filter-co")
    r1 = _insert_posting(conn, "r1", company_id, "REMOTE")
    _insert_posting(conn, "h1", company_id, "HYBRID")
    rows = list_feed_postings(conn, workplace_type="REMOTE")
    # PORT-SEAM: private asserted on the `dedup_key` column, which this
    # host's list_feed_postings does not select (jobcannon/db/_feed.py's
    # _SELECT_COLUMNS omits it -- it selects `p.id` instead); `id` serves
    # the same "which row(s) matched" purpose here.
    assert {r["id"] for r in rows} == {r1}

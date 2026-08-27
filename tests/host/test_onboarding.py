"""Picker-first onboarding: GET/POST /start (jobcannon/web/onboarding.py).

Own throwaway database, same shape as tests/host/test_webhooks.py: POST
/start does real, durable INSERT/UPSERT writes on users/profiles, so this
module cannot share the session-scoped postgres_test_dsn every other
tests/host/ module reads inside a rollback-isolated transaction.
"""

from __future__ import annotations

import random
import string

import psycopg
import pytest
from psycopg.rows import dict_row

from jobcannon.web.onboarding import (
    MAX_COMP_FLOOR_USD,
    MAX_COMPANIES_PER_SELECTION,
    MAX_COMPANY_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_TITLES_PER_SELECTION,
    SKILLS_OPTIONS,
    _parse_submission,
)
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

pytestmark = requires_postgres

# RFC 6265's commonly-enforced per-cookie ceiling: browsers may silently drop
# a Set-Cookie header past this size. The picker's whole selection round-trips
# through a signed Flask session cookie (jobcannon/web/anon_session.py's
# set_pending_picker), so a boundary-case submission that writes to Postgres
# successfully but blows this budget would still break /preview for a real
# visitor — see MAX_TITLES_PER_SELECTION's module comment on onboarding.py.
_COMMON_BROWSER_COOKIE_LIMIT = 4093


def _non_repeating_title(seed: int, length: int) -> str:
    """Deterministic but non-repeating text. A single repeated character
    compresses to almost nothing under itsdangerous's zlib session encoding,
    which would make a cookie-size assertion measure the wrong thing — the
    caps were sized against realistic, low-redundancy text (see
    MAX_TITLES_PER_SELECTION's module comment), not a degenerate input."""
    rng = random.Random(seed)
    alphabet = string.ascii_letters + " "
    return "".join(rng.choice(alphabet) for _ in range(length))


def _non_repeating_hostname(seed: int, length: int) -> str:
    """Same non-repeating-text rationale as _non_repeating_title, restricted
    to characters a real hostname can carry (urlsplit(referrer).hostname
    lowercases and only ever yields [a-z0-9-.])."""
    rng = random.Random(seed)
    alphabet = string.ascii_lowercase + string.digits
    return "".join(rng.choice(alphabet) for _ in range(length))


SEEDED_COMPANY = "Onboarding Test Co"


def _seed_postings(dsn: str, titles: list[str]) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        company_id = conn.execute(
            "INSERT INTO companies (name) VALUES (%s) RETURNING id", (SEEDED_COMPANY,)
        ).fetchone()[0]
        for i, title in enumerate(titles):
            conn.execute(
                "INSERT INTO postings (dedup_key, company_id, title, company) "
                "VALUES (%s, %s, %s, %s)",
                (f"onboarding-{i}", company_id, title, SEEDED_COMPANY),
            )


def _anon_user_count(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT count(*) FROM users WHERE id LIKE 'anon\\_%' ESCAPE '\\'"
        ).fetchone()[0]


def _profile_row_for_anon(dsn: str):
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT p.* FROM profiles p JOIN users u ON u.id = p.user_id WHERE u.id LIKE 'anon\\_%' ESCAPE '\\'"
        ).fetchone()


@pytest.fixture()
def app():
    """Own throwaway database — see module docstring."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    dsn, db_name = create_throwaway_db("jobcannon_onboarding")
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


def test_start_is_reachable_unauthenticated(app):
    """Today every non-PUBLIC_PATHS path 401s (jobcannon/web/__init__.py) —
    /start must be exempt and render, with no Clerk credentials at all."""
    resp = app.test_client().get("/start")
    assert resp.status_code == 200
    assert "Tell us what you're looking for" in resp.get_data(as_text=True)


def test_picker_options_come_from_the_corpus_not_a_literal_list(app):
    """Positive control: a distinctively-named seeded posting title AND its
    company must both appear in the rendered picker, proving both option
    lists round-trip through the corpus query rather than a hardcoded list
    (which would never contain either string)."""
    _seed_postings(app.config["_TEST_DSN"], ["Distinctive Corpus Title Alpha"])

    html = app.test_client().get("/start").get_data(as_text=True)

    assert "Distinctive Corpus Title Alpha" in html
    assert SEEDED_COMPANY in html


def test_picker_submit_writes_profile_row_through_upsert_profile(app):
    """A stranger completes the picker without supplying any personal data:
    after one anonymous POST /start, a profiles row exists, and no
    email/name/free-text field was supplied or stored."""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "titles": ["Engineer"],
            "skills": ["python", "sql"],
            "seniority_level": "senior",
            "years_of_experience": "5",
            "workplace_type": "remote",
        },
    )
    assert resp.status_code in (302, 303)

    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row is not None
    assert row["seniority_level"] == "senior"
    assert sorted(row["skills"]) == ["python", "sql"]

    with psycopg.connect(app.config["_TEST_DSN"]) as conn:
        email = conn.execute(
            "SELECT email FROM users WHERE id LIKE 'anon\\_%' ESCAPE '\\'"
        ).fetchone()[0]
    assert email is None

    # The session must carry a token postings.workplace_type can actually
    # equal (uppercase, per jobcannon/engine/location_canonical.py's
    # WorkplaceType), not the form's lowercase "remote" verbatim — a later
    # PR wires this session value straight into a `= %s` filter with no
    # case-folding on either side.
    with client.session_transaction() as sess:
        assert sess["pending_picker"]["workplace_type"] == "REMOTE"


def test_picker_submit_mints_exactly_one_anon_user_row(app):
    client = app.test_client()
    client.post(
        "/start",
        data={"seniority_level": "senior", "years_of_experience": "5", "workplace_type": "any"},
    )
    assert _anon_user_count(app.config["_TEST_DSN"]) == 1


def test_repeat_submit_reuses_the_same_anon_id(app):
    client = app.test_client()
    client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})
    client.post("/start", data={"seniority_level": "staff", "workplace_type": "any"})

    assert _anon_user_count(app.config["_TEST_DSN"]) == 1
    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row["seniority_level"] == "staff"


def test_target_locations_and_experience_summary_are_not_written(app):
    client = app.test_client()
    client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})

    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row["target_locations"] is None
    assert row["experience_summary"] is None


def test_invalid_seniority_rerenders_form_without_writing(app):
    client = app.test_client()
    resp = client.post("/start", data={"seniority_level": "bogus", "workplace_type": "any"})

    assert resp.status_code == 200
    assert "unrecognized seniority level" in resp.get_data(as_text=True)
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0


def test_failed_submit_leaves_no_orphan_anon_user_row(app, monkeypatch):
    """mint_anon_user and upsert_profile must share one transaction, not one
    connection with two independent commits: if the second write raises, the
    first must roll back too, or a stranger's retry mints a second anon row
    forever (falsifying test_picker_submit_mints_exactly_one_anon_user_row on
    the failure path)."""

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure between mint and upsert")

    monkeypatch.setattr("jobcannon.web.onboarding.upsert_profile", _boom)

    client = app.test_client()
    with pytest.raises(RuntimeError, match="simulated failure between mint and upsert"):
        client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})

    assert _anon_user_count(app.config["_TEST_DSN"]) == 0


def test_oversized_title_count_rerenders_without_writing(app):
    """issue #54: a submission with more title selections than a real
    visitor could ever check in the rendered picker (MAX_TITLES_PER_SELECTION)
    must re-render with 200 and write neither a users nor a profiles row —
    same pattern as the existing enum/range failures above."""
    client = app.test_client()
    titles = [f"Title {i}" for i in range(MAX_TITLES_PER_SELECTION + 1)]
    resp = client.post(
        "/start",
        data={"titles": titles, "seniority_level": "mid", "workplace_type": "any"},
    )

    assert resp.status_code == 200
    assert "too many titles selected" in resp.get_data(as_text=True)
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0
    assert _profile_row_for_anon(app.config["_TEST_DSN"]) is None


def test_oversized_title_length_rerenders_without_writing(app):
    """issue #54: a single title longer than MAX_TITLE_LENGTH (e.g. an
    arbitrary pasted text blob) must be rejected before it reaches
    upsert_profile, not silently truncated."""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "titles": ["x" * (MAX_TITLE_LENGTH + 1)],
            "seniority_level": "mid",
            "workplace_type": "any",
        },
    )

    assert resp.status_code == 200
    assert f"{MAX_TITLE_LENGTH}-character limit" in resp.get_data(as_text=True)
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0
    assert _profile_row_for_anon(app.config["_TEST_DSN"]) is None


def test_title_with_control_character_rerenders_without_writing(app):
    """issue #54's explicit proposal: reject values containing control
    characters (e.g. an embedded bell/escape byte), not just long ones."""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "titles": ["Engineer\x07"],
            "seniority_level": "mid",
            "workplace_type": "any",
        },
    )

    assert resp.status_code == 200
    assert "invalid (control) characters" in resp.get_data(as_text=True)
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0
    assert _profile_row_for_anon(app.config["_TEST_DSN"]) is None


def test_non_string_title_is_rejected_by_type_check():
    """Werkzeug's real form MultiDict.getlist() always returns str (no HTTP
    form encoding can carry a non-str value), so the type check has no
    reachable path through a genuine POST. Exercise it the same way
    start_submit() invokes _parse_submission, against a minimal form
    double, to cover the branch directly."""

    class _NonStringValueForm:
        def get(self, key, default=None):
            return {"seniority_level": "mid", "workplace_type": "any"}.get(key, default)

        def getlist(self, key):
            return [123] if key == "titles" else []

    selections, error = _parse_submission(_NonStringValueForm())

    assert selections is None
    assert error == "titles must be text values"


def test_title_selections_at_the_cap_boundary_write_successfully(app):
    """Happy path at both boundaries inclusive: MAX_TITLES_PER_SELECTION
    titles, each exactly MAX_TITLE_LENGTH characters, must still succeed —
    the caps reject strictly-over, not at-the-limit, submissions. Also the
    regression guard for the caps' actual sizing rationale: this exact
    boundary case must fit in one session cookie (see
    _COMMON_BROWSER_COOKIE_LIMIT above and MAX_TITLES_PER_SELECTION's
    module comment) — a cap that lets Postgres accept a submission the
    visitor's own browser then silently drops is still broken."""
    client = app.test_client()
    titles = [_non_repeating_title(i, MAX_TITLE_LENGTH) for i in range(MAX_TITLES_PER_SELECTION)]
    resp = client.post(
        "/start",
        data={"titles": titles, "seniority_level": "mid", "workplace_type": "any"},
    )

    assert resp.status_code in (302, 303)
    cookie_bytes = sum(len(h) for h in resp.headers.get_all("Set-Cookie"))
    assert cookie_bytes < _COMMON_BROWSER_COOKIE_LIMIT, (
        f"session cookie ({cookie_bytes}B) exceeds the common browser per-cookie limit"
    )

    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row is not None
    assert len(row["target_titles"]) == MAX_TITLES_PER_SELECTION
    assert all(len(t) == MAX_TITLE_LENGTH for t in row["target_titles"])


def test_oversized_company_count_rerenders_without_writing(app):
    """issue #80: a submission with more company selections than a real
    visitor could ever check in the rendered picker (MAX_COMPANIES_PER_SELECTION)
    must re-render with 200 and write neither a users nor a profiles row —
    same pattern as the title count cap above."""
    client = app.test_client()
    companies = [f"Company {i}" for i in range(MAX_COMPANIES_PER_SELECTION + 1)]
    resp = client.post(
        "/start",
        data={"companies": companies, "seniority_level": "mid", "workplace_type": "any"},
    )

    assert resp.status_code == 200
    assert "too many companies selected" in resp.get_data(as_text=True)
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0
    assert _profile_row_for_anon(app.config["_TEST_DSN"]) is None


def test_oversized_company_length_rerenders_without_writing(app):
    """issue #80: a single company selection longer than MAX_COMPANY_LENGTH
    (e.g. an arbitrary pasted text blob) must be rejected before it reaches
    the session cookie, not silently truncated."""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "companies": ["x" * (MAX_COMPANY_LENGTH + 1)],
            "seniority_level": "mid",
            "workplace_type": "any",
        },
    )

    assert resp.status_code == 200
    assert f"{MAX_COMPANY_LENGTH}-character limit" in resp.get_data(as_text=True)
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0
    assert _profile_row_for_anon(app.config["_TEST_DSN"]) is None


def test_non_string_company_is_rejected_by_type_check():
    """Werkzeug's real form MultiDict.getlist() always returns str (no HTTP
    form encoding can carry a non-str value), so the type check has no
    reachable path through a genuine POST. Exercise it the same way
    start_submit() invokes _parse_submission, against a minimal form
    double, to cover the branch directly — mirrors
    test_non_string_title_is_rejected_by_type_check."""

    class _NonStringValueForm:
        def get(self, key, default=None):
            return {"seniority_level": "mid", "workplace_type": "any"}.get(key, default)

        def getlist(self, key):
            return [123] if key == "companies" else []

    selections, error = _parse_submission(_NonStringValueForm())

    assert selections is None
    assert error == "companies must be text values"


def test_company_selections_at_the_cap_boundary_write_successfully(app):
    """Happy path at both boundaries inclusive: MAX_COMPANIES_PER_SELECTION
    companies, each exactly MAX_COMPANY_LENGTH characters, must still
    succeed. Submitted ALONGSIDE titles at THEIR cap in the same POST —
    this is the combined worst-case cookie payload issue #80 asks to be
    verified (both fields maxed simultaneously, not just companies in
    isolation), so this is the regression guard for MAX_COMPANY_LENGTH's
    module-comment arithmetic, not merely a companies-only echo of the
    titles boundary test above.

    This is also the FIRST request in the session, so
    jobcannon/web/anon_session.py's before_request hook mints
    anon_session_id/feed_session_id and captures attribution into the same
    cookie on this exact call — a `ref` query param and `Referer` header at
    their own respective worst cases (attribution's `channel`/`referrer_host`
    fields, capped by _CHANNEL_MAX_LEN / events_schema._MAX_STR) ride along,
    so the measured total here is the true combined worst case, not just
    titles+companies in isolation. SESSION_COOKIE_SECURE is forced True (the
    `app` fixture's TESTING=True flips it False, per
    jobcannon/web/__init__.py) so the measured byte count matches the real
    production `Set-Cookie` shape, "; Secure" attribute included, rather
    than an artificially-smaller testing-only one."""
    app.config["SESSION_COOKIE_SECURE"] = True
    client = app.test_client()
    titles = [_non_repeating_title(i, MAX_TITLE_LENGTH) for i in range(MAX_TITLES_PER_SELECTION)]
    companies = [
        _non_repeating_title(10_000 + i, MAX_COMPANY_LENGTH)
        for i in range(MAX_COMPANIES_PER_SELECTION)
    ]
    channel = _non_repeating_hostname(20_000, 32)  # _CHANNEL_MAX_LEN
    referrer_host = _non_repeating_hostname(30_000, 200)  # events_schema._MAX_STR
    resp = client.post(
        f"/start?ref={channel}",
        data={
            "titles": titles,
            "companies": companies,
            "skills": list(SKILLS_OPTIONS),
            "seniority_level": "principal",
            "years_of_experience": "12.5",
            "workplace_type": "onsite",
        },
        headers={"Referer": f"https://{referrer_host}/apply"},
    )

    assert resp.status_code in (302, 303)
    cookie_bytes = sum(len(h) for h in resp.headers.get_all("Set-Cookie"))
    assert cookie_bytes < _COMMON_BROWSER_COOKIE_LIMIT, (
        f"session cookie ({cookie_bytes}B) exceeds the common browser per-cookie limit"
    )

    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row is not None
    assert len(row["target_titles"]) == MAX_TITLES_PER_SELECTION

    # `companies` never reaches durable storage (see onboarding.py's module
    # docstring) — only the session copy carries it, which is exactly the
    # thing this test's cookie-size assertion above is protecting.
    with client.session_transaction() as sess:
        assert len(sess["pending_picker"]["companies"]) == MAX_COMPANIES_PER_SELECTION
        assert all(len(c) == MAX_COMPANY_LENGTH for c in sess["pending_picker"]["companies"])


def test_comp_floor_usd_written_to_profile(app):
    """#28 item 2: the optional numeric field reaches profiles.comp_floor_usd
    (m0008) through upsert_profile, same as every other picker field."""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "seniority_level": "senior",
            "comp_floor_usd": "120000",
            "workplace_type": "any",
        },
    )
    assert resp.status_code in (302, 303)

    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row["comp_floor_usd"] == 120000


def test_comp_floor_usd_zero_writes_zero_not_null(app):
    """PR #164 review (devin lead, self-verified): 0 is a valid, distinct-
    from-NULL comp_floor_usd (CHECK is `IS NULL OR >= 0`). No prior test in
    this file submitted the literal HTTP value "0" and asserted it lands as
    0 rather than NULL — closes that end-to-end coverage gap. (A future
    regression that swapped `_parse_submission`'s `if comp_floor_raw:` guard
    for a truthiness check on the parsed int, or added an `or None` on the
    result, would coerce 0 into NULL and pass every other existing test.)"""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "seniority_level": "mid",
            "comp_floor_usd": "0",
            "workplace_type": "any",
        },
    )
    assert resp.status_code in (302, 303)

    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row["comp_floor_usd"] == 0
    assert row["comp_floor_usd"] is not None


def test_comp_floor_usd_omitted_defaults_null(app):
    """Optional field: a submission with no comp_floor_usd must still
    succeed, storing NULL rather than rejecting the submission."""
    client = app.test_client()
    resp = client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})
    assert resp.status_code in (302, 303)

    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row["comp_floor_usd"] is None


def test_comp_floor_usd_never_reaches_the_session_cookie(app):
    """Deliberate exclusion (see onboarding.py's comment at the
    comp_floor_usd validation site): unlike every other picker field, it has
    no /preview reader, so it must never be spread into pending_picker —
    protects the already-tight measured session-cookie budget
    (MAX_COMPANY_LENGTH's module comment) from an unused key."""
    client = app.test_client()
    client.post(
        "/start",
        data={
            "seniority_level": "senior",
            "comp_floor_usd": "120000",
            "workplace_type": "any",
        },
    )

    with client.session_transaction() as sess:
        assert "comp_floor_usd" not in sess["pending_picker"]


def test_non_numeric_comp_floor_usd_rerenders_without_writing(app):
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "seniority_level": "mid",
            "comp_floor_usd": "not-a-number",
            "workplace_type": "any",
        },
    )

    assert resp.status_code == 200
    assert "compensation floor must be a whole number" in resp.get_data(as_text=True)
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0
    assert _profile_row_for_anon(app.config["_TEST_DSN"]) is None


def test_decimal_comp_floor_usd_rerenders_without_writing(app):
    """comp_floor_usd is a whole-dollar `integer` column (m0008) — a
    fractional submission must be rejected outright, never silently
    truncated."""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "seniority_level": "mid",
            "comp_floor_usd": "120000.50",
            "workplace_type": "any",
        },
    )

    assert resp.status_code == 200
    assert "compensation floor must be a whole number" in resp.get_data(as_text=True)
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0
    assert _profile_row_for_anon(app.config["_TEST_DSN"]) is None


def test_negative_comp_floor_usd_rerenders_without_writing(app):
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "seniority_level": "mid",
            "comp_floor_usd": "-1",
            "workplace_type": "any",
        },
    )

    assert resp.status_code == 200
    assert f"compensation floor must be between 0 and {MAX_COMP_FLOOR_USD:,}" in resp.get_data(
        as_text=True
    )
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0
    assert _profile_row_for_anon(app.config["_TEST_DSN"]) is None


def test_comp_floor_usd_above_int4_range_rerenders_without_writing(app):
    """Above Postgres int4's max: must re-render with 200 (the same
    boundary MAX_YEARS_OF_EXPERIENCE enforces for its own column), never
    reach upsert_profile and raise psycopg.errors.NumericValueOutOfRange as
    an unhandled 500."""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "seniority_level": "mid",
            "comp_floor_usd": str(MAX_COMP_FLOOR_USD + 1),
            "workplace_type": "any",
        },
    )

    assert resp.status_code == 200
    assert f"compensation floor must be between 0 and {MAX_COMP_FLOOR_USD:,}" in resp.get_data(
        as_text=True
    )
    assert _anon_user_count(app.config["_TEST_DSN"]) == 0
    assert _profile_row_for_anon(app.config["_TEST_DSN"]) is None


def test_comp_floor_usd_at_int4_max_boundary_writes_successfully(app):
    """Happy path at the upper boundary inclusive: MAX_COMP_FLOOR_USD itself
    must still succeed — the cap rejects strictly-over, not at-the-limit."""
    client = app.test_client()
    resp = client.post(
        "/start",
        data={
            "seniority_level": "mid",
            "comp_floor_usd": str(MAX_COMP_FLOOR_USD),
            "workplace_type": "any",
        },
    )

    assert resp.status_code in (302, 303)
    row = _profile_row_for_anon(app.config["_TEST_DSN"])
    assert row["comp_floor_usd"] == MAX_COMP_FLOOR_USD


def test_repeat_get_start_after_submit_shows_completion_state(app):
    """The redirect target, confirmed working on its own: GET /start after
    a completed POST /start renders the "submitted" confirmation with a
    link to /preview, not a 401 or a 500."""
    client = app.test_client()
    post_resp = client.post("/start", data={"seniority_level": "mid", "workplace_type": "any"})
    assert post_resp.status_code in (302, 303)

    resp = client.get("/start")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "picker submitted" in html.lower()
    assert 'href="/preview"' in html

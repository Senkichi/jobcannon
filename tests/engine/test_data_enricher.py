# PORTED from job_finder/web/tests/test_data_enricher.py @ 5fd8807eba99f087984f1baac707b404b67c871d (private job-cannon). Ledger L-0174.
"""Tests for jobcannon.engine.data_enricher — cost-ordered enrichment pipeline.

Ported from job_finder/web/tests/test_data_enricher.py (job-cannon private
repo) at the same SHA the engine module itself was ported from (ledger
L-0174). The private suite is migration/network-heavy; the engine has no
migrations system and no ScanServices seam for several tier functions still
on L-0178 HOLD or otherwise unported. What did NOT port, and why:

  - TestSearchSerpapi / TestSearchDuckDuckGo (8 tests): exercise
    enrichment_tiers.search_serpapi / search_duckduckgo directly against a
    real ``requests.get`` mock. enrichment_tiers.py itself is L-0178 (HOLD,
    unlanded) -- this port only reaches those two functions through the
    svc.search_serpapi / svc.search_duckduckgo ScanServices hooks (see
    TestEnrichJobTierOrder / TestDDGTierPersist below). Skipped wholesale.
  - TestFetchDirectJd (7 tests) / TestAuthWallGuard (7 tests): same L-0178
    HOLD reason -- both exercise enrichment_tiers._fetch_direct_jd directly.
    Skipped wholesale.
  - TestMigration15 (3 tests) and all 4 tests in TestPipelineIntegration:
    drive real job_finder.web migrations (m008/m015) through a live
    MigrationContext, or inspect job_finder.web.scoring_runner's source.
    The engine has no migrations system and no scoring_runner counterpart
    (host-owned, not ported -- see CLAUDE.md's engine/host split). Both
    classes skipped wholesale.
  - TestRunEnrichmentBackfillSelect (5 tests): all target
    run_enrichment_backfill, one of the three functions this port
    deliberately drops (see data_enricher.py's own module docstring /
    PORT-SEAM comment) because it calls
    job_finder.web.autoheal.agentic_enricher, which has no ledger row or
    ScanServices seam in this port's read scope. Dropped wholesale rather
    than skipped: unlike the migration/L-0178 classes above (which reference
    real functions that exist elsewhere and may land a seam later),
    run_enrichment_backfill itself does not exist post-port, so there is no
    ledger-preserving value in keeping empty stub methods that reference a
    deleted function.

Everything else ports with unchanged assertions except:

  1. DB/services wiring: the private ``temp_db`` / ``promo_db`` fixtures
     (real sqlite3 files migrated by the private migrations system) are
     replaced by the ``db`` fixture below -- an in-memory schema covering
     just the columns data_enricher.py's SQL actually touches, wired into
     jobcannon.engine.services.ScanServices via ``_install_services()``.
     ``patch("job_finder.web.data_enricher.X")`` context managers become
     ScanServices field overrides (``_install_services(fetch_direct_jd=...)``)
     -- the enrichment_tiers.* hooks (L-0178 HOLD) are only reachable this
     way post-port. ``_install_services()``'s defaults for those seven hooks
     reproduce the private ``stub_enrichment_network`` fixture's "always
     miss" shapes, so classes that used
     ``@pytest.mark.usefixtures("stub_enrichment_network")`` need no
     replacement marker -- the ``db``/``_install_services()`` defaults
     already are that fixture.
  2. ``set_jd_full`` test double: ``_fake_set_jd_full()`` below re-implements
     the private job_finder.db._jd_full.set_jd_full()'s content-gating
     contract using the REAL ported jobcannon.engine.jd_content_contract
     pure functions (``_is_jd_junk``, ``jd_content_reject``) plus the same
     terminal-tier NULL-reset issue #1374 needs, so the auto-promote /
     description-promotion / #1374-regression tests exercise real gating
     logic rather than a no-op stub.
  3. ``apply_location_observation`` / ``set_direct_url`` test doubles: both
     are concrete Postgres persistence-layer functions in jobcannon.db
     (``%s`` placeholders, a ``postings`` table, ``psycopg.Error`` handling)
     -- genuinely incompatible with this suite's SQLite fixture. Neither is
     reachable by any test ported here in its REAL form. ``set_direct_url``
     needs no test double at all: unlike ``apply_location_observation`` (a
     direct module-level import), data_enricher.py never imports
     ``set_direct_url`` directly -- it is only reachable through the
     optional ``svc.set_direct_url`` ScanServices field (already ``is not
     None``-guarded at its one call site), which ``_install_services()``
     leaves unset by default. It is also never triggered in its real form
     by any fixture here regardless: ``pick_direct_link`` only returns
     non-None for a real ATS-domain URL or an explicit
     direct_url/direct_url_confidence key, neither of which any fixture
     here supplies. ``apply_location_observation`` IS reached by one
     ported test (`test_valid_location_written_when_jd_is_junk`), so it
     gets a minimal faithful test double (``_fake_apply_location_observation``)
     that writes the raw location string verbatim to the ``location``
     column -- not the real function's locations_raw merge / structured
     parse / workplace_type derivation, since no ported test needs that
     fidelity. This is a test-side workaround, not a source fix; the
     underlying architectural gap (no SQLite-compatible ScanServices seam
     for ``apply_location_observation``) is worth its own follow-up ledger
     row.
  4. Salary-reconciliation divergence (Wave-1 divergence #3, documented in
     data_enricher.py's own _persist() comments): the ported _persist()
     replaces the private repo's trust-ranked ``_reconcile_salary_for_write``
     (which validated plausibility, swapped simple inversions, and dropped
     extreme ones) with a strictly simpler "fill-if-null-only" policy: if
     EITHER of job_row['salary_min'] / job_row['salary_max'] is already
     non-null, ANY incoming salary pair is dropped outright (regardless of
     plausibility); if BOTH are null, the incoming pair is written VERBATIM
     (no swap, no plausibility check). Four tests are adapted to this
     behavior, with the divergence called out inline at each:
       - test_inverted_salary_swapped_and_written: no swap logic remains in
         _persist() -- existing DB fields are null, so the incoming
         (inverted) pair is written UNSWAPPED, verbatim.
       - test_extreme_salary_inversion_dropped_tier_written: same null-fill
         branch -- the >10x-ratio pair is written VERBATIM, not dropped.
         Plausibility no longer gates the null-fill case; only "an existing
         field is already populated" does.
       - test_tier_written_when_all_fields_are_junk: jd_full stays
         junk-gated (unaffected -- routed through _fake_set_jd_full,
         unchanged behavior), but the salary pair is now written verbatim
         (existing fields are null) instead of dropped.
       - test_jd_full_clear_not_reverted_by_salary_add_branch: since the
         existing salary fields are both null, canonical_written becomes
         True (the pair is written), so `salary_implausible` is CLEARED (the
         `if canonical_written:` branch), not SET -- see the dead-elif
         finding below.
     test_normal_salary_order_written_unchanged and
     test_jd_full_clear_not_reverted_by_salary_remove_branch need no
     adaptation -- both already exercise the null-fill / clear-branch paths
     the new policy also takes.

Findings surfaced but NOT fixed (flagged for a follow-up ledger row rather
than fixed unilaterally -- outside "minimal, obviously-correct" bugfix
scope):

  - Dead ``elif`` in ``_persist()``'s salary-quarantine sync: ``elif
    resolution == "implausible" and (job_row.get("salary_min") is None and
    job_row.get("salary_max") is None):`` is only reachable when
    ``canonical_written`` is False, which (under the fill-if-null policy)
    only happens when at least one existing DB field is non-null -- the
    exact negation of the elif's own guard. The branch can therefore never
    execute; ``salary_implausible`` can only ever be CLEARED post-port,
    never SET, via this code path. This is a genuine design gap under the
    new policy (the private repo's trust-ranked reconciler could reach the
    equivalent SET path), but fixing it requires a product decision about
    desired quarantine behavior under fill-if-null, not a "restore the
    obviously-intended guard" fix -- left for a follow-up ledger row.
  - ``set_direct_url`` / ``apply_location_observation`` Postgres
    incompatibility -- see point 3 above. Worth its own follow-up ledger row
    (a SQLite- or dialect-agnostic seam for both).

Bugfix applied to data_enricher.py during this port (see that file's inline
comment at ``_apply_post_fetch_extraction``): ``svc.parse_structured_fields``
was called unconditionally, unlike every sibling optional ScanServices hook
in the same module (all guarded with ``is not None``). Since
parse_structured_fields legitimately defaults to None (services.py: L-0178
HOLD) and every fixture here leaves it unset by default, the unguarded call
raised TypeError on almost every real-JD path, silently swallowed by
enrich_job's per-tier ``except Exception``, cascading every affected row to
'exhausted'. Fixed with the same ``is None: return`` guard already used by
every sibling hook in the module.
"""

import contextlib
import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from jobcannon.engine import services
from jobcannon.engine.enrichment_states import LOW_SIGNAL_TERMINAL
from jobcannon.engine.jd_content_contract import (
    JD_CONTENT_REASON_CODES,
    _is_jd_junk,
    jd_content_reject,
)

_LOW_SIGNAL_TERMINAL_VALUES = frozenset(tier.value for tier in LOW_SIGNAL_TERMINAL)

# Parity with jobcannon.host.wiring._JD_STORAGE_MAX_CHARS.
_JD_STORAGE_MAX_CHARS = 50_000

_SCHEMA = """
CREATE TABLE jobs (
    dedup_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    jd_full TEXT DEFAULT NULL,
    salary_min INTEGER DEFAULT NULL,
    salary_max INTEGER DEFAULT NULL,
    salary_provenance TEXT DEFAULT NULL,
    salary_observations TEXT NOT NULL DEFAULT '[]',
    source_urls TEXT DEFAULT '[]',
    company_id INTEGER DEFAULT NULL,
    enrichment_tier TEXT DEFAULT NULL,
    locations_raw TEXT DEFAULT NULL,
    locations_structured TEXT DEFAULT NULL,
    workplace_type TEXT DEFAULT 'UNSPECIFIED',
    primary_country_code TEXT DEFAULT NULL,
    unresolved_reasons TEXT NOT NULL DEFAULT '[]',
    classification TEXT DEFAULT NULL,
    sub_scores_json TEXT DEFAULT NULL,
    fit_analysis TEXT DEFAULT NULL,
    scoring_model TEXT DEFAULT NULL,
    jd_content_verdict TEXT DEFAULT NULL,
    jd_content_signal TEXT DEFAULT NULL,
    jd_adjudicated_version INTEGER DEFAULT NULL,
    has_subcountry_constraint INTEGER DEFAULT NULL,
    description TEXT DEFAULT NULL
);

CREATE TABLE companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_raw TEXT NOT NULL,
    homepage_url TEXT DEFAULT NULL,
    ats_platform TEXT DEFAULT NULL,
    ats_slug TEXT DEFAULT NULL,
    ats_probe_status TEXT DEFAULT 'pending'
);

CREATE TABLE scoring_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT DEFAULT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    timestamp TEXT NOT NULL
);
"""


def _fake_set_jd_full(conn, dedup_key, text, *, source, title=None, config=None):
    """Test-local stand-in for job_finder.db._jd_full.set_jd_full().

    Reuses the REAL ported jd_content_contract gating (``_is_jd_junk``,
    ``jd_content_reject``) rather than a no-op stub, so the auto-promote /
    description-promotion / issue #1374 regression tests exercise real
    content-gating logic. Mirrors data_enricher._mutate_unresolved_reason's
    surgical (re-SELECT, list-rebuild) update so an unrelated existing
    reason code is preserved rather than clobbered.
    """
    if text is None or _is_jd_junk(text):
        return False

    verdict = jd_content_reject(text, title, config)

    row = conn.execute(
        "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?", (dedup_key,)
    ).fetchone()
    try:
        reasons = json.loads(row["unresolved_reasons"]) if row is not None else []
        if not isinstance(reasons, list):
            reasons = []
    except (TypeError, ValueError):
        reasons = []

    if verdict is not None:
        reason_code, _signal = verdict
        if reason_code not in reasons:
            reasons = [*reasons, reason_code]
        conn.execute(
            "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
            (json.dumps(reasons), dedup_key),
        )
        conn.commit()
        # issue #1374: a content-reject on a row parked at a LOW_SIGNAL_TERMINAL
        # tier resets that tier to NULL so the row re-enters the pipeline.
        tier_row = conn.execute(
            "SELECT enrichment_tier FROM jobs WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if tier_row is not None and tier_row["enrichment_tier"] in _LOW_SIGNAL_TERMINAL_VALUES:
            conn.execute(
                "UPDATE jobs SET enrichment_tier = NULL WHERE dedup_key = ?",
                (dedup_key,),
            )
            conn.commit()
        return False

    reasons = [r for r in reasons if r not in JD_CONTENT_REASON_CODES]
    conn.execute(
        "UPDATE jobs SET jd_full = ?, unresolved_reasons = ? WHERE dedup_key = ?",
        (text[:_JD_STORAGE_MAX_CHARS], json.dumps(reasons), dedup_key),
    )
    conn.commit()
    return True


def _fake_apply_location_observation(conn, dedup_key, raw_location, *, source):
    """Test-local stand-in for jobcannon.db._locations.apply_location_observation.

    The real function is a concrete Postgres persistence-layer writer (``%s``
    placeholders, a ``postings`` table, ``psycopg.Error`` handling) --
    genuinely incompatible with this suite's SQLite fixture (architectural
    gap, not fixed here -- see module docstring). This minimal stand-in
    writes the raw location string verbatim to the ``location`` column so
    ported assertions that check the write landed continue to hold; it does
    not reproduce the real function's locations_raw merge / structured
    parse / workplace_type derivation, since no ported test needs that
    fidelity.

    The real function's docstring is an explicit contract: "Never raises on
    parse failure -- logs at WARNING and returns False," and its DB write is
    wrapped in ``except psycopg.Error`` with the comment "the funnel never
    raises -- a side-door write must not abort the surrounding enrichment
    persist (mirrors set_jd_full's soft-fail)." This stand-in mirrors that
    by catching sqlite3.Error around its own UPDATE (relevant to
    TestPersistInvariantGuards::test_trigger_rejection_still_records_tier,
    whose DB-level trigger simulates exactly the kind of write rejection the
    real function is contractually required to swallow).
    """
    if not dedup_key or not raw_location or not str(raw_location).strip():
        return False
    try:
        conn.execute(
            "UPDATE jobs SET location = ? WHERE dedup_key = ?",
            (str(raw_location), dedup_key),
        )
        conn.commit()
    except sqlite3.Error:
        return False
    return True


def _extract_salary_from_text(text):
    """Test-local override for the optional svc.extract_salary_from_text hook.

    services.py documents this field as tied to the private repo's
    salary_extractor module (L-0253, DIES verdict -- that module will never
    be ported), so it legitimately defaults to None for every host. Wired
    ONLY for TestDDGTierPersist::test_ddg_jd_triggers_post_fetch_salary_extraction
    below -- every other test leaves it at _install_services()'s
    production-faithful default (None: "no host supplies this").

    Reuses the real ported jobcannon.engine.salary_normalizer regex+
    normalization pipeline rather than a hand-rolled regex.
    """
    from jobcannon.engine.salary_normalizer import normalize_observation, parse_salary_text

    obs = parse_salary_text(text, provenance="jd_regex")
    if obs is None:
        return (None, None)
    norm = normalize_observation(obs)
    if norm.salary_min is not None and norm.salary_max is not None:
        return (norm.salary_min, norm.salary_max)
    return (None, None)


def _install_services(conn=None, **overrides):
    """Build and register one ScanServices for a test.

    Defaults mirror the private repo's ``stub_enrichment_network`` fixture:
    every enrichment_tiers.* hook (L-0178 HOLD) defaults to its real "no
    result" shape so enrich_job proceeds to its DB logic immediately. Pass
    a fresh MagicMock as a keyword override for any hook a test needs to
    assert on (call count, args, etc.); the rest keep their default "miss".
    """

    @contextlib.contextmanager
    def factory(*, synchronous="FULL"):
        yield conn

    defaults = dict(
        connection_factory=factory,
        upsert_job=MagicMock(),
        set_jd_full=_fake_set_jd_full,
        upsert_company=MagicMock(),
        config={},
        get_secret=MagicMock(return_value=None),
        jd_storage_max_chars=_JD_STORAGE_MAX_CHARS,
        fetch_direct_jd=MagicMock(return_value=None),
        query_ats_api=MagicMock(return_value={}),
        scrape_careers_tier=MagicMock(return_value={}),
        search_ddg_web=MagicMock(return_value={}),
        fetch_ddg_jds=MagicMock(return_value=(None, None)),
        search_duckduckgo=MagicMock(return_value=None),
        search_serpapi=MagicMock(return_value=(None, [])),
    )
    defaults.update(overrides)
    services.set_services(services.ScanServices(**defaults))


@pytest.fixture
def db():
    """(conn) — an in-memory SQLite DB wired into ScanServices.

    Replaces the private repo's ``temp_db`` / ``promo_db`` fixtures (real
    sqlite3 files migrated by the private migrations system). Installs
    production-faithful default services via ``_install_services()``;
    individual tests re-call ``_install_services(db, **overrides)`` to
    override specific hooks. ``apply_location_observation`` is patched at
    the data_enricher import site (see module docstring point 3) for the
    duration of the test -- ``set_direct_url`` needs no such patch: it is
    NOT a module-level import in data_enricher.py, only the optional
    ``svc.set_direct_url`` ScanServices field (already ``is not
    None``-guarded and left unset by ``_install_services()``'s defaults),
    so it is simply never called by any test here.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    _install_services(conn)

    import jobcannon.engine.data_enricher as _de_mod

    original_apply_location_observation = _de_mod.apply_location_observation
    _de_mod.apply_location_observation = _fake_apply_location_observation
    try:
        yield conn
    finally:
        _de_mod.apply_location_observation = original_apply_location_observation
        services.clear_services()
        conn.close()


@pytest.fixture
def sparse_job_row():
    """A job row missing jd_full and salary data (needs enrichment)."""
    return {
        "dedup_key": "acme|data-scientist|remote",
        "title": "Data Scientist",
        "company": "Acme Corp",
        "location": "Remote",
        "jd_full": None,
        "salary_min": None,
        "salary_max": None,
        "source_urls": '["https://example.com/job/123"]',
        "company_id": None,
        "enrichment_tier": None,
        "description": "Build ML models",
    }


@pytest.fixture
def rich_job_row():
    """A job row with all scoring-relevant data (no enrichment needed)."""
    return {
        "dedup_key": "beta|staff-ds|sf",
        "title": "Staff Data Scientist",
        "company": "Beta Inc",
        "location": "San Francisco, CA",
        "jd_full": "Full job description text here with lots of detail about the role.",
        "salary_min": 200000,
        "salary_max": 280000,
        "source_urls": "[]",
        "company_id": None,
        "enrichment_tier": None,
        "description": "Lead data science.",
    }


# ---------------------------------------------------------------------------
# Not ported: enrichment_tiers.* (L-0178 HOLD) direct-call tests
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Exercises job_finder.web.enrichment_tiers.search_serpapi directly "
        "against a mocked requests.get. enrichment_tiers.py is L-0178 (HOLD, "
        "unlanded) -- this port only reaches it through the svc.search_serpapi "
        "ScanServices hook (see TestEnrichJobTierOrder / TestDDGTierPersist), "
        "exercised there via a MagicMock override, not the real function body."
    )
)
class TestSearchSerpapi:
    def test_search_serpapi_makes_request_with_job_query(self):
        pass

    def test_search_serpapi_returns_dict_with_job_data(self):
        pass

    def test_search_serpapi_returns_none_when_no_results(self):
        pass

    def test_search_serpapi_returns_none_on_request_error(self):
        pass


@pytest.mark.skip(
    reason=(
        "Exercises job_finder.web.enrichment_tiers.search_duckduckgo directly "
        "against a mocked requests.get. Same L-0178 HOLD reason as "
        "TestSearchSerpapi above -- only reachable post-port via the "
        "svc.search_duckduckgo ScanServices hook."
    )
)
class TestSearchDuckDuckGo:
    def test_search_duckduckgo_queries_ddg_api(self):
        pass

    def test_search_duckduckgo_returns_abstract_text(self):
        pass

    def test_search_duckduckgo_returns_none_when_no_abstract(self):
        pass

    def test_search_duckduckgo_returns_none_on_error(self):
        pass


# ---------------------------------------------------------------------------
# Tests for enrich_job tier ordering
# ---------------------------------------------------------------------------


class TestEnrichJobTierOrder:
    """Verify strict cost ordering: free -> DDG -> SerpAPI -> agentic."""

    def test_free_tier_url_fetch_runs_first(self, sparse_job_row):
        """Direct URL fetch is attempted before any other tier."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '["https://example.com/job/123"]'

        mock_fetch = MagicMock(return_value="Full job description from direct URL fetch. " * 5)
        mock_ddg = MagicMock()
        mock_serp = MagicMock()
        _install_services(
            fetch_direct_jd=mock_fetch,
            search_duckduckgo=mock_ddg,
            search_serpapi=mock_serp,
        )

        enrich_job(sparse_job_row, serpapi_key="key")

        mock_fetch.assert_called_once()
        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()

    def test_ddg_runs_after_free_tier_fails(self, sparse_job_row):
        """DDG only called when free tier doesn't satisfy missing fields."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        mock_ddg = MagicMock(return_value="Some DDG text about the job.")
        _install_services(
            fetch_direct_jd=MagicMock(return_value=None),
            search_ddg_web=MagicMock(return_value={"ddg_urls": [], "ddg_snippet": ""}),
            fetch_ddg_jds=MagicMock(return_value=(None, None)),
            search_duckduckgo=mock_ddg,
            search_serpapi=MagicMock(return_value=(None, [])),
        )

        enrich_job(sparse_job_row, serpapi_key=None)

        mock_ddg.assert_called_once()

    def test_serpapi_runs_after_ddg_for_jd(self, sparse_job_row):
        """SerpAPI only called when JD still missing after DDG."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        # search_serpapi is contractually a tuple[dict|None, list] (see
        # data_enricher.py's docstring); this single-dict return value is
        # the private suite's own mock shape, ported unchanged. enrich_job's
        # 2-value unpack of it raises inside the per-tier try/except
        # (silently logged) -- irrelevant here since this test only asserts
        # the hook was invoked.
        mock_serp = MagicMock(return_value={"jd_full": "Full JD from SerpAPI."})
        _install_services(
            fetch_direct_jd=MagicMock(return_value=None),
            search_ddg_web=MagicMock(return_value={"ddg_urls": [], "ddg_snippet": ""}),
            fetch_ddg_jds=MagicMock(return_value=(None, None)),
            search_duckduckgo=MagicMock(return_value=None),
            search_serpapi=mock_serp,
        )

        enrich_job(sparse_job_row, serpapi_key="test-key")

        mock_serp.assert_called_once()

    def test_serpapi_skipped_when_free_satisfies_jd(self, sparse_job_row):
        """If free tier URL fetch returns JD, SerpAPI is never called."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '["https://example.com/job/123"]'
        sparse_job_row["salary_min"] = 100000

        mock_ddg = MagicMock()
        mock_serp = MagicMock()
        _install_services(
            fetch_direct_jd=MagicMock(return_value="Full job description from direct URL. " * 6),
            search_duckduckgo=mock_ddg,
            search_serpapi=mock_serp,
        )

        enrich_job(sparse_job_row, serpapi_key="test-key")

        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()

    def test_free_tier_ats_query_runs_when_company_has_slug(self, sparse_job_row, db):
        """ATS API queried in free tier when company has ats_probe_status='hit'."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = 1

        db.execute(
            "INSERT INTO companies (id, name, name_raw, ats_platform, ats_slug, ats_probe_status) "
            "VALUES (1, 'acme corp', 'Acme Corp', 'lever', 'acme-corp', 'hit')"
        )
        db.commit()

        mock_ats = MagicMock(return_value={"jd_full": "ATS API returned full JD. " * 8})
        mock_ddg = MagicMock(return_value=None)
        _install_services(
            db,
            fetch_direct_jd=MagicMock(return_value=None),
            query_ats_api=mock_ats,
            search_duckduckgo=mock_ddg,
        )

        enrich_job(sparse_job_row, conn=db)

        mock_ats.assert_called_once()
        mock_ddg.assert_not_called()

    def test_free_tier_careers_scrape_runs_after_ats(self, sparse_job_row, db):
        """Careers page scraper tried when ATS query returns nothing."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = 1

        db.execute(
            "INSERT INTO companies (id, name, name_raw, ats_probe_status, homepage_url) "
            "VALUES (1, 'acme corp', 'Acme Corp', 'miss', 'https://acmecorp.com')"
        )
        db.commit()

        mock_scrape = MagicMock(return_value={"jd_full": "Careers page JD. " * 13})
        mock_ddg = MagicMock(return_value=None)
        _install_services(
            db,
            fetch_direct_jd=MagicMock(return_value=None),
            query_ats_api=MagicMock(return_value={}),
            scrape_careers_tier=mock_scrape,
            search_duckduckgo=mock_ddg,
        )

        enrich_job(sparse_job_row, conn=db)

        mock_scrape.assert_called_once()
        mock_ddg.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for DDG tier persistence (issue #224 — silent JD drop)
# ---------------------------------------------------------------------------


class TestDDGTierPersist:
    """DDG-fetched JDs must be persisted under enrichment_tier='ddg' rather than
    being captured into ``fragments`` then discarded when control falls through to
    the terminal exhausted-persist.
    """

    def test_ddg_jd_persisted_under_ddg_tier(self, sparse_job_row, db):
        """A real DDG-fetched JD (>= 200 chars) is written with enrichment_tier='ddg'."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, source_urls) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sparse_job_row["dedup_key"],
                sparse_job_row["title"],
                sparse_job_row["company"],
                sparse_job_row["location"],
                sparse_job_row["source_urls"],
            ),
        )
        db.commit()

        ddg_jd = "DuckDuckGo-fetched job description with lots of detail. " * 5
        assert len(ddg_jd) >= 200  # sanity: must pass the stub gate

        _install_services(
            db,
            fetch_direct_jd=MagicMock(return_value=None),
            search_ddg_web=MagicMock(
                return_value={
                    "ddg_urls": ["https://example.com/posting"],
                    "ddg_snippet": "snippet",
                }
            ),
            fetch_ddg_jds=MagicMock(return_value=(ddg_jd, "https://example.com/posting")),
            search_duckduckgo=MagicMock(return_value=None),
            search_serpapi=MagicMock(return_value=(None, [])),
        )

        result = enrich_job(sparse_job_row, serpapi_key=None, conn=db)

        row = db.execute(
            "SELECT jd_full, enrichment_tier FROM jobs WHERE dedup_key = ?",
            (sparse_job_row["dedup_key"],),
        ).fetchone()

        assert row is not None
        assert row["jd_full"] == ddg_jd, "DDG JD must be persisted, not dropped"
        assert row["enrichment_tier"] == "ddg", "Tier must reflect DDG (not 'exhausted')"
        assert result.get("jd_full") == ddg_jd

    def test_ddg_stub_jd_rejected_and_escalates(self, sparse_job_row, db):
        """A DDG-returned stub (< 200 chars) is rejected; row is not persisted
        under 'ddg' and escalation to SerpAPI/agentic proceeds."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, source_urls) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sparse_job_row["dedup_key"],
                sparse_job_row["title"],
                sparse_job_row["company"],
                sparse_job_row["location"],
                sparse_job_row["source_urls"],
            ),
        )
        db.commit()

        stub_jd = "Apply now"  # < 200 chars — must be rejected

        _install_services(
            db,
            fetch_direct_jd=MagicMock(return_value=None),
            search_ddg_web=MagicMock(
                return_value={
                    "ddg_urls": ["https://example.com/posting"],
                    "ddg_snippet": "snippet",
                }
            ),
            fetch_ddg_jds=MagicMock(return_value=(stub_jd, "https://example.com/posting")),
            search_duckduckgo=MagicMock(return_value=None),
            search_serpapi=MagicMock(return_value=(None, [])),
        )

        enrich_job(sparse_job_row, serpapi_key=None, conn=db)

        row = db.execute(
            "SELECT jd_full, enrichment_tier FROM jobs WHERE dedup_key = ?",
            (sparse_job_row["dedup_key"],),
        ).fetchone()

        assert row is not None
        assert row["jd_full"] is None, "Stub DDG JD must NOT be persisted"
        assert row["enrichment_tier"] == "exhausted"

    def test_ddg_jd_triggers_post_fetch_salary_extraction(self, sparse_job_row, db):
        """The DDG-fetched JD must flow through _apply_post_fetch_extraction so
        salary regex sees the description (proves effective_jd is populated)."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, source_urls) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sparse_job_row["dedup_key"],
                sparse_job_row["title"],
                sparse_job_row["company"],
                sparse_job_row["location"],
                sparse_job_row["source_urls"],
            ),
        )
        db.commit()

        ddg_jd = (
            "Senior Data Scientist role at Acme Corp. "
            "Salary range: $150,000 - $200,000 USD per year. "
            "Build ML models and collaborate cross-functionally. "
            "We work on large-scale ML systems and value engineering rigor. "
            "Required: 5+ years Python; strong SQL; experience with cloud platforms."
        )
        assert len(ddg_jd) >= 200

        _install_services(
            db,
            fetch_direct_jd=MagicMock(return_value=None),
            search_ddg_web=MagicMock(
                return_value={
                    "ddg_urls": ["https://example.com/posting"],
                    "ddg_snippet": "snippet",
                }
            ),
            fetch_ddg_jds=MagicMock(return_value=(ddg_jd, "https://example.com/posting")),
            search_duckduckgo=MagicMock(return_value=None),
            search_serpapi=MagicMock(return_value=(None, [])),
            extract_salary_from_text=_extract_salary_from_text,
        )

        enrich_job(sparse_job_row, serpapi_key=None, conn=db)

        row = db.execute(
            "SELECT jd_full, enrichment_tier, salary_min, salary_max FROM jobs WHERE dedup_key = ?",
            (sparse_job_row["dedup_key"],),
        ).fetchone()

        assert row is not None
        assert row["enrichment_tier"] == "ddg"
        assert row["jd_full"] == ddg_jd
        # Regex-based salary extractor should have picked up "$150,000 - $200,000"
        assert row["salary_min"] == 150_000
        assert row["salary_max"] == 200_000


# ---------------------------------------------------------------------------
# Tests for per-field cost ceilings
# ---------------------------------------------------------------------------


class TestFieldCeilings:
    """Salary stops at ddg tier; JD escalates all the way to agentic."""

    def test_salary_ceiling_at_ddg(self, sparse_job_row):
        """When only salary is missing and ddg fetch fails, SerpAPI is not called."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["jd_full"] = "Full job description already present."
        sparse_job_row["salary_min"] = None
        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        mock_serp = MagicMock()
        _install_services(
            fetch_direct_jd=MagicMock(return_value=None),
            search_ddg_web=MagicMock(return_value={"ddg_urls": [], "ddg_snippet": ""}),
            fetch_ddg_jds=MagicMock(return_value=(None, None)),
            search_duckduckgo=MagicMock(return_value=None),
            search_serpapi=mock_serp,
        )

        enrich_job(sparse_job_row, serpapi_key="test-key")

        # SerpAPI should NOT be called when only salary is missing — JD already
        # present, so the jd_still_missing guard prevents SerpAPI/agentic escalation.
        mock_serp.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for enrichment_tier persistence
# ---------------------------------------------------------------------------


class TestEnrichmentTierPersistence:
    """enrichment_tier persisted atomically; resume-from-next-tier; exhausted skip."""

    def test_enrichment_tier_persisted_atomically(self, sparse_job_row, db):
        """enrichment_tier and enriched fields written in single UPDATE."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = '["https://example.com/job"]'

        db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, source_urls) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sparse_job_row["dedup_key"],
                sparse_job_row["title"],
                sparse_job_row["company"],
                sparse_job_row["location"],
                sparse_job_row["source_urls"],
            ),
        )
        db.commit()

        mock_jd = "Full job description from direct URL. " * 6  # 228 chars — passes stub gate
        _install_services(db, fetch_direct_jd=MagicMock(return_value=mock_jd))

        enrich_job(sparse_job_row, conn=db)

        row = db.execute(
            "SELECT jd_full, enrichment_tier FROM jobs WHERE dedup_key = ?",
            (sparse_job_row["dedup_key"],),
        ).fetchone()

        assert row is not None
        assert row["jd_full"] == mock_jd
        assert row["enrichment_tier"] == "free"

    def test_resumes_from_next_tier(self, sparse_job_row, db):
        """Job with enrichment_tier='ddg' starts at SerpAPI, not free."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["enrichment_tier"] = "ddg"
        sparse_job_row["source_urls"] = '["https://example.com/job"]'

        mock_fetch = MagicMock()
        mock_ats = MagicMock()
        mock_scrape = MagicMock()
        mock_ddg = MagicMock()
        mock_serp = MagicMock(return_value={"jd_full": "SerpAPI found the JD."})
        _install_services(
            db,
            fetch_direct_jd=mock_fetch,
            query_ats_api=mock_ats,
            scrape_careers_tier=mock_scrape,
            search_duckduckgo=mock_ddg,
            search_serpapi=mock_serp,
        )

        enrich_job(sparse_job_row, serpapi_key="test-key", conn=db)

        mock_fetch.assert_not_called()
        mock_ats.assert_not_called()
        mock_scrape.assert_not_called()
        mock_ddg.assert_not_called()
        mock_serp.assert_called_once()

    def test_exhausted_jobs_skipped(self, sparse_job_row):
        """Job with enrichment_tier='exhausted' returns empty dict immediately."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["enrichment_tier"] = "exhausted"

        mock_fetch = MagicMock()
        mock_ddg = MagicMock()
        mock_serp = MagicMock()
        _install_services(
            fetch_direct_jd=mock_fetch,
            search_duckduckgo=mock_ddg,
            search_serpapi=mock_serp,
        )

        result = enrich_job(sparse_job_row, serpapi_key="key")

        assert result == {}
        mock_fetch.assert_not_called()
        mock_ddg.assert_not_called()
        mock_serp.assert_not_called()


# ---------------------------------------------------------------------------
# _persist invariant-guard tests (issue #106)
# ---------------------------------------------------------------------------


class TestPersistInvariantGuards:
    """_persist routes jd_full through set_jd_full() and salary through the
    fill-if-null policy so a bad field cannot silently discard the
    enrichment_tier bookmark or sibling fields.
    """

    def _insert_job(self, conn, dedup_key: str) -> None:
        conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, location) VALUES (?, ?, ?, ?)",
            (dedup_key, "Test Job", "Test Co", "Remote"),
        )
        conn.commit()

    def _fetch(self, conn, dedup_key: str) -> dict:
        row = conn.execute(
            "SELECT jd_full, salary_min, salary_max, location, enrichment_tier "
            "FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert row is not None, f"Job {dedup_key!r} not found"
        return dict(row)

    # ------------------------------------------------------------------ #
    # jd_full — I-13 gate
    # ------------------------------------------------------------------ #

    def test_junk_jd_not_written_but_tier_recorded(self, db):
        """A junk jd_full (< 200 chars) is gated by set_jd_full(); tier still written."""
        from jobcannon.engine.data_enricher import _persist

        key = "co|junk-jd|test"
        self._insert_job(db, key)

        junk_jd = "loading"  # classic auth-wall junk
        _persist(db, {"dedup_key": key}, {"jd_full": junk_jd}, "free")

        row = self._fetch(db, key)
        assert row["jd_full"] is None, "Junk jd_full must NOT be written"
        assert row["enrichment_tier"] == "free", "Tier must be written even when jd_full is junk"

    def test_valid_jd_written_and_tier_recorded(self, db):
        """A valid jd_full (>= 200 chars) is written and tier is recorded."""
        from jobcannon.engine.data_enricher import _persist

        key = "co|valid-jd|test"
        self._insert_job(db, key)

        good_jd = "This is a real job description with lots of detail. " * 4  # > 200 chars
        _persist(db, {"dedup_key": key}, {"jd_full": good_jd}, "free")

        row = self._fetch(db, key)
        assert row["jd_full"] == good_jd
        assert row["enrichment_tier"] == "free"

    def test_valid_jd_clears_jd_content_reasons(self, db):
        """A successful _persist routes through set_jd_full, which clears
        stale I-18 reason codes from unresolved_reasons atomically."""
        from jobcannon.engine.data_enricher import _persist

        key = "co|valid-jd-clears-reasons|test"
        self._insert_job(db, key)
        db.execute(
            "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
            ('["jd_full_truncated", "legacy_other"]', key),
        )
        db.commit()

        good_jd = "This is a real job description with lots of detail. " * 4  # > 200 chars
        _persist(db, {"dedup_key": key}, {"jd_full": good_jd}, "free")

        raw = db.execute(
            "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?", (key,)
        ).fetchone()[0]
        reasons = json.loads(raw)
        assert "jd_full_truncated" not in reasons
        assert "legacy_other" in reasons

    # ------------------------------------------------------------------ #
    # issue #1374 round-2: Step 1 set_jd_full clear must not be reverted by
    # Step 2's salary _mutate_unresolved_reason reading a stale job_row
    # ------------------------------------------------------------------ #

    def test_jd_full_clear_not_reverted_by_salary_remove_branch(self, db):
        """Regression (issue #1374 round-2): when _persist writes BOTH a valid
        jd_full (healing a prior jd_full_truncated flag) AND a plausible salary
        pair in the same call, Step 2's salary sync must not resurrect the
        jd_full_truncated code Step 1 just cleared."""
        from jobcannon.engine.data_enricher import _persist

        key = "co|jd-heal-and-salary-remove|test"
        self._insert_job(db, key)
        db.execute(
            "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
            ('["jd_full_truncated", "salary_implausible"]', key),
        )
        db.commit()

        good_jd = "This is a real job description with lots of detail. " * 4  # > 200 chars
        stale_row = {
            "dedup_key": key,
            "unresolved_reasons": '["jd_full_truncated", "salary_implausible"]',
        }
        _persist(
            db,
            stale_row,
            {"jd_full": good_jd, "salary_min": 120_000, "salary_max": 180_000},
            "free",
        )

        raw = db.execute(
            "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?", (key,)
        ).fetchone()[0]
        reasons = json.loads(raw)
        assert "jd_full_truncated" not in reasons, (
            "jd_full_truncated must stay cleared after Step 1's set_jd_full; "
            "Step 2's salary sync must not resurrect it from a stale job_row"
        )
        assert "salary_implausible" not in reasons, (
            "plausible canonical salary must clear the salary_implausible code"
        )

    def test_jd_full_clear_not_reverted_by_salary_add_branch(self, db):
        """Regression (issue #1374 round-2), adapted for the fill-if-null
        salary policy (Wave-1 divergence #3, see module docstring point 4).

        job_row's existing salary fields are both null, so the incoming
        (implausible, 300_000/15) pair is written VERBATIM by the fill-if-null
        branch -- canonical_written becomes True, so the ``if
        canonical_written:`` branch CLEARS salary_implausible rather than
        setting it (the private repo's trust-ranked reconciler would have
        dropped this pair as implausible and SET the code instead; see the
        dead-elif finding in the module docstring). The jd_full_truncated
        clear-not-reverted assertion is unaffected by this divergence and is
        kept unchanged.
        """
        from jobcannon.engine.data_enricher import _persist

        key = "co|jd-heal-and-salary-add|test"
        self._insert_job(db, key)
        db.execute(
            "UPDATE jobs SET unresolved_reasons = ? WHERE dedup_key = ?",
            ('["jd_full_truncated"]', key),
        )
        db.commit()

        good_jd = "This is a real job description with lots of detail. " * 4  # > 200 chars
        stale_row = {
            "dedup_key": key,
            "unresolved_reasons": '["jd_full_truncated"]',
            "salary_min": None,
            "salary_max": None,
        }
        _persist(
            db,
            stale_row,
            {"jd_full": good_jd, "salary_min": 300_000, "salary_max": 15},
            "free",
        )

        raw = db.execute(
            "SELECT unresolved_reasons FROM jobs WHERE dedup_key = ?", (key,)
        ).fetchone()[0]
        reasons = json.loads(raw)
        assert "jd_full_truncated" not in reasons, (
            "jd_full_truncated must stay cleared after Step 1's set_jd_full; "
            "Step 2's salary sync must not resurrect it from a stale job_row"
        )
        assert "salary_implausible" not in reasons, (
            "fill-if-null policy divergence: existing salary fields are both "
            "null, so the incoming pair is written verbatim (canonical_written "
            "= True) and salary_implausible is CLEARED, not set -- see module "
            "docstring's dead-elif finding"
        )
        row = self._fetch(db, key)
        assert row["salary_min"] == 300_000
        assert row["salary_max"] == 15

    # ------------------------------------------------------------------ #
    # salary — fill-if-null policy (Wave-1 divergence #3)
    # ------------------------------------------------------------------ #

    def test_inverted_salary_swapped_and_written(self, db):
        """Adapted for the fill-if-null policy: existing DB fields are null,
        so the inverted pair is written VERBATIM (no swap) -- _persist() no
        longer contains any swap logic (see module docstring point 4)."""
        from jobcannon.engine.data_enricher import _persist

        key = "anthropic|data-scientist|test"
        self._insert_job(db, key)

        _persist(
            db,
            {"dedup_key": key},
            {"salary_min": 300_000, "salary_max": 200_000},
            "free",
        )

        row = self._fetch(db, key)
        assert row["salary_min"] == 300_000, "fill-if-null writes the pair verbatim, unswapped"
        assert row["salary_max"] == 200_000
        assert row["enrichment_tier"] == "free"

    def test_extreme_salary_inversion_dropped_tier_written(self, db):
        """Adapted for the fill-if-null policy: existing DB fields are null,
        so the >10x-ratio pair is written VERBATIM, not dropped -- plausibility
        no longer gates the null-fill case (see module docstring point 4)."""
        from jobcannon.engine.data_enricher import _persist

        key = "co|extreme-sal|test"
        self._insert_job(db, key)

        _persist(
            db,
            {"dedup_key": key},
            {"salary_min": 300_000, "salary_max": 15},
            "free",
        )

        row = self._fetch(db, key)
        assert row["salary_min"] == 300_000, "fill-if-null writes verbatim regardless of ratio"
        assert row["salary_max"] == 15
        assert row["enrichment_tier"] == "free", "Tier must be written regardless of salary write"

    def test_normal_salary_order_written_unchanged(self, db):
        """A correctly ordered salary pair passes through unchanged."""
        from jobcannon.engine.data_enricher import _persist

        key = "co|normal-sal|test"
        self._insert_job(db, key)

        _persist(
            db,
            {"dedup_key": key},
            {"salary_min": 120_000, "salary_max": 180_000},
            "ddg",
        )

        row = self._fetch(db, key)
        assert row["salary_min"] == 120_000
        assert row["salary_max"] == 180_000
        assert row["enrichment_tier"] == "ddg"

    # ------------------------------------------------------------------ #
    # field isolation — one bad field must not discard siblings
    # ------------------------------------------------------------------ #

    def test_valid_location_written_when_jd_is_junk(self, db):
        """When jd_full is junk and location is valid, location is still persisted."""
        from jobcannon.engine.data_enricher import _persist

        key = "co|partial-enrich|test"
        self._insert_job(db, key)

        _persist(
            db,
            {"dedup_key": key},
            {"jd_full": "sign in to view", "location": "San Francisco, CA"},
            "free",
        )

        row = self._fetch(db, key)
        assert row["jd_full"] is None, "Junk jd_full must NOT be written"
        assert row["location"] == "San Francisco, CA", "Valid location must be written"
        assert row["enrichment_tier"] == "free"

    def test_tier_written_when_all_fields_are_junk(self, db):
        """jd_full stays junk-gated (unchanged behavior); the salary pair is
        now written verbatim under the fill-if-null policy (existing DB
        fields are null) rather than dropped -- see module docstring point 4.
        """
        from jobcannon.engine.data_enricher import _persist

        key = "co|all-junk|test"
        self._insert_job(db, key)

        _persist(
            db,
            {"dedup_key": key},
            {"jd_full": "loading", "salary_min": 300_000, "salary_max": 15},
            "free",
        )

        row = self._fetch(db, key)
        assert row["jd_full"] is None, "Junk jd_full must still NOT be written"
        assert row["salary_min"] == 300_000, "fill-if-null writes the salary pair verbatim"
        assert row["salary_max"] == 15
        assert row["enrichment_tier"] == "free", "Tier must be written even when jd_full is dropped"

    # ------------------------------------------------------------------ #
    # trigger fallback — simulate a DB trigger rejecting the UPDATE
    # ------------------------------------------------------------------ #

    def test_trigger_rejection_still_records_tier(self, db):
        """Even if a DB trigger fires on the remaining-fields UPDATE, the tier
        fallback UPDATE ensures the job is not re-fetched indefinitely."""
        from jobcannon.engine.data_enricher import _persist

        key = "co|trigger-test|test"
        self._insert_job(db, key)

        db.execute(
            "CREATE TRIGGER tg_test_reject_location "
            "BEFORE UPDATE OF location ON jobs "
            "BEGIN "
            "  SELECT RAISE(ABORT, 'X-01: test rejection'); "
            "END"
        )
        db.commit()

        _persist(
            db,
            {"dedup_key": key},
            {"location": "New York, NY"},
            "ddg",
        )

        row = self._fetch(db, key)
        assert row["location"] == "Remote"
        assert row["enrichment_tier"] == "ddg", (
            "Tier fallback UPDATE must succeed even when the main UPDATE is rejected"
        )

    # ------------------------------------------------------------------ #
    # #1202: residency_location + has_subcountry_constraint
    # ------------------------------------------------------------------ #

    def test_residency_location_routed_through_funnel(self, db, monkeypatch):
        """_persist routes residency_location through apply_location_observation
        with source='llm_extract_residency' (not a direct column write)."""
        from jobcannon.engine.data_enricher import _persist

        key = "henry schein|senior data analyst|test"
        self._insert_job(db, key)

        calls: list[dict] = []

        def fake_apply(conn, dedup_key, raw_location, *, source):
            calls.append({"dedup_key": dedup_key, "raw_location": raw_location, "source": source})

        monkeypatch.setattr(
            "jobcannon.engine.data_enricher.apply_location_observation",
            fake_apply,
        )

        _persist(
            db,
            {"dedup_key": key},
            {"residency_location": "United Kingdom"},
            "free",
        )

        residency_calls = [c for c in calls if c["source"] == "llm_extract_residency"]
        assert len(residency_calls) == 1
        assert residency_calls[0]["raw_location"] == "United Kingdom"

    def test_has_subcountry_constraint_written_to_column(self, db):
        """_persist writes has_subcountry_constraint to its column (1 for True)."""
        from jobcannon.engine.data_enricher import _persist

        key = "genworth financial|principal data analyst|test"
        self._insert_job(db, key)

        _persist(
            db,
            {"dedup_key": key},
            {"has_subcountry_constraint": True},
            "free",
        )

        row = db.execute(
            "SELECT has_subcountry_constraint FROM jobs WHERE dedup_key = ?",
            (key,),
        ).fetchone()
        assert row[0] == 1

    def test_has_subcountry_constraint_false_written_as_zero(self, db):
        """_persist writes 0 (not NULL) when the LLM says no sub-country constraint,
        so subsequent enrichment passes skip the residency check."""
        from jobcannon.engine.data_enricher import _persist

        key = "co|no-constraint|test"
        self._insert_job(db, key)

        _persist(
            db,
            {"dedup_key": key},
            {"has_subcountry_constraint": False},
            "free",
        )

        row = db.execute(
            "SELECT has_subcountry_constraint FROM jobs WHERE dedup_key = ?",
            (key,),
        ).fetchone()
        assert row[0] == 0

    # ------------------------------------------------------------------ #
    # #1856: lock-retry on `database is locked` — both primary + fallback
    # ------------------------------------------------------------------ #

    def test_lock_retry_preserves_tier_write(self, db, monkeypatch):
        """A `database is locked` error on the step-3 UPDATE is retried with
        backoff so the enrichment_tier bookmark is not silently dropped (#1856)."""
        import time as _time

        from jobcannon.engine.data_enricher import _persist

        monkeypatch.setattr(_time, "sleep", lambda _s: None)

        key = "co|lock-retry|test"
        self._insert_job(db, key)

        class _LockThenSucceedConn:
            def __init__(self, real):
                self._real = real
                self._fail_remaining = 2

            def execute(self, sql, *args, **kwargs):
                if self._fail_remaining > 0 and "UPDATE jobs SET" in sql:
                    self._fail_remaining -= 1
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        conn = _LockThenSucceedConn(db)
        _persist(conn, {"dedup_key": key}, {}, "free")

        row = self._fetch(db, key)
        assert row["enrichment_tier"] == "free", (
            "Tier must be written after lock-retry succeeds, not silently dropped"
        )

    def test_lock_exhaustion_requeues_via_null_tier(self, db, monkeypatch, caplog):
        """When the primary UPDATE exhausts all lock retries, the tier-only
        fallback is NOT called — the job requeues via its existing (NULL)
        enrichment_tier instead of silently losing the enriched payload (#1856)."""
        import logging
        import time as _time

        from jobcannon.engine.data_enricher import _persist

        monkeypatch.setattr(_time, "sleep", lambda _s: None)

        key = "co|lock-exhaust-requeue|test"
        self._insert_job(db, key)

        class _AlwaysLockedConn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if "UPDATE jobs SET" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        conn = _AlwaysLockedConn(db)
        with caplog.at_level(logging.WARNING, logger="jobcannon.engine.data_enricher"):
            _persist(
                conn,
                {"dedup_key": key},
                {"salary_min": 140000, "salary_max": 180000},
                "free",
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Failed to persist enrichment" in r.getMessage() for r in warnings), (
            "Primary lock exhaustion must be logged at WARNING, not silently dropped"
        )
        assert not any("tier fallback" in r.getMessage() for r in warnings), (
            "Tier-only fallback must NOT be called when primary failure was a lock error"
        )
        row = self._fetch(db, key)
        assert row["enrichment_tier"] is None, (
            "Tier must stay NULL after primary lock exhaustion so the job requeues"
        )

    def test_non_lock_error_not_retried(self, db, monkeypatch):
        """A non-lock error (e.g. trigger violation) on the primary UPDATE is
        NOT retried — it falls straight through to the tier-only fallback
        (unchanged behavior). Only `database is locked` is retried (#1856)."""
        import time as _time

        from jobcannon.engine.data_enricher import _persist

        sleep_calls: list[float] = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleep_calls.append(s))

        key = "co|non-lock|test"
        self._insert_job(db, key)

        call_count = 0

        class _NonLockErrorConn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                nonlocal call_count
                if "UPDATE jobs SET enrichment_tier" in sql and call_count == 0:
                    call_count += 1
                    raise sqlite3.OperationalError("not a lock error")
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        conn = _NonLockErrorConn(db)
        _persist(conn, {"dedup_key": key}, {}, "free")

        assert sleep_calls == [], "Non-lock errors must not trigger lock-retry backoff"
        row = self._fetch(db, key)
        assert row["enrichment_tier"] == "free"

    def test_lock_exhaustion_fallback_not_called_even_if_would_succeed(
        self, db, monkeypatch, caplog
    ):
        """When the primary UPDATE exhausts all lock retries, the tier-only
        fallback is NOT called even though it would succeed — the enriched
        payload is preserved for requeue, not silently bookmarked away (#1856)."""
        import logging
        import time as _time

        from jobcannon.engine.data_enricher import _persist

        monkeypatch.setattr(_time, "sleep", lambda _s: None)

        key = "co|lock-exhaust-no-fallback|test"
        self._insert_job(db, key)

        class _PrimaryLockedFallbackSucceedsConn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if "salary_min" in sql or "salary_max" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        conn = _PrimaryLockedFallbackSucceedsConn(db)
        with caplog.at_level(logging.WARNING, logger="jobcannon.engine.data_enricher"):
            _persist(
                conn,
                {"dedup_key": key},
                {"salary_min": 140000, "salary_max": 180000},
                "free",
            )

        row = self._fetch(db, key)
        assert row["enrichment_tier"] is None, (
            "Tier must stay NULL — fallback must not be called after primary lock exhaustion"
        )
        assert row["salary_min"] is None
        assert row["salary_max"] is None

    def test_non_lock_primary_fallback_lock_retry_succeeds(self, db, monkeypatch):
        """When the primary UPDATE fails with a non-lock error and the
        tier-only fallback then hits `database is locked`, the fallback's own
        retry succeeds — the tier bookmark is written (#1856)."""
        import time as _time

        from jobcannon.engine.data_enricher import _persist

        sleep_calls: list[float] = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleep_calls.append(s))

        key = "co|non-lock-fallback-lock-retry|test"
        self._insert_job(db, key)

        class _NonLockPrimaryThenFallbackLockConn:
            def __init__(self, real):
                self._real = real
                self._primary_failed = False
                self._fallback_attempts = 0

            def execute(self, sql, *args, **kwargs):
                if (
                    "UPDATE jobs SET" in sql
                    and not self._primary_failed
                    and "enrichment_tier" in sql
                ):
                    self._primary_failed = True
                    raise sqlite3.OperationalError("trigger violation: not a lock error")
                if "UPDATE jobs SET enrichment_tier" in sql and self._fallback_attempts == 0:
                    self._fallback_attempts += 1
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        conn = _NonLockPrimaryThenFallbackLockConn(db)
        _persist(conn, {"dedup_key": key}, {}, "free")

        assert len(sleep_calls) == 1, (
            "Fallback must retry once on lock error after non-lock primary failure"
        )
        row = self._fetch(db, key)
        assert row["enrichment_tier"] == "free"


# ---------------------------------------------------------------------------
# Backward compatibility and never-raises
# ---------------------------------------------------------------------------


class TestEnrichJobBackwardCompat:
    """Old call patterns and error handling still work."""

    def test_enrich_job_backward_compatible_signature(self, sparse_job_row, db):
        """Call pattern (job_row, serpapi_key, conn, config) works with keyword args."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        _install_services(
            db,
            fetch_direct_jd=MagicMock(return_value=None),
            search_serpapi=MagicMock(return_value={"jd_full": "SerpAPI JD."}),
            search_duckduckgo=MagicMock(return_value=None),
        )

        result = enrich_job(
            sparse_job_row,
            "test-serp-key",
            db,
            {"scoring": {}},
        )

        assert isinstance(result, dict)

    def test_enrich_job_never_raises(self, sparse_job_row, db):
        """All exceptions caught, empty dict returned."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"

        _install_services(
            db, fetch_direct_jd=MagicMock(side_effect=Exception("Unexpected catastrophic error"))
        )

        result = enrich_job(sparse_job_row, serpapi_key="test-key", conn=db)

        assert isinstance(result, dict)

    def test_enrich_job_returns_empty_dict_when_nothing_missing(self, rich_job_row):
        """enrich_job returns empty dict when job already has all scoring-relevant data."""
        from jobcannon.engine.data_enricher import enrich_job

        mock_fetch = MagicMock()
        mock_serpapi = MagicMock()
        _install_services(fetch_direct_jd=mock_fetch, search_serpapi=mock_serpapi)

        result = enrich_job(rich_job_row, serpapi_key="test-key")

        mock_fetch.assert_not_called()
        mock_serpapi.assert_not_called()
        assert result == {}

    def test_enrich_job_skips_serpapi_when_no_key(self, sparse_job_row, db):
        """enrich_job skips SerpAPI tier when serpapi_key is None."""
        from jobcannon.engine.data_enricher import enrich_job

        sparse_job_row["source_urls"] = "[]"
        sparse_job_row["company_id"] = None

        mock_serpapi = MagicMock(return_value=None)
        _install_services(
            db,
            fetch_direct_jd=MagicMock(return_value=None),
            search_serpapi=mock_serpapi,
            search_duckduckgo=MagicMock(return_value="Some DDG text about the company."),
        )

        enrich_job(
            sparse_job_row,
            serpapi_key=None,
            conn=db,
            config={},
        )

        mock_serpapi.assert_not_called()

    def test_tier_order_constant_exported(self):
        """TIER_ORDER constant is exported from data_enricher (synthesis-free)."""
        from jobcannon.engine.data_enricher import TIER_ORDER

        assert isinstance(TIER_ORDER, list)
        assert "free" in TIER_ORDER
        assert "ddg" in TIER_ORDER
        assert "serpapi" in TIER_ORDER
        assert "agentic" in TIER_ORDER
        assert "exhausted" in TIER_ORDER
        # Synthesis tiers removed in Phase 2b sub-fix RC4
        assert "low" not in TIER_ORDER
        assert "mid" not in TIER_ORDER
        assert TIER_ORDER.index("free") < TIER_ORDER.index("ddg")
        assert TIER_ORDER.index("ddg") < TIER_ORDER.index("serpapi")
        assert TIER_ORDER.index("serpapi") < TIER_ORDER.index("agentic")
        assert TIER_ORDER.index("agentic") < TIER_ORDER.index("exhausted")


# ---------------------------------------------------------------------------
# Not ported: migration + host-only integration tests
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "All 4 tests either drive real job_finder.web migrations (m008) "
        "through a live MigrationContext, or inspect "
        "job_finder.web.scoring_runner.run_scoring's source for a "
        "'fetch_jd' reference. The engine has no migrations system and no "
        "scoring_runner counterpart (host-owned, not ported -- see "
        "CLAUDE.md's engine/host split). Nothing here tests data_enricher.py "
        "itself."
    )
)
class TestPipelineIntegration:
    def test_run_scoring_does_not_call_fetch_jd(self):
        pass

    def test_enrichment_tier_column_exists_after_migration(self):
        pass

    def test_existing_enriched_jobs_marked_serpapi_after_migration(self):
        pass

    def test_existing_unenriched_jobs_have_null_tier_after_migration(self):
        pass


@pytest.mark.skip(
    reason=(
        "Exercises enrichment_tiers._fetch_direct_jd directly against a "
        "mocked requests.get. enrichment_tiers.py is L-0178 (HOLD, "
        "unlanded) -- see module docstring."
    )
)
class TestFetchDirectJd:
    def test_successful_url_returns_text(self):
        pass

    def test_strips_script_style_nav_footer_header_tags(self):
        pass

    def test_caps_result_at_storage_limit(self):
        pass

    def test_returns_none_on_timeout(self):
        pass

    def test_returns_none_on_404(self):
        pass

    def test_returns_none_for_none_url(self):
        pass

    def test_returns_none_on_connection_error(self):
        pass


@pytest.mark.skip(
    reason=(
        "Exercises enrichment_tiers._fetch_direct_jd's auth-wall guard "
        "directly against a mocked requests.get. Same L-0178 HOLD reason as "
        "TestFetchDirectJd above."
    )
)
class TestAuthWallGuard:
    def test_returns_none_for_linkedin_login_page(self):
        pass

    def test_returns_none_for_sign_in_or_join(self):
        pass

    def test_returns_none_for_captcha_page(self):
        pass

    def test_returns_none_for_access_denied(self):
        pass

    def test_allows_normal_jd_through(self):
        pass

    def test_auth_wall_check_is_case_insensitive(self):
        pass

    def test_rejects_spa_shell_with_only_title(self):
        pass


@pytest.mark.skip(
    reason=(
        "All 3 tests drive real job_finder.web migration m015 through a "
        "live MigrationContext. The engine has no migrations system "
        "(host-owned, not ported -- see CLAUDE.md's engine/host split)."
    )
)
class TestMigration15:
    def test_migration_15_nullifies_poison_jd_full(self):
        pass

    def test_migration_15_deletes_notification_rows(self):
        pass

    def test_migration_15_promotes_descriptions(self):
        pass


# ---------------------------------------------------------------------------
# Description promotion tests
# ---------------------------------------------------------------------------


class TestDescriptionPromotion:
    """Verify enrich_job auto-promotes long descriptions to jd_full.

    A description > 200 chars with jd_full=None should be promoted to
    jd_full. Short descriptions and existing jd_full values must not be
    affected. Uses the module-level ``db`` fixture (a superset of the
    private repo's separate ``promo_db`` fixture schema) — its
    ``_install_services()`` defaults already reproduce the private
    ``stub_enrichment_network`` "always miss" behavior these tests relied on.
    """

    def test_promotes_long_description_to_jd_full(self, db):
        """job_row with description > 200 chars and jd_full=None -> enrich_job sets jd_full."""
        from jobcannon.engine.data_enricher import enrich_job

        long_desc = "A" * 250  # > 200 chars
        job_row = {
            "dedup_key": "test|promo-job|remote",
            "title": "Promo Job",
            "company": "Test Co",
            "location": "Remote",
            "jd_full": None,
            "salary_min": None,
            "salary_max": None,
            "source_urls": "[]",
            "company_id": None,
            "enrichment_tier": None,
            "description": long_desc,
        }

        db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, description) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                job_row["dedup_key"],
                job_row["title"],
                job_row["company"],
                job_row["location"],
                long_desc,
            ),
        )
        db.commit()

        enrich_job(job_row, conn=db)

        assert job_row.get("jd_full") == long_desc, (
            f"Expected jd_full to be set to description, got: {job_row.get('jd_full')!r}"
        )

    def test_does_not_promote_short_description(self, db):
        """job_row with description < 200 chars and jd_full=None -> jd_full stays None."""
        from jobcannon.engine.data_enricher import enrich_job

        short_desc = "A brief description."  # < 200 chars
        job_row = {
            "dedup_key": "test|short-desc|remote",
            "title": "Short Job",
            "company": "Test Co",
            "location": "Remote",
            "jd_full": None,
            "salary_min": None,
            "salary_max": None,
            "source_urls": "[]",
            "company_id": None,
            "enrichment_tier": None,
            "description": short_desc,
        }

        enrich_job(job_row, conn=db)

        assert job_row.get("jd_full") is None, (
            f"Expected jd_full to remain None for short description, got: {job_row.get('jd_full')!r}"
        )

    def test_does_not_overwrite_existing_jd_full(self, db):
        """job_row with description > 200 chars and jd_full already set -> jd_full unchanged."""
        from jobcannon.engine.data_enricher import enrich_job

        existing_jd = "Existing full job description already stored."
        long_desc = "B" * 250  # > 200 chars
        job_row = {
            "dedup_key": "test|has-jd|remote",
            "title": "Has JD Job",
            "company": "Test Co",
            "location": "Remote",
            "jd_full": existing_jd,
            "salary_min": None,
            "salary_max": None,
            "source_urls": "[]",
            "company_id": None,
            "enrichment_tier": None,
            "description": long_desc,
        }

        enrich_job(job_row, conn=db)

        assert job_row.get("jd_full") == existing_jd, (
            f"Expected existing jd_full to be preserved, got: {job_row.get('jd_full')!r}"
        )

    def test_promotion_persists_to_db(self, db):
        """With conn provided, promotion UPDATE writes to DB."""
        from jobcannon.engine.data_enricher import enrich_job

        long_desc = "C" * 250  # > 200 chars
        dedup_key = "test|db-persist|remote"
        job_row = {
            "dedup_key": dedup_key,
            "title": "DB Persist Job",
            "company": "Test Co",
            "location": "Remote",
            "jd_full": None,
            "salary_min": None,
            "salary_max": None,
            "source_urls": "[]",
            "company_id": None,
            "enrichment_tier": None,
            "description": long_desc,
        }

        db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, description) "
            "VALUES (?, ?, ?, ?, ?)",
            (dedup_key, job_row["title"], job_row["company"], job_row["location"], long_desc),
        )
        db.commit()

        enrich_job(job_row, conn=db)

        row = db.execute("SELECT jd_full FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()

        assert row is not None, "Job row not found in DB"
        assert row["jd_full"] is not None, "Expected jd_full to be set in DB after promotion"
        assert row["jd_full"] == long_desc[:_JD_STORAGE_MAX_CHARS], (
            f"Expected jd_full in DB to match description (capped at "
            f"_JD_STORAGE_MAX_CHARS), got: {row['jd_full']!r}"
        )

    @pytest.mark.parametrize(
        "tier",
        ["agentic", "agentic_exhausted"],
        ids=["agentic", "agentic_exhausted"],
    )
    def test_truncated_description_at_low_signal_terminal_keeps_tier_null(self, db, tier):
        """Issue #1374 regression: enrich_job must not clobber the tier NULL-reset.

        A row at a LOW_SIGNAL_TERMINAL enrichment_tier (agentic /
        agentic_exhausted) with a truncated description (trailing ellipsis,
        > 200 chars) and no jd_full: the auto-promote step routes the
        description through set_jd_full, which content-rejects it
        (JD_TRUNCATED) and resets the terminal enrichment_tier to NULL so the
        row re-enters the regular pipeline. The tail fallback
        ``_persist(..., "exhausted", ...)`` in the SAME enrich_job call must
        NOT overwrite that NULL back to a terminal value, or the row is
        re-marooned and the reset is defeated.
        """
        from jobcannon.engine.data_enricher import enrich_job
        from jobcannon.engine.jd_content_contract import JD_TRUNCATED

        truncated_desc = "Senior Data Scientist at Acme building ML models. " * 7 + "..."
        assert len(truncated_desc) > 200

        dedup_key = "test|trunc-terminal|remote"
        db.execute(
            "INSERT INTO jobs (dedup_key, title, company, location, description, "
            "enrichment_tier, jd_full) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (dedup_key, "Senior Data Scientist", "Acme Corp", "Remote", truncated_desc, tier),
        )
        db.commit()

        job_row = {
            "dedup_key": dedup_key,
            "title": "Senior Data Scientist",
            "company": "Acme Corp",
            "location": "Remote",
            "jd_full": None,
            "salary_min": None,
            "salary_max": None,
            "source_urls": "[]",
            "company_id": None,
            "enrichment_tier": tier,
            "description": truncated_desc,
        }

        result = enrich_job(job_row, conn=db)

        assert result == {}

        row = db.execute(
            "SELECT jd_full, unresolved_reasons, enrichment_tier FROM jobs WHERE dedup_key = ?",
            (dedup_key,),
        ).fetchone()
        assert row["jd_full"] is None
        assert JD_TRUNCATED in json.loads(row["unresolved_reasons"])
        assert row["enrichment_tier"] is None, (
            f"enrichment_tier clobbered back to {row['enrichment_tier']!r}; "
            "the content-reject NULL-reset was defeated by the tail fallback _persist"
        )


# ---------------------------------------------------------------------------
# Stub-JD gate: _is_stub_jd, _find_missing_fields, _resolve_from_fragments
# ---------------------------------------------------------------------------


class TestStubJdGate:
    """Verify that stub/truncated JDs are rejected by _find_missing_fields and
    _resolve_from_fragments. Pure functions -- no DB/services fixture needed.
    """

    _STUB = "Software Engineer at Acme Corp"  # 30 chars — well below 200
    _REAL = "x" * 201  # 201 chars — above the 200-char threshold
    _REAL_URL_JD = "y" * 201  # a real URL-fetched JD
    _SNIPPET = "x" * 227 + "..."  # 230 chars, trailing ellipsis -> truncated

    # ------------------------------------------------------------------ #
    # _is_stub_jd
    # ------------------------------------------------------------------ #

    def test_is_stub_jd_none_is_stub(self):
        """None jd_text → stub."""
        from jobcannon.engine.data_enricher import _is_stub_jd

        assert _is_stub_jd(None) is True

    def test_is_stub_jd_empty_is_stub(self):
        """Empty string → stub."""
        from jobcannon.engine.data_enricher import _is_stub_jd

        assert _is_stub_jd("") is True

    def test_is_stub_jd_short_is_stub(self):
        """Text shorter than _MIN_JD_LENGTH after strip → stub."""
        from jobcannon.engine.data_enricher import _is_stub_jd

        assert _is_stub_jd(self._STUB) is True

    def test_is_stub_jd_exactly_200_is_not_stub(self):
        """Text of exactly 200 chars → NOT a stub (threshold is < 200)."""
        from jobcannon.engine.data_enricher import _is_stub_jd

        assert _is_stub_jd("x" * 200) is False

    def test_is_stub_jd_real_jd_not_stub(self):
        """Text of 201+ chars → NOT a stub."""
        from jobcannon.engine.data_enricher import _is_stub_jd

        assert _is_stub_jd(self._REAL) is False

    def test_is_stub_jd_whitespace_only_is_stub(self):
        """Whitespace-only (collapses to empty on strip) → stub."""
        from jobcannon.engine.data_enricher import _is_stub_jd

        assert _is_stub_jd("   \n\t  ") is True

    # ------------------------------------------------------------------ #
    # _find_missing_fields — stub JD treated as missing
    # ------------------------------------------------------------------ #

    def test_find_missing_fields_stub_jd_is_missing(self):
        """A stub jd_full (< 200 chars) is treated as missing."""
        from jobcannon.engine.data_enricher import _find_missing_fields

        row = {"jd_full": self._STUB, "title": "SWE", "company": "Acme", "salary_min": 100_000}
        assert "jd_full" in _find_missing_fields(row)

    def test_find_missing_fields_real_jd_not_missing(self):
        """A real jd_full (>= 200 chars) is NOT treated as missing."""
        from jobcannon.engine.data_enricher import _find_missing_fields

        row = {"jd_full": self._REAL, "title": "SWE", "company": "Acme", "salary_min": 100_000}
        assert "jd_full" not in _find_missing_fields(row)

    def test_find_missing_fields_none_jd_is_missing(self):
        """None jd_full → missing (baseline regression guard)."""
        from jobcannon.engine.data_enricher import _find_missing_fields

        row = {"jd_full": None, "salary_min": 100_000}
        assert "jd_full" in _find_missing_fields(row)

    # ------------------------------------------------------------------ #
    # _resolve_from_fragments — stub fragments rejected
    # ------------------------------------------------------------------ #

    def test_resolve_rejects_stub_jd_fragment(self):
        """A stub jd_full in fragments is NOT returned."""
        from jobcannon.engine.data_enricher import _resolve_from_fragments

        fragments = {"jd_full": self._STUB}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert "jd_full" not in result

    def test_resolve_rejects_stub_url_jd_fragment(self):
        """A stub url_jd (< 200 chars) is NOT mapped to jd_full."""
        from jobcannon.engine.data_enricher import _resolve_from_fragments

        fragments = {"url_jd": self._STUB}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert "jd_full" not in result

    def test_resolve_accepts_real_jd_fragment(self):
        """A real jd_full (>= 200 chars) IS returned."""
        from jobcannon.engine.data_enricher import _resolve_from_fragments

        fragments = {"jd_full": self._REAL}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert result.get("jd_full") == self._REAL

    def test_resolve_accepts_real_url_jd_fragment(self):
        """A real url_jd (>= 200 chars) IS mapped to jd_full."""
        from jobcannon.engine.data_enricher import _resolve_from_fragments

        fragments = {"url_jd": self._REAL}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert result.get("jd_full") == self._REAL

    def test_resolve_stub_does_not_block_non_jd_fields(self):
        """Stub jd rejection does not prevent other fields (salary_min) from resolving."""
        from jobcannon.engine.data_enricher import _resolve_from_fragments

        fragments = {"jd_full": self._STUB, "salary_min": 120_000}
        result = _resolve_from_fragments(
            fragments, ["jd_full", "salary_min"], {"title": "SWE", "company": "Acme"}
        )
        assert "jd_full" not in result
        assert result.get("salary_min") == 120_000

    # ------------------------------------------------------------------ #
    # Issue #1295: truncated snippets must not be persisted as jd_full
    # ------------------------------------------------------------------ #

    def test_is_stub_jd_ellipsis_snippet_is_stub(self):
        """A long snippet ending in '...' is treated as a stub."""
        from jobcannon.engine.data_enricher import _is_stub_jd

        assert _is_stub_jd(self._SNIPPET) is True

    def test_resolve_prefers_url_jd_when_jd_full_is_truncated_snippet(self):
        """A truncated snippet jd_full is ignored in favor of a real url_jd."""
        from jobcannon.engine.data_enricher import _resolve_from_fragments

        fragments = {"jd_full": self._SNIPPET, "url_jd": self._REAL_URL_JD}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert result.get("jd_full") == self._REAL_URL_JD

    def test_resolve_truncated_snippet_without_url_is_empty(self):
        """A truncated snippet alone does not satisfy a missing jd_full."""
        from jobcannon.engine.data_enricher import _resolve_from_fragments

        fragments = {"jd_full": self._SNIPPET}
        result = _resolve_from_fragments(
            fragments, ["jd_full"], {"title": "SWE", "company": "Acme"}
        )
        assert "jd_full" not in result

    def test_find_missing_fields_truncated_snippet_is_missing(self):
        """A row whose jd_full is a truncated snippet is still missing jd_full."""
        from jobcannon.engine.data_enricher import _find_missing_fields

        row = {"jd_full": self._SNIPPET, "title": "SWE", "company": "Acme", "salary_min": 100_000}
        assert "jd_full" in _find_missing_fields(row)

# PORTED from tests/test_agentic_enricher.py @ cb30fe6464ae6ce86008e0dd2eb11123afebc29d (private job-cannon). Ledger L-0529.
"""Tests for jobcannon.engine.agentic_enricher.

Ported from tests/test_agentic_enricher.py (job-cannon private repo) at the
carry SHA the module itself was ported from (ledger L-0132, public PR #378).
Every test class ports; nothing is dropped wholesale. What changed and why:

# PORT-SEAM: this module docstring is new prose replacing the private
# module's docstring wholesale; see the per-test PORT-SEAM comments below for
# each individual adaptation's exact hunk.

  1. call_model injection: the public module threads `call_model` as a
     REQUIRED keyword-only parameter on every function that needs it
     (`_generate_queries` / `_validate_page` / `enrich_single_job` /
     `run_agentic_backfill`) instead of importing the private repo's
     `job_finder.web.model_provider.call_model` at call time (see
     agentic_enricher.py's own module docstring PORT-SEAM note). Every
     `patch("job_finder.web.model_provider.call_model", ...)` context manager
     becomes a `call_model=MagicMock(...)` keyword argument passed directly
     into the call under test — no patching needed. `_make_mock_provider`
     (built an OllamaProvider-shaped MagicMock) is dropped as unused; the
     module never constructs a provider object anymore.
  2. DB/services wiring: the private `migrated_db_path` / `migrated_db_mem`
     fixtures (real sqlite3 files migrated by the private migrations system,
     from `tests/conftest.py`) are replaced by same-named LOCAL fixtures
     below, backed by a minimal `_SCHEMA` covering only the columns this
     module's SQL touches (mirrors tests/engine/test_data_enricher.py's own
     `db` fixture / `_SCHEMA` precedent, L-0174). `tests/conftest.py` is
     therefore NOT carried — there is no root conftest in this repo for it
     to extend, and its `migrated_db_path`/`migrated_db_mem` fixtures have no
     public equivalent to wrap.
  3. `_set_jd_full` (a private module-level alias for
     `job_finder.db._jd_full.set_jd_full`) has no public counterpart --
     the port calls `svc.set_jd_full` (a ScanServices seam) instead. Tests
     that patched `agentic_enricher._set_jd_full` now override
     `set_jd_full` on the installed ScanServices via `_install_services`.
     `_fake_set_jd_full` below reuses the REAL ported
     `jobcannon.engine.jd_content_contract._is_jd_junk` gate (same technique
     as test_data_enricher.py's own `_fake_set_jd_full`, simplified: no
     unresolved_reasons/LOW_SIGNAL_TERMINAL bookkeeping, since no test
     carried here reads that surface).
  4. `db_path` dropped from `run_agentic_backfill`'s signature --
     `svc.connection_factory()` is zero-arg (L-0465 precedent). Every
     `run_agentic_backfill(path, {}, limit=10)` call becomes
     `run_agentic_backfill({}, limit=10, call_model=...)` with `path` moved
     into `_install_services(path, ...)` so `connection_factory` opens real
     connections against it (reusing
     `tests/engine/helpers/ats_scan_services.py`'s `make_connection_factory`
     — same "fresh connection per call, on-disk file" semantics the private
     `db_helpers.standalone_connection(db_path)` had, which
     `test_passes_open_conn_to_enrich_single_job` specifically regression-
     guards).
  5. Patch targets collapse onto one module: the private repo's
     `job_finder.web.enrichment_tiers
     .{fetch_linkedin_jd,is_short_auth_page,is_chrome_or_login_page}` are
     inlined into agentic_enricher.py itself (L-0178 boundary-guard note in
     the module docstring), so patches retarget to
     `jobcannon.engine.agentic_enricher.<name>`. `_http_constants._TIMEOUT`
     moves from `job_finder.web` to `jobcannon.engine`.
  6. `_MAX_JD_CHARS` (private module constant `= JD_STORAGE_MAX_CHARS`) has
     no public alias — `get_services().jd_storage_max_chars` reads it
     instead (agentic_enricher.py's own PORT-SEAM comment). Assertions
     against the private literal `16000  # _MAX_JD_CHARS * 2` comment (which
     no longer matches JD_STORAGE_MAX_CHARS=50_000 even privately — a stale
     comment, not a live invariant) are rewritten to assert against the
     installed `jd_storage_max_chars` value directly rather than a magic
     number, since every string used in these tests is far shorter than any
     plausible trim width anyway.
  7. OllamaProvider `sys.modules` patching (the private repo's
     `job_finder.web.providers.ollama_provider`) is dropped everywhere — the
     public module never imports or
     constructs a provider object post-port. The `playwright.sync_api`
     `sys.modules` patch stays: `run_agentic_backfill` still does a real
     (try/except-guarded) `from playwright.sync_api import sync_playwright`.
  8. `tests/helpers/timeouts.py`'s two timeout constants are carried
     verbatim (`SUBPROCESS_HANG_TIMEOUT_S` has no public equivalent —
     grepped `tests/helpers/` and found only `ats_session.py`); the file's
     own module docstring is adapted (private incident specifics and issue
     numbers redacted, per this program's redaction rule).
     `tests/helpers/subprocess_lockdown.py` is NOT carried: its own
     docstring and `_LOCKDOWN_DIR.is_dir()` assert require
     `tests/network_lockdown.py` + `tests/_subprocess_lockdown/
     sitecustomize.py` to exist (neither does — grepped the whole `tests/`
     tree for `network_lockdown` / `_subprocess_lockdown`, zero hits). This
     repo has no repo-wide network-lockdown convention to extend into a
     child process, so carrying the helper verbatim would fail its own
     precondition assert. `test_import_and_fetch_survive_missing_playwright`
     is adapted to spawn its subprocess with a plain `env` dict instead of
     `locked_down_env(env)` — see the `# PORT-SEAM` comment at that call
     site. `tests/conftest.py` is likewise not carried (see point 2).

Everything else ports with unchanged assertions and behavior.
"""

import contextlib  # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from jobcannon.engine import services
from jobcannon.engine.jd_content_contract import _is_jd_junk
from jobcannon.engine.model_types import ModelResult
from tests.engine.helpers.ats_scan_services import (
    make_connection_factory,
)  # PORT-SEAM: builtin TimeoutError replaces PlaywrightTimeoutError (playwright not installed in this dev venv)
from tests.helpers.timeouts import SUBPROCESS_HANG_TIMEOUT_S

# PORT-SEAM: parity with jobcannon/host/wiring.py's own
# `_JD_STORAGE_MAX_CHARS = 50_000` (and, by coincidence, the private repo's
# JD_STORAGE_MAX_CHARS at the carry SHA — both 50_000). Mirrors
# tests/engine/test_data_enricher.py's identical local constant.
_JD_STORAGE_MAX_CHARS = 50_000

# PORT-SEAM: minimal schema replacing the private repo's fully-migrated
# `migrated_db_path`/`migrated_db_mem` fixtures — only the columns
# agentic_enricher.py's SQL literally references (verified against
# jobcannon/engine/agentic_enricher.py's run_agentic_backfill /
# requeue_or_expire_agentic_exhausted bodies), plus salary_min/location for
# the hand-rolled backfill_skip_sql regression test below.
_SCHEMA = """
CREATE TABLE jobs (
    dedup_key TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    jd_full TEXT DEFAULT NULL,
    enrichment_tier TEXT DEFAULT NULL,
    classification TEXT DEFAULT NULL,
    sub_scores_json TEXT DEFAULT NULL,
    location_policy_verdict TEXT DEFAULT NULL,
    first_seen TEXT DEFAULT NULL,
    agentic_exhausted_at TEXT DEFAULT NULL,
    agentic_retry_count INTEGER NOT NULL DEFAULT 0,
    salary_min INTEGER DEFAULT NULL,
    location TEXT NOT NULL DEFAULT ''
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model_result(data) -> ModelResult:  # type: ignore[type-arg]
    """Create a ModelResult as call_model would return.
    data may be a dict OR a list — call_model returns whatever the model
    decoded, which for query-generation prompts is a JSON array (list).  # PORT-SEAM: OllamaProvider sys.modules injection dropped; call_model is the seam now
    """
    return ModelResult(
        data=data,  # type: ignore[arg-type]
        cost_usd=0.0,
        input_tokens=50,
        output_tokens=20,
        model="qwen2.5:14b",
        provider="ollama",
        schema_valid=True,
    )


# PORT-SEAM: private-only OllamaProvider mock helper removed; call_model is injected directly
def _make_migrated_db(path: str) -> tuple[str, sqlite3.Connection]:
    """Open a connection to an already-schema'd DB path (see migrated_db_path fixture)."""
    conn = sqlite3.connect(
        path
    )  # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
    conn.row_factory = sqlite3.Row
    return path, conn


def _insert_job(conn: sqlite3.Connection, dedup_key: str, **kwargs) -> None:
    """Insert a minimal job row into the test DB."""  # PORT-SEAM: minimal test schema replaces the private repo's fully migrated schema
    defaults = {
        "title": "Data Scientist",  # PORT-SEAM: minimal test schema replaces the private repo's fully migrated schema
        "company": "Acme Corp",
        "jd_full": None,
        # PORT-SEAM: minimal test schema replaces the private repo's fully migrated schema
        "enrichment_tier": "exhausted",
        "classification": "consider",
        "sub_scores_json": (
            '{"title_fit": 3, "location_fit": 4, "comp_fit": 3, '
            '"domain_match": 3, "seniority_match": 3, "skills_match": 4}'
        ),
        "location_policy_verdict": None,
        "first_seen": "2026-01-01T00:00:00",
        "agentic_exhausted_at": None,
        "agentic_retry_count": 0,
        "salary_min": None,
        "location": "Remote",
        # PORT-SEAM: minimal test schema replaces the private repo's fully migrated schema
    }
    defaults.update(
        kwargs
    )  # PORT-SEAM: minimal test schema replaces the private repo's fully migrated schema
    conn.execute(
        """INSERT OR REPLACE INTO jobs
        (dedup_key, title, company, jd_full, enrichment_tier, classification,
         sub_scores_json, location_policy_verdict, first_seen,
         agentic_exhausted_at, agentic_retry_count, salary_min, location)
        """
        # PORT-SEAM: minimal test schema replaces the private repo's fully migrated schema
        """VALUES
        (:dedup_key, :title, :company, :jd_full, :enrichment_tier, :classification,
         :sub_scores_json, :location_policy_verdict, :first_seen,
         :agentic_exhausted_at, :agentic_retry_count, :salary_min, :location)""",  # PORT-SEAM: minimal test schema replaces the private repo's fully migrated schema
        {"dedup_key": dedup_key, **defaults},
    )  # PORT-SEAM: minimal test schema replaces the private repo's fully migrated schema
    conn.commit()


def _fake_set_jd_full(conn, dedup_key, text, *, source=None, title=None, config=None):
    """Test-local stand-in for the ScanServices.set_jd_full seam.

    Reuses the REAL ported jd_content_contract._is_jd_junk gate (mirrors
    tests/engine/test_data_enricher.py's own _fake_set_jd_full) so the
    junk-JD-gate test below exercises real content-gating logic, not a
    no-op stub.
    """
    if text is None or _is_jd_junk(text):
        return False
    conn.execute(
        "UPDATE jobs SET jd_full = ? WHERE dedup_key = ?",
        (text[:_JD_STORAGE_MAX_CHARS], dedup_key),
    )
    conn.commit()
    return True


# PORT-SEAM: replaces every `patch("job_finder.web.db_helpers.standalone_
# connection", ...)` / private ScanServices-less DB access — installs a
# ScanServices bundle whose connection_factory is either a real on-disk
# factory (db_path_or_conn is a str) or a fixed pass-through (a raw conn or
# None), matching tests/engine/test_data_enricher.py's `_install_services`
# convention.
def _install_services(db_path_or_conn=None, **overrides):
    if isinstance(db_path_or_conn, str):
        connection_factory = make_connection_factory(db_path_or_conn)
    else:

        @contextlib.contextmanager
        def connection_factory(*, synchronous="FULL"):
            yield db_path_or_conn

    defaults = dict(
        connection_factory=connection_factory,
        upsert_job=MagicMock(),
        set_jd_full=_fake_set_jd_full,
        upsert_company=MagicMock(),
        config={},
        get_secret=MagicMock(return_value=None),
        jd_storage_max_chars=_JD_STORAGE_MAX_CHARS,
    )
    defaults.update(overrides)
    services.set_services(services.ScanServices(**defaults))


@pytest.fixture(autouse=True)
def _default_services():
    """Baseline ScanServices for tests that never touch a real DB
    (TestGenerateQueries / TestValidatePage / TestSocialPostUrlFilter /
    TestSearchDdg / most of TestEnrichSingleJob*) — they only need
    `jd_storage_max_chars` for the final trim. Tests that need a real
    on-disk DB re-call `_install_services(path, **overrides)` themselves;
    this fixture's teardown still runs exactly once regardless of how many
    times a test re-registers services mid-body.
    """
    _install_services()
    yield
    services.clear_services()


@pytest.fixture
def migrated_db_path(tmp_path):
    """(path) — a fresh on-disk SQLite DB file with _SCHEMA applied.

    Replaces the private repo's fully-migrated `migrated_db_path` fixture
    (see module docstring point 2).
    """
    path = str(tmp_path / "agentic_backfill.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    yield path


@pytest.fixture
def migrated_db_mem():
    """(conn) — an in-memory SQLite DB with _SCHEMA applied.

    Replaces the private repo's `migrated_db_mem` fixture. Used only by
    TestRequeueOrExpireAgenticExhausted, which calls
    requeue_or_expire_agentic_exhausted(conn, config) directly against a raw
    connection — that function takes no ScanServices seam.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# _generate_queries()
# ---------------------------------------------------------------------------


class TestGenerateQueries:
    def test_list_response_shape(self):
        """call_model returns a JSON array — queries extracted directly."""
        from jobcannon.engine.agentic_enricher import _generate_queries

        queries = ["Acme Corp Data Scientist", "site:greenhouse.io Acme Data Scientist"]
        # PORT-SEAM: call_model injected directly (required keyword-only param)
        # instead of patch("job_finder.web.model_provider.call_model", ...).
        result = _generate_queries(
            "Data Scientist",
            "Acme Corp",
            n=2,
            conn=None,
            config={},
            call_model=MagicMock(return_value=_make_model_result(queries)),
        )
        assert result == queries[:2]

    def test_dict_queries_key(self):
        """call_model returns {'queries': [...]} — extracts from 'queries' key."""
        from jobcannon.engine.agentic_enricher import _generate_queries

        result = _generate_queries(
            "Engineer",
            "BetterHelp",
            n=3,
            conn=None,
            config={},
            call_model=MagicMock(return_value=_make_model_result({"queries": ["q1", "q2", "q3"]})),
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert result == ["q1", "q2", "q3"]

    # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
    def test_dict_search_queries_key(self):
        """call_model returns {'search_queries': [...]} — extracts from 'search_queries' key."""
        from jobcannon.engine.agentic_enricher import _generate_queries

        result = _generate_queries(
            "ML Engineer",
            "Stripe",
            n=4,
            conn=None,
            config={},
            call_model=MagicMock(
                return_value=_make_model_result({"search_queries": ["sq1", "sq2"]})
            ),
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert result == ["sq1", "sq2"]

    # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
    def test_provider_exception_fallback(self):
        """On call_model exception, falls back to heuristic queries."""
        from jobcannon.engine.agentic_enricher import _fallback_queries, _generate_queries

        result = _generate_queries(
            "Staff Data Scientist",
            "Stripe",
            n=4,
            conn=None,
            config={},
            call_model=MagicMock(side_effect=RuntimeError("connection refused")),
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        fallback = _fallback_queries("Staff Data Scientist", "Stripe")
        assert (
            result == fallback
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch

    def test_malformed_response_fallback(self):
        """Unrecognized data shape falls back to heuristic queries."""
        from jobcannon.engine.agentic_enricher import _generate_queries

        result = _generate_queries(
            "Analyst",
            "Uber",
            n=3,
            conn=None,
            config={},
            call_model=MagicMock(return_value=_make_model_result({"unexpected_key": 42})),
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        # Should be non-empty fallback queries
        assert (
            len(result) > 0
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert all(isinstance(q, str) for q in result)

    def test_no_json_loads_called_on_result_data(self):
        """result.data is consumed directly — json.loads must NOT be called."""
        import jobcannon.engine.agentic_enricher as mod

        # If json.loads were called on a list, it would raise TypeError.
        # Absence of error proves no json.loads() call on result.data.
        result = mod._generate_queries(
            "DS",
            "Co",
            n=2,
            conn=None,
            config={},
            call_model=MagicMock(return_value=_make_model_result(["q1", "q2"])),
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert result == ["q1", "q2"]


# PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch

# ---------------------------------------------------------------------------
# _validate_page()
# ---------------------------------------------------------------------------


class TestValidatePage:
    def test_match_true_extracts_confidence(self):
        """call_model returns is_match=true — returns (True, confidence) correctly."""
        from jobcannon.engine.agentic_enricher import _validate_page

        is_match, confidence = _validate_page(
            "Job posting for Data Scientist at Acme Corp",
            "Data Scientist",
            "Acme Corp",
            None,
            {},
            call_model=MagicMock(
                return_value=_make_model_result(
                    {"is_match": True, "confidence": 0.92, "reason": "exact"}
                )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            ),
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert is_match is True
        assert (
            abs(confidence - 0.92) < 0.001
        )  # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)

    def test_match_false(self):
        """call_model returns is_match=false — propagates as (False, ...)."""
        from jobcannon.engine.agentic_enricher import _validate_page

        is_match, _confidence = _validate_page(
            "Software Engineer at Some Company",
            "Data Scientist",
            "Acme Corp",
            None,
            {},
            call_model=MagicMock(
                return_value=_make_model_result(
                    {"is_match": False, "confidence": 0.1, "reason": "wrong role"}
                )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            ),
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert is_match is False

    # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
    def test_call_model_exception_returns_false_zero(self):
        """When call_model raises, _validate_page returns (False, 0.0)."""
        from jobcannon.engine.agentic_enricher import _validate_page

        is_match, confidence = _validate_page(
            "text",
            "title",
            "company",
            None,
            {},
            call_model=MagicMock(side_effect=RuntimeError("timeout")),
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert is_match is False
        assert (
            confidence == 0.0
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch

    def test_no_json_loads_on_result_data(self):
        """Validates that data is consumed as dict without json.loads()."""
        from jobcannon.engine.agentic_enricher import _validate_page

        # If json.loads were called on a dict, TypeError would propagate.
        is_match, _confidence = _validate_page(
            "some text",
            "title",
            "company",
            None,
            {},
            call_model=MagicMock(
                return_value=_make_model_result({"is_match": True, "confidence": 0.8})
            ),
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert is_match is True


# PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch

# ---------------------------------------------------------------------------
# enrich_single_job()
# ---------------------------------------------------------------------------


class TestEnrichSingleJob:
    # PORT-SEAM: private-only unused page-mock helper removed (_make_page_mock)
    def test_returns_jd_when_high_confidence_match(self):
        # PORT-SEAM: private-only unused page-mock helper removed
        """When a page matches with confidence >= 0.5, returns trimmed JD text."""
        from jobcannon.engine.agentic_enricher import enrich_single_job

        long_jd = (
            "Acme Corp is hiring a Data Scientist. Responsibilities include "
            "building and maintaining machine learning models, partnering with "
            "engineering, and communicating insights to stakeholders. "
            "Qualifications: experience with Python, SQL, and statistics. " * 5
        )

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        with (
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            patch("jobcannon.engine.agentic_enricher._search_ddg") as mock_ddg,
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            patch("jobcannon.engine.agentic_enricher._fetch_page_text") as mock_fetch,
        ):
            mock_ddg.return_value = [
                {"href": "https://boards.greenhouse.io/acme/jobs/1", "title": "t", "body": "b"}
            ]
            mock_fetch.return_value = long_jd

            result = enrich_single_job(
                job_row,
                page,
                conn=None,
                config={},
                call_model=MagicMock(
                    side_effect=[
                        # First call: _generate_queries
                        _make_model_result(["Acme Corp Data Scientist site:linkedin.com"]),
                        # Second call: _validate_page
                        _make_model_result(
                            {"is_match": True, "confidence": 0.85, "reason": "match"}
                        ),
                    ]
                ),
            )

        assert (
            result is not None
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        # PORT-SEAM: private `_MAX_JD_CHARS` import replaced — no module-level
        # alias exists post-port; read the installed seam value directly.
        from jobcannon.engine.services import get_services

        assert len(result) <= get_services().jd_storage_max_chars  # trimmed to the JD storage cap

    def test_content_rejected_page_skips_validation_and_continues(self):
        """A deterministically-junk page (CMS placeholder scaffold) is skipped
        via the jd_content_reject pre-filter BEFORE the LLM validate call —
        it never wins the "best match" slot, and the loop tries the next
        candidate URL instead of stopping on a false-positive match.
        """
        from jobcannon.engine.agentic_enricher import enrich_single_job

        junk_page = (
            "News from Acme Corp. Find a Job that Suit With Your Passion. "
            "Your engaging subtitle goes here. Provide a brief description "
            "of the role. " * 4
        )
        real_jd = "Acme Corp Data Scientist. " + "Responsibilities include modeling. " * 20

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        def _fetch_side_effect(_page, url, *args, **kwargs):
            return junk_page if "junky" in url else real_jd

        with (
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            patch("jobcannon.engine.agentic_enricher._search_ddg") as mock_ddg,
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            patch("jobcannon.engine.agentic_enricher._fetch_page_text") as mock_fetch,
            patch("jobcannon.engine.agentic_enricher._validate_page") as mock_validate,
        ):
            mock_ddg.return_value = [
                {"href": "https://acme.com/careers/junky-page", "title": "t1", "body": "b1"},
                {"href": "https://boards.greenhouse.io/acme/jobs/1", "title": "t2", "body": "b2"},
            ]
            mock_fetch.side_effect = _fetch_side_effect
            mock_validate.return_value = (True, 0.9)

            result = enrich_single_job(
                job_row,
                page,
                conn=None,
                config={},
                call_model=MagicMock(return_value=_make_model_result(["Acme Corp Data Scientist"])),
            )

        assert (
            result is not None
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert "Responsibilities" in result
        # _validate_page must be called exactly once — only for the non-junk
        # URL. The CMS-placeholder page is skipped before ever reaching the LLM.
        mock_validate.assert_called_once()
        assert mock_validate.call_args[0][0] == real_jd

    def test_generic_careers_landing_page_rejected_specific_posting_accepted(self):
        """A generic careers-page template must not be accepted as this job's
        JD, even when the LLM is fooled into calling it a match — the
        deterministic CLEAN-verdict gate (JD-shape signal + title grounding)
        is what must reject it. A genuinely posting-specific page for the
        same company/title from a later candidate URL is still accepted.
        # PORT-SEAM: test rationale paragraph trimmed for the public port (redundant narrative dropped; assertions unchanged)
        """
        # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
        from jobcannon.engine.agentic_enricher import enrich_single_job

        generic_landing_page = (
            "Welcome to Acme Corp Careers! We're building the future together "
            "and looking to grow our team across every department. Explore "
            "open roles in engineering, sales, and design, including our Data "
            "Scientist openings. We offer great benefits, flexible schedules, "
            "and a collaborative culture where everyone can thrive. Join us "
            "today and help shape what comes next. " * 6
        )
        real_posting_page = (
            "Acme Corp Data Scientist. Responsibilities include building and "
            "maintaining predictive models, partnering with engineering, and "
            "communicating insights to stakeholders. Qualifications: "
            "experience with Python, SQL, and statistical modeling. " * 6
        )

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        def _fetch_side_effect(_page, url, *args, **kwargs):
            return generic_landing_page if "careers-landing" in url else real_posting_page

        with (
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            # _rank_urls sorts by domain_priority (ATS platforms first), which
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            # would otherwise reorder the greenhouse.io URL ahead of the plain
            # company domain and mask what this test targets. Pin the fetch
            # order explicitly: the generic landing page must be tried FIRST
            # (and rejected) before the loop reaches the real posting.
            patch("jobcannon.engine.agentic_enricher._search_ddg") as mock_ddg,  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            patch("jobcannon.engine.agentic_enricher._rank_urls") as mock_rank,
            patch(
                "jobcannon.engine.agentic_enricher._fetch_page_text"
            ) as mock_fetch,  # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
            patch("jobcannon.engine.agentic_enricher._validate_page") as mock_validate,
        ):
            mock_ddg.return_value = [
                {"href": "https://acme.com/careers-landing", "title": "t1", "body": "b1"},
                {"href": "https://boards.greenhouse.io/acme/jobs/1", "title": "t2", "body": "b2"},
            ]
            mock_rank.return_value = [
                "https://acme.com/careers-landing",
                "https://boards.greenhouse.io/acme/jobs/1",
            ]
            mock_fetch.side_effect = _fetch_side_effect
            # The LLM says match+high-confidence for BOTH pages — proving the
            # deterministic shape gate, not the LLM, is what rejects the
            # generic page.
            mock_validate.return_value = (True, 0.9)

            result = enrich_single_job(
                job_row,
                page,
                conn=None,
                config={},
                call_model=MagicMock(return_value=_make_model_result(["Acme Corp Data Scientist"])),
            )

        assert (
            result is not None
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert "Responsibilities" in result
        assert "Welcome to Acme Corp Careers" not in result
        assert mock_validate.call_count == 2

    # PORT-SEAM: explanatory comment on the jd_content_reject pre-filter trimmed for the public port (behavior/assertions unchanged)
    # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)

    def test_concurrent_ddg_queries_merge_and_deduplicate(self):
        """All generated DDG queries are searched concurrently and their results
        are merged and ranked in the same query order as the serial path.
        """
        from jobcannon.engine.agentic_enricher import (
            _MAX_SEARCH_QUERIES,
            enrich_single_job,
        )

        long_jd = (
            "Acme Corp is hiring a Data Scientist. Responsibilities include "
            "building and maintaining machine learning models, partnering with "
            "engineering, and communicating insights to stakeholders. "
            "Qualifications: experience with Python, SQL, and statistics. " * 5
        )

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        queries = [
            "Acme careers Data Scientist",
            "site:linkedin.com Acme Data Scientist",
            "site:greenhouse.io Acme Data Scientist",
            "site:lever.co Acme Data Scientist",
        ]

        def _ddg_side_effect(query, max_results):
            return [
                {
                    "href": f"https://example.com/{query.replace(' ', '-')}",
                    "title": "t",
                    "body": "b",
                }
            ]

        # Scope the time.sleep mock to agentic_enricher's namespace only.
        # ``patch("...agentic_enricher.time.sleep")`` patches ``sleep`` on the
        # global ``time`` module object, so any concurrent thread calling
        # ``time.sleep`` gets recorded by the mock. Replacing the ``time``
        # *reference* in agentic_enricher with a wraps-mock keeps real
        # ``time.time()`` / ``time.monotonic()`` working while isolating the
        # ``sleep`` assertion to agentic_enricher's own call site.  # PORT-SEAM: comment shortened for the public port; private CI-environment-specific digression dropped (behavior/assertion unchanged)
        import time as _real_time

        # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
        mock_time = MagicMock(wraps=_real_time)

        with (
            patch("jobcannon.engine.agentic_enricher._search_ddg") as mock_ddg,
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            patch("jobcannon.engine.agentic_enricher._fetch_page_text") as mock_fetch,
            patch("jobcannon.engine.agentic_enricher._validate_page") as mock_validate,
            patch("jobcannon.engine.agentic_enricher.time", mock_time),
        ):
            mock_ddg.side_effect = _ddg_side_effect
            mock_fetch.return_value = long_jd
            mock_validate.return_value = (True, 0.85)

            result = enrich_single_job(
                job_row,
                page,
                conn=None,
                config={},
                call_model=MagicMock(return_value=_make_model_result(queries)),
            )

        assert (
            result is not None
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert mock_ddg.call_count == _MAX_SEARCH_QUERIES
        assert mock_fetch.call_count == 1
        assert mock_validate.call_count == 1
        mock_time.sleep.assert_not_called()

    def test_ddg_queries_genuinely_overlap(self):
        """All DDG searches are in flight simultaneously, not merely submitted.

        Every mocked search waits on a shared barrier sized to the query count;
        the barrier only releases when all searches overlap in time. A serial
        implementation deadlocks the first call until the barrier breaks and
        the error propagates, so this test fails against any non-parallel
        regression.
        """
        import threading

        from jobcannon.engine.agentic_enricher import (
            _MAX_SEARCH_QUERIES,
            enrich_single_job,
        )

        long_jd = (
            "Acme Corp is hiring a Data Scientist. Responsibilities include "
            "building and maintaining machine learning models, partnering with "
            "engineering, and communicating insights to stakeholders. "
            "Qualifications: experience with Python, SQL, and statistics. " * 5
        )
        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()
        queries = [
            "Acme careers Data Scientist",
            "site:linkedin.com Acme Data Scientist",
            "site:greenhouse.io Acme Data Scientist",
            "site:lever.co Acme Data Scientist",
        ]

        barrier = threading.Barrier(_MAX_SEARCH_QUERIES)
        overlapped = []
        overlapped_lock = threading.Lock()

        def _ddg_side_effect(query, max_results):
            barrier.wait(timeout=5)
            with overlapped_lock:
                overlapped.append(query)
            return [
                {
                    "href": f"https://example.com/{query.replace(' ', '-')}",
                    "title": "t",
                    "body": "b",
                }
            ]

        with (
            patch("jobcannon.engine.agentic_enricher._search_ddg") as mock_ddg,
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            patch("jobcannon.engine.agentic_enricher._fetch_page_text") as mock_fetch,
            patch("jobcannon.engine.agentic_enricher._validate_page") as mock_validate,
        ):
            mock_ddg.side_effect = _ddg_side_effect
            mock_fetch.return_value = long_jd
            mock_validate.return_value = (True, 0.85)

            result = enrich_single_job(
                job_row,
                page,
                conn=None,
                config={},
                call_model=MagicMock(return_value=_make_model_result(queries)),
            )

        assert (
            result is not None
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        # Every search passed the barrier => all were in flight at once.
        assert len(overlapped) == _MAX_SEARCH_QUERIES
        assert not barrier.broken

    def test_ddg_query_exception_treated_as_empty(self):
        """A single DDG query raising does not abort the job; its results are
        treated as empty and the remaining queries still feed the fetch loop.
        """
        from jobcannon.engine.agentic_enricher import enrich_single_job

        long_jd = (
            "Acme Corp is hiring a Data Scientist. Responsibilities include "
            "building and maintaining machine learning models, partnering with "
            "engineering, and communicating insights to stakeholders. "
            "Qualifications: experience with Python, SQL, and statistics. " * 5
        )

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        def _ddg_side_effect(query, max_results):
            if "bad" in query:
                raise RuntimeError("simulated DDG failure")
            return [{"href": "https://example.com/job/1", "title": "t", "body": "b"}]

        with (
            patch("jobcannon.engine.agentic_enricher._search_ddg") as mock_ddg,
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            patch("jobcannon.engine.agentic_enricher._fetch_page_text") as mock_fetch,
            patch("jobcannon.engine.agentic_enricher._validate_page") as mock_validate,
        ):
            mock_ddg.side_effect = _ddg_side_effect
            mock_fetch.return_value = long_jd
            mock_validate.return_value = (True, 0.85)

            result = enrich_single_job(
                job_row,
                page,
                conn=None,
                config={},
                call_model=MagicMock(
                    return_value=_make_model_result(["query1", "bad query", "query3"])
                ),
            )

        assert (
            result is not None
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert mock_ddg.call_count == 3
        assert mock_fetch.call_count == 1
        assert mock_validate.call_count == 1

    def test_returns_none_when_no_urls_found(self):
        """When DDG returns no URLs, returns None immediately."""
        from jobcannon.engine.agentic_enricher import enrich_single_job

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        with patch("jobcannon.engine.agentic_enricher._search_ddg", return_value=[]):
            result = enrich_single_job(
                job_row,
                page,
                conn=None,
                config={},
                call_model=MagicMock(return_value=_make_model_result(["query1"])),
            )

        assert (
            result is None
        )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch

    def test_returns_none_for_missing_title_or_company(self):
        """Jobs with empty title or company are skipped immediately.

        call_model is a MagicMock that raises so we can also assert the early
        return happens before the function would try to call the model. If
        the early guard ever regresses, RuntimeError leaks instead of None
        being returned.  # PORT-SEAM: docstring reworded: call_model is now a plain MagicMock kwarg, not a patched module attribute
        """
        from jobcannon.engine.agentic_enricher import (
            enrich_single_job,
        )  # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)

        page = MagicMock()
        call_model = MagicMock(side_effect=RuntimeError("call_model should not be reached"))

        assert (
            enrich_single_job(
                {"title": "", "company": "Acme"}, page, conn=None, config={}, call_model=call_model  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            )
            is None
        )
        assert (
            enrich_single_job(
                {"title": "DS", "company": ""}, page, conn=None, config={}, call_model=call_model  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            )
            is None
        )


# PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
# ---------------------------------------------------------------------------
# _fetch_page_text() — LinkedIn routing
# ---------------------------------------------------------------------------


class TestFetchPageTextLinkedinRouting:
    """LinkedIn URLs should try the lightweight extractor before Playwright."""

    def test_linkedin_url_tries_lightweight_extractor_first(self):
        """LinkedIn URLs call fetch_linkedin_jd() before Playwright goto."""
        from jobcannon.engine.agentic_enricher import _fetch_page_text
        from jobcannon.engine.services import get_services

        page = MagicMock()
        long_jd = "D" * 500

        # PORT-SEAM: patch target collapses onto the same module --
        # fetch_linkedin_jd is inlined into agentic_enricher.py (L-0178
        # boundary-guard note), not imported from enrichment_tiers.
        with patch("jobcannon.engine.agentic_enricher.fetch_linkedin_jd") as mock_li:
            mock_li.return_value = long_jd

            result = _fetch_page_text(page, "https://www.linkedin.com/jobs/view/123456/")

        mock_li.assert_called_once_with("https://www.linkedin.com/jobs/view/123456/")
        # Playwright page.goto should NOT be called since LinkedIn extractor succeeded
        page.goto.assert_not_called()
        # PORT-SEAM: private `_MAX_JD_CHARS * 2` literal (16000) is stale even
        # privately (JD_STORAGE_MAX_CHARS is 50_000); assert against the
        # installed seam value instead of a magic number.
        assert result == long_jd[: get_services().jd_storage_max_chars * 2]

    def test_linkedin_extractor_failure_falls_through_to_playwright(self):
        """When LinkedIn extractor returns None, Playwright is used as fallback."""
        from jobcannon.engine.agentic_enricher import _fetch_page_text

        page = MagicMock()
        page.content.return_value = "<html><body><p>Job description</p></body></html>"
        # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)

        with (
            patch("jobcannon.engine.agentic_enricher.fetch_linkedin_jd") as mock_li,
            patch("jobcannon.engine.agentic_enricher.is_short_auth_page", return_value=False),
            patch("jobcannon.engine.agentic_enricher.is_chrome_or_login_page", return_value=False),  # PORT-SEAM: patch target moves to agentic_enricher (public module consolidates the private enrichment_tiers helpers)
        ):
            mock_li.return_value = None  # LinkedIn extractor fails  # PORT-SEAM: patch target moves to agentic_enricher (public module consolidates the private enrichment_tiers helpers)

            # PORT-SEAM: redundant comment removed during port cleanup (redundant with the docstring)
            _fetch_page_text(page, "https://www.linkedin.com/jobs/view/123456/")

        mock_li.assert_called_once()  # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
        page.goto.assert_called_once()  # Playwright was used as fallback

    def test_non_linkedin_url_skips_linkedin_extractor(self):
        """Non-LinkedIn URLs go straight to Playwright without trying LinkedIn extractor."""
        from jobcannon.engine.agentic_enricher import _fetch_page_text

        page = MagicMock()
        page.content.return_value = "<html><body>" + "A" * 500 + "</body></html>"

        with (
            patch("jobcannon.engine.agentic_enricher.fetch_linkedin_jd") as mock_li,
            patch("jobcannon.engine.agentic_enricher.is_short_auth_page", return_value=False),
            patch("jobcannon.engine.agentic_enricher.is_chrome_or_login_page", return_value=False),  # PORT-SEAM: patch target moves to agentic_enricher (public module consolidates the private enrichment_tiers helpers)
        ):
            mock_li.return_value = None  # PORT-SEAM: patch target moves to agentic_enricher (public module consolidates the private enrichment_tiers helpers)

            _fetch_page_text(page, "https://boards.greenhouse.io/acme/jobs/1")

        mock_li.assert_not_called()
        page.goto.assert_called_once()


# ---------------------------------------------------------------------------
# _fetch_page_text() — networkidle early-exit (Issue #1124)
# ---------------------------------------------------------------------------


class TestFetchPageTextNetworkidle:
    """Playwright load wait should exit early on networkidle, not burn a fixed sleep."""

    def test_uses_wait_for_load_state_networkidle(self):
        """_fetch_page_text calls wait_for_load_state('networkidle') and never wait_for_timeout."""
        from jobcannon.engine.agentic_enricher import _PAGE_LOAD_WAIT_MS, _fetch_page_text

        page = MagicMock()
        page.content.return_value = "<html><body>" + "A" * 500 + "</body></html>"

        with (
            patch("jobcannon.engine.agentic_enricher.fetch_linkedin_jd", return_value=None),
            patch("jobcannon.engine.agentic_enricher.is_short_auth_page", return_value=False),
            patch("jobcannon.engine.agentic_enricher.is_chrome_or_login_page", return_value=False),  # PORT-SEAM: patch target moves to agentic_enricher (public module consolidates the private enrichment_tiers helpers)
        ):
            _fetch_page_text(
                page, "https://boards.greenhouse.io/acme/jobs/1"
            )  # PORT-SEAM: patch target moves to agentic_enricher (public module consolidates the private enrichment_tiers helpers)

        page.goto.assert_called_once()
        page.wait_for_load_state.assert_called_once_with("networkidle", timeout=_PAGE_LOAD_WAIT_MS)
        page.wait_for_timeout.assert_not_called()

    def test_swallows_networkidle_timeout(self):
        """A networkidle timeout is swallowed; the page content is still extracted.

        The private test imported
        ``from playwright.sync_api import TimeoutError as PlaywrightTimeoutError``
        at module scope and raised that class here. playwright is not
        installed in this dev venv (confirmed: `uv sync` here does not pull
        it in), so a module-level playwright import would fail collection.
        The public `_wait_for_networkidle` already handles this: it imports
        `PlaywrightTimeoutError` lazily inside a try/except and falls back to
        bare `Exception` when playwright is absent — so raising a generic
        built-in `TimeoutError` here exercises the exact same swallow path
        this test targets, regardless of whether playwright happens to be
        installed in the environment running this suite (see the
        `# PORT-SEAM:` comment on the substitution below).
        """
        from jobcannon.engine.agentic_enricher import _PAGE_LOAD_WAIT_MS, _fetch_page_text

        page = MagicMock()
        page.content.return_value = "<html><body>" + "A" * 500 + "</body></html>"
        page.wait_for_load_state.side_effect = TimeoutError("timeout")  # PORT-SEAM: builtin TimeoutError replaces PlaywrightTimeoutError (playwright not installed in this dev venv)

        with (
            patch(
                "jobcannon.engine.agentic_enricher.fetch_linkedin_jd", return_value=None
            ),  # PORT-SEAM: builtin TimeoutError replaces PlaywrightTimeoutError (playwright not installed in this dev venv)
            patch("jobcannon.engine.agentic_enricher.is_short_auth_page", return_value=False),
            patch("jobcannon.engine.agentic_enricher.is_chrome_or_login_page", return_value=False),
        ):
            result = _fetch_page_text(page, "https://boards.greenhouse.io/acme/jobs/1")
        # PORT-SEAM: patch target moves to agentic_enricher (public module consolidates the private enrichment_tiers helpers)
        page.wait_for_load_state.assert_called_once_with("networkidle", timeout=_PAGE_LOAD_WAIT_MS)
        assert result is not None


# PORT-SEAM: private issue cross-references redacted from public prose
class TestAgenticEnricherImportWithoutPlaywright:
    """Playwright is not a core dependency (only transitive via the dev extra's
    pytest-playwright); every playwright import in this module must be lazy and
    guarded so a non-dev install never crashes on module import.

    Simulates absence via a fake site-packages directory prepended to
    PYTHONPATH, containing a ``playwright`` stub that raises ImportError on
    import. Runs in a subprocess so the fake package genuinely shadows any
    real playwright install and no playwright state leaks into other tests
    via sys.modules.
    """

    def test_import_and_fetch_survive_missing_playwright(
        self, tmp_path
    ):  # PORT-SEAM: private-repo cross-file reference redacted (test does not exist publicly)
        """Import succeeds, and _fetch_page_text's timeout path degrades
        without NameError/ModuleNotFoundError, when playwright is absent."""
        import subprocess
        import sys
        import textwrap
        # PORT-SEAM: subprocess_lockdown.py not carried; see module docstring for why

        fake_site = tmp_path / "fake_site"
        # PORT-SEAM: subprocess_lockdown.py not carried; see module docstring for why
        playwright_pkg = fake_site / "playwright"
        playwright_pkg.mkdir(parents=True)
        (playwright_pkg / "__init__.py").write_text(
            textwrap.dedent("""\
                raise ImportError("playwright not installed (stub for test)")
            """),
            encoding="utf-8",
        )

        env = os.environ.copy()
        existing_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(fake_site) + (os.pathsep + existing_path if existing_path else "")

        code = textwrap.dedent("""
            import sys
            from unittest.mock import MagicMock, patch

            import jobcannon.engine.agentic_enricher as m

            leaked = [
                name for name in sys.modules
                if name == "playwright" or name.startswith("playwright.")
            ]
            assert not leaked, f"playwright imported at module load: {leaked}"

            import contextlib  # PORT-SEAM: adapted for the public ScanServices/call_model seam (subprocess script installs ScanServices directly since it runs outside the parent process's autouse fixture)

            from jobcannon.engine import services as _services_mod

            @contextlib.contextmanager
            def _factory(*, synchronous="FULL"):
                yield None

            _services_mod.set_services(
                _services_mod.ScanServices(
                    connection_factory=_factory,
                    upsert_job=MagicMock(),
                    set_jd_full=MagicMock(),
                    upsert_company=MagicMock(),
                    config={},
                    get_secret=MagicMock(return_value=None),
                    jd_storage_max_chars=50_000,
                )
            )

            page = MagicMock()
            page.content.return_value = "<html><body>" + "A" * 500 + "</body></html>"
            # No real PlaywrightTimeoutError class exists in this process; a  # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
            # generic TimeoutError stands in for "the wait timed out".
            page.wait_for_load_state.side_effect = TimeoutError("simulated timeout")

            with (
                patch("jobcannon.engine.agentic_enricher.fetch_linkedin_jd", return_value=None),
                patch("jobcannon.engine.agentic_enricher.is_short_auth_page", return_value=False),
                patch("jobcannon.engine.agentic_enricher.is_chrome_or_login_page", return_value=False),  # PORT-SEAM: patch target moves to agentic_enricher (public module consolidates the private enrichment_tiers helpers)
            ):
                result = m._fetch_page_text(page, "https://boards.greenhouse.io/acme/jobs/1")  # PORT-SEAM: patch target moves to agentic_enricher (public module consolidates the private enrichment_tiers helpers)
            assert result is not None, f"expected degraded-but-successful fetch, got {result!r}"
            print("REGRESSION_TEST_OK")
        """)
        # PORT-SEAM: private repo wrapped `env` with
        # `tests.helpers.subprocess_lockdown.locked_down_env(env)`. That
        # helper's own precondition (`tests/_subprocess_lockdown/` must
        # exist) is unmet here — this repo has no repo-wide network-lockdown
        # convention (grepped `tests/` for `network_lockdown` /
        # `_subprocess_lockdown`: zero hits) — so it is not carried (see
        # module docstring point 8). Spawn with the plain fake-site env instead.
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_HANG_TIMEOUT_S,
            env=env,  # PORT-SEAM: subprocess_lockdown.py not carried; see module docstring for why
        )
        assert result.returncode == 0, (
            f"agentic_enricher import/fetch crashed without playwright.\n"  # PORT-SEAM: subprocess_lockdown.py not carried; see module docstring for why
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "REGRESSION_TEST_OK" in result.stdout


# ---------------------------------------------------------------------------
# enrich_single_job() — Company bypass and observability
# ---------------------------------------------------------------------------


class TestEnrichSingleJobObservability:
    """Tests for failure reason tracking and company-name bypass."""

    def test_company_bypass_for_long_pages_with_short_names(self):
        """Long pages with short company names: the company-token bypass still
        fires, but #1892 closes the acceptance gap it used to open (see
        jd_content_contract.py's own comments for the full #1892/#1813
        rationale -- both issue numbers are already public there, since it
        documents the already-landed public module's own behavior).
        """
        from jobcannon.engine.agentic_enricher import enrich_single_job

        # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
        long_text = (
            "We are hiring a Data Scientist to join our growing analytics team. "
            # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
            "Responsibilities include building predictive models, partnering "
            "with engineering and product teams, and communicating insights to "
            "stakeholders. Qualifications: experience with Python, SQL, and "
            "statistical modeling. " * 10
        )

        job_row = {
            "title": "Data Scientist",
            "company": "Zo",
        }  # 2-char company → "zo" stem under #1892 (see jd_content_contract.py)
        page = MagicMock()

        with (  # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
            patch("jobcannon.engine.agentic_enricher._search_ddg") as mock_ddg,
            patch("jobcannon.engine.agentic_enricher._fetch_page_text") as mock_fetch,
            # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        ):
            mock_ddg.return_value = [
                {"href": "https://example.com/job/1", "title": "t", "body": "b"}
            ]
            mock_fetch.return_value = long_text

            result = enrich_single_job(
                job_row,
                page,
                conn=None,
                config={},
                call_model=MagicMock(
                    side_effect=[
                        _make_model_result(["query1"]),
                        _make_model_result(
                            {"is_match": True, "confidence": 0.85, "reason": "match"}
                        ),
                    ]
                ),
            )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch

        assert result is None, (
            "enrich_single_job must reject a long page whose body never mentions "  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            "the 2-char company — #1892's company_absent gate closes the long-page "
            "bypass for short names that previously yielded no distinctive stem"
        )

    def test_failure_stats_logged(self, caplog):
        """Failure breakdown is logged at INFO level."""
        import logging

        from jobcannon.engine.agentic_enricher import enrich_single_job

        job_row = {"title": "Data Scientist", "company": "Acme Corp"}
        page = MagicMock()

        # PORT-SEAM: logger name moves with the module.
        with caplog.at_level(logging.INFO, logger="jobcannon.engine.agentic_enricher"):
            with (
                patch("jobcannon.engine.agentic_enricher._search_ddg") as mock_ddg,
                patch("jobcannon.engine.agentic_enricher._fetch_page_text") as mock_fetch,
                # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            ):
                mock_ddg.return_value = [
                    {"href": "https://example.com/job/1", "title": "t", "body": "b"},
                ]
                mock_fetch.return_value = None  # All fetches fail → auth_wall

                enrich_single_job(
                    job_row,
                    page,
                    conn=None,
                    config={},
                    call_model=MagicMock(return_value=_make_model_result(["query1"])),
                )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch

        # Check that the INFO-level failure breakdown was logged
        # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        info_messages = [
            r.message for r in caplog.records if r.levelno == logging.INFO
        ]  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
        assert any(
            "urls=" in msg and "fetched=" in msg and "auth_wall=" in msg for msg in info_messages
        )


# ---------------------------------------------------------------------------
# _is_social_post_url() and _rank_urls() — social-surface URL filtering  # PORT-SEAM: test class relocated later in the file during the port (unchanged content -- see TestRunAgenticBackfillIsolation below); private issue cross-reference redacted from public prose
# ---------------------------------------------------------------------------


class TestSocialPostUrlFilter:
    """Social-post URL path patterns are filtered by _rank_urls."""

    def test_linkedin_posts_url_is_social(self):
        from jobcannon.engine.agentic_enricher import _is_social_post_url

        assert _is_social_post_url(
            "https://www.linkedin.com/posts/lindsaybrothers_senior-product-manager"
        )

    def test_linkedin_jobs_url_is_not_social(self):
        """linkedin.com/jobs/ is a valid JD source and must NOT be filtered."""
        from jobcannon.engine.agentic_enricher import _is_social_post_url

        assert not _is_social_post_url("https://www.linkedin.com/jobs/view/123456/")

    def test_twitter_status_url_is_social(self):
        from jobcannon.engine.agentic_enricher import _is_social_post_url

        assert _is_social_post_url("https://twitter.com/status/123456789")
        assert _is_social_post_url("https://x.com/status/123456789")

    def test_non_social_urls_not_filtered(self):
        from jobcannon.engine.agentic_enricher import _is_social_post_url

        assert not _is_social_post_url("https://boards.greenhouse.io/acme/jobs/1")
        assert not _is_social_post_url("https://lever.co/acme/data-scientist")
        assert not _is_social_post_url("https://www.acme.com/careers/data-scientist")

    def test_rank_urls_excludes_linkedin_posts(self):
        """_rank_urls must not return linkedin.com/posts/ URLs in candidate pool."""
        from jobcannon.engine.agentic_enricher import _rank_urls

        search_results = [
            {"href": "https://www.linkedin.com/posts/someone_senior-product-manager-xyz"},
            {"href": "https://boards.greenhouse.io/acme/jobs/ds-123"},
        ]
        urls = _rank_urls(search_results)

        assert "https://www.linkedin.com/posts/someone_senior-product-manager-xyz" not in urls, (
            "linkedin.com/posts/ URL must be filtered by _rank_urls"
        )
        assert "https://boards.greenhouse.io/acme/jobs/ds-123" in urls, (
            "Greenhouse ATS URL must still be included"
        )


# ---------------------------------------------------------------------------
# _search_ddg() — DuckDuckGo search timeout  # PORT-SEAM: private issue cross-reference redacted from public prose
# ---------------------------------------------------------------------------


class TestSearchDdg:
    """DDGS constructor receives the shared timeout and exceptions are caught."""

    def test_ddgs_called_with_timeout(self):
        """DDGS constructor in _search_ddg receives the shared timeout value."""
        from jobcannon.engine._http_constants import _TIMEOUT
        from jobcannon.engine.agentic_enricher import _search_ddg

        with patch("ddgs.DDGS") as MockDDGS:
            mock_ddgs_instance = MagicMock()
            mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
            mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
            mock_ddgs_instance.text.return_value = []
            MockDDGS.return_value = mock_ddgs_instance

            _search_ddg("Data Scientist Acme Corp", max_results=3)

        assert MockDDGS.call_count == 1
        assert MockDDGS.call_args.kwargs.get("timeout") == _TIMEOUT

    def test_timeout_exception_logs_debug_and_continues(self, caplog):
        """httpx.TimeoutException from DDGS.text is caught and logged at DEBUG."""
        import logging

        import httpx

        from jobcannon.engine.agentic_enricher import _search_ddg

        with (
            patch("ddgs.DDGS") as MockDDGS,
            caplog.at_level(logging.DEBUG, logger="jobcannon.engine.agentic_enricher"),
        ):
            mock_ddgs_instance = MagicMock()
            mock_ddgs_instance.__enter__ = MagicMock(return_value=mock_ddgs_instance)
            mock_ddgs_instance.__exit__ = MagicMock(return_value=False)
            mock_ddgs_instance.text.side_effect = httpx.TimeoutException("request timed out")
            MockDDGS.return_value = mock_ddgs_instance

            result = _search_ddg("Data Scientist Acme Corp", max_results=3)

        assert result == []
        debug_messages = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("DDG search failed" in r.message for r in debug_messages)


# ---------------------------------------------------------------------------
# requeue_or_expire_agentic_exhausted() — bounded retry + expiry (T2.9 / D21,
# already public in agentic_enricher.py's own comments)
# ---------------------------------------------------------------------------


class TestRequeueOrExpireAgenticExhausted:
    """Bounded retry / terminal expiry sweep for 'agentic_exhausted' rows.

    requeue_or_expire_agentic_exhausted(conn, config) takes a raw sqlite3
    connection directly — no ScanServices seam — so these tests need no
    _install_services() call beyond the autouse default.
    """

    @staticmethod
    def _set_exhausted(conn, dedup_key, *, exhausted_at, retry_count):
        conn.execute(
            "UPDATE jobs SET enrichment_tier = 'agentic_exhausted', "
            "agentic_exhausted_at = ?, agentic_retry_count = ? WHERE dedup_key = ?",
            (exhausted_at, retry_count, dedup_key),
        )
        conn.commit()

    def test_cooldown_not_elapsed_leaves_row_untouched(self, migrated_db_mem):
        from datetime import UTC, datetime, timedelta

        from jobcannon.engine.agentic_enricher import requeue_or_expire_agentic_exhausted

        conn = migrated_db_mem
        _insert_job(conn, "test|job|cooldown-not-elapsed")
        recent = (datetime.now(UTC) - timedelta(days=5)).replace(tzinfo=None).isoformat()
        self._set_exhausted(
            conn, "test|job|cooldown-not-elapsed", exhausted_at=recent, retry_count=0
        )

        requeued, expired = requeue_or_expire_agentic_exhausted(conn, {})

        assert requeued == 0
        assert expired == 0
        row = conn.execute(
            "SELECT enrichment_tier, agentic_retry_count FROM jobs WHERE dedup_key = ?",
            ("test|job|cooldown-not-elapsed",),
        ).fetchone()
        assert row["enrichment_tier"] == "agentic_exhausted"
        assert row["agentic_retry_count"] == 0

    def test_cooldown_elapsed_requeues_for_retry(self, migrated_db_mem):
        from datetime import UTC, datetime, timedelta

        from jobcannon.engine.agentic_enricher import requeue_or_expire_agentic_exhausted

        conn = migrated_db_mem
        _insert_job(conn, "test|job|cooldown-elapsed")
        stale = (datetime.now(UTC) - timedelta(days=31)).replace(tzinfo=None).isoformat()
        self._set_exhausted(conn, "test|job|cooldown-elapsed", exhausted_at=stale, retry_count=0)

        requeued, expired = requeue_or_expire_agentic_exhausted(conn, {})

        assert requeued == 1
        assert expired == 0
        row = conn.execute(
            "SELECT enrichment_tier, agentic_retry_count, agentic_exhausted_at "
            "FROM jobs WHERE dedup_key = ?",
            ("test|job|cooldown-elapsed",),
        ).fetchone()
        assert row["enrichment_tier"] is None, "reset to NULL re-enters the regular pipeline"
        assert row["agentic_retry_count"] == 1
        assert row["agentic_exhausted_at"] is None

    def test_legacy_null_timestamp_requeues_immediately(self, migrated_db_mem):
        """Pre-existing agentic_exhausted rows (from before this column shipped)
        have NULL agentic_exhausted_at — treated as cooldown-satisfied so the
        stuck backlog gets its first retry on the very first sweep instead of
        waiting another 30 days for a timestamp that never existed."""  # PORT-SEAM: private-internal ticket/phase reference redacted from public prose
        from jobcannon.engine.agentic_enricher import requeue_or_expire_agentic_exhausted

        conn = migrated_db_mem
        _insert_job(conn, "test|job|legacy-null-ts")
        self._set_exhausted(conn, "test|job|legacy-null-ts", exhausted_at=None, retry_count=0)

        requeued, expired = requeue_or_expire_agentic_exhausted(conn, {})

        assert requeued == 1
        assert expired == 0
        row = conn.execute(
            "SELECT enrichment_tier FROM jobs WHERE dedup_key = ?",
            ("test|job|legacy-null-ts",),
        ).fetchone()
        assert row["enrichment_tier"] is None

    def test_budget_exhausted_expires_regardless_of_cooldown(self, migrated_db_mem):
        """A row that already used up its retry budget moves straight to the
        terminal 'expired' tier — no further cooldown wait, there is nothing
        left to retry (never an infinite retry loop)."""
        from datetime import UTC, datetime

        from jobcannon.engine.agentic_enricher import requeue_or_expire_agentic_exhausted

        conn = migrated_db_mem
        _insert_job(conn, "test|job|budget-exhausted")
        recent = datetime.now(UTC).replace(tzinfo=None).isoformat()
        self._set_exhausted(conn, "test|job|budget-exhausted", exhausted_at=recent, retry_count=2)

        requeued, expired = requeue_or_expire_agentic_exhausted(
            conn, {"agentic": {"retry_max_attempts": 2}}
        )

        assert requeued == 0
        assert expired == 1
        row = conn.execute(
            "SELECT enrichment_tier FROM jobs WHERE dedup_key = ?",
            ("test|job|budget-exhausted",),
        ).fetchone()
        assert row["enrichment_tier"] == "expired"

    def test_config_overrides_cooldown_and_budget(self, migrated_db_mem):
        """agentic.retry_cooldown_days / agentic.retry_max_attempts are honored
        over the code defaults (30 days / 2 retries)."""
        from datetime import UTC, datetime, timedelta

        from jobcannon.engine.agentic_enricher import requeue_or_expire_agentic_exhausted

        conn = migrated_db_mem
        _insert_job(conn, "test|job|config-override")
        # 2 days old would NOT clear the 30-day default cooldown, but does
        # clear a config-overridden 1-day cooldown.
        two_days_ago = (datetime.now(UTC) - timedelta(days=2)).replace(tzinfo=None).isoformat()
        self._set_exhausted(
            conn, "test|job|config-override", exhausted_at=two_days_ago, retry_count=0
        )

        requeued, expired = requeue_or_expire_agentic_exhausted(
            conn, {"agentic": {"retry_cooldown_days": 1, "retry_max_attempts": 2}}
        )

        assert requeued == 1
        assert expired == 0

    def test_expired_rows_excluded_from_enrichment_backfill_selection(self, migrated_db_mem):
        """The real consumer: a backfill base_sql built from backfill_skip_sql()
        must never re-select an 'expired' row — exercised against actual SQL
        rather than just asserting enum-set membership in isolation.

        The private test imported `job_finder.enrichment_states.backfill_skip_sql`;
        the ported function lives at `jobcannon.engine.enrichment_states.backfill_skip_sql`
        (unchanged signature/behavior).
        """
        # PORT-SEAM: public import path for backfill_skip_sql (unchanged behavior)
        from jobcannon.engine.enrichment_states import backfill_skip_sql

        conn = migrated_db_mem
        _insert_job(conn, "test|job|expired-excluded", enrichment_tier="expired", jd_full=None)

        base_sql = f"""SELECT * FROM jobs
               WHERE (enrichment_tier IS NULL
                      OR {backfill_skip_sql()})
                 AND (jd_full IS NULL OR jd_full = ''
                      OR salary_min IS NULL
                      OR location IS NULL OR location = '')"""
        rows = conn.execute(base_sql).fetchall()
        assert not any(r["dedup_key"] == "test|job|expired-excluded" for r in rows)


# ---------------------------------------------------------------------------
# run_agentic_backfill() — per-job isolation and junk-JD gate  # PORT-SEAM: private issue cross-reference redacted from public prose
# ---------------------------------------------------------------------------


class TestRunAgenticBackfillIsolation:
    """Per-job exception isolation and junk-gate pre-write check."""

    def _setup_playwright_mocks(self):
        """Return (mock_playwright_mod, mock_pw_ctx) for sys.modules patching."""
        mock_pw_ctx = MagicMock()
        mock_pw_ctx.__enter__ = MagicMock(
            return_value=MagicMock()
        )  # PORT-SEAM: private-internal ticket/phase reference redacted from public prose
        mock_pw_ctx.__exit__ = MagicMock(return_value=False)
        mock_playwright_mod = MagicMock()
        mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx
        return mock_playwright_mod, mock_pw_ctx

    def test_per_job_exception_does_not_abort_batch(self, migrated_db_path):
        """One job's exception (e.g. a DB IntegrityError) must not abort the batch.

        Setup: 2 exhausted jobs — job 1 raises RuntimeError during enrich,
        job 2 returns a valid JD. Expect result == 1 (job 2 enriched).
        """
        from jobcannon.engine.agentic_enricher import (
            run_agentic_backfill,
        )  # PORT-SEAM: private-internal ticket/phase reference redacted from public prose

        path, conn = _make_migrated_db(migrated_db_path)
        try:
            _insert_job(conn, "acme|job1|remote", title="Job One", enrichment_tier="exhausted")
            _insert_job(conn, "acme|job2|remote", title="Job Two", enrichment_tier="exhausted")
            conn.close()

            long_jd = "Full job description text for the second job. " * 20

            call_count = [0]

            # PORT-SEAM: **_kw absorbs the call_model= kwarg run_agentic_backfill
            # now always passes to enrich_single_job.
            def _enrich_side_effect(job, page, *, conn, config, **_kw):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("Simulated per-job failure")
                return long_jd

            mock_playwright_mod, _ = self._setup_playwright_mocks()
            # PORT-SEAM: connection_factory bound to the real on-disk path
            # replaces db_helpers.standalone_connection patching.  # PORT-SEAM: private-internal ticket/phase reference redacted from public prose
            _install_services(path, call_model=MagicMock())

            with (
                patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}),
                patch("jobcannon.engine.agentic_enricher._create_browser") as mock_browser,
                patch(
                    "jobcannon.engine.agentic_enricher.enrich_single_job",
                    side_effect=_enrich_side_effect,
                ),
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                # PORT-SEAM: db_path dropped from the public signature --
                # connection_factory (installed above) is zero-arg.
                result = run_agentic_backfill({}, limit=10, call_model=MagicMock())

            assert result == 1, (
                f"Expected 1 job enriched (job 2 should survive job 1's exception), got {result}"
            )
            assert call_count[0] == 2, (
                f"enrich_single_job should be called twice, got {call_count[0]}"
            )

            verify_conn = sqlite3.connect(path)
            verify_conn.row_factory = sqlite3.Row
            rows = {
                r["dedup_key"]: dict(r)
                for r in verify_conn.execute(
                    "SELECT dedup_key, enrichment_tier, jd_full FROM jobs"
                ).fetchall()
            }
            verify_conn.close()

            assert rows["acme|job2|remote"]["enrichment_tier"] == "agentic", (
                "Job 2 must be marked 'agentic' even though job 1 raised an exception"
            )
            assert rows["acme|job2|remote"]["jd_full"] == long_jd
        # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_junk_jd_not_written_enrichment_tier_unchanged(self, migrated_db_path):
        """A junk JD (fails the content-density gate) must not be written to the DB.
        set_jd_full() gates the write; it does NOT side-effect on
        enrichment_tier. enrichment_tier stays 'exhausted' so the job may be
        retried; jd_full stays NULL so no junk reaches the scorer.
        """
        from jobcannon.engine.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db(migrated_db_path)
        try:  # PORT-SEAM: private-internal ticket/phase reference redacted from public prose
            _insert_job(conn, "acme|ds|remote", enrichment_tier="exhausted")
            conn.close()

            # Junk JD: too short to pass _is_jd_junk (< 200 chars post-strip).
            junk_jd = "sign in to view this job"

            mock_playwright_mod, _ = self._setup_playwright_mocks()
            _install_services(path, call_model=MagicMock())
            # PORT-SEAM: private-internal ticket/phase reference redacted from public prose

            with (
                patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}),
                patch("jobcannon.engine.agentic_enricher._create_browser") as mock_browser,
                patch(  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
                    "jobcannon.engine.agentic_enricher.enrich_single_job",
                    return_value=junk_jd,
                ),
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                result = run_agentic_backfill({}, limit=10, call_model=MagicMock())

            assert result == 0, "Junk JD must not count as a successful enrichment"

            verify_conn = sqlite3.connect(path)
            verify_conn.row_factory = sqlite3.Row  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            row = dict(
                verify_conn.execute(
                    "SELECT enrichment_tier, jd_full FROM jobs WHERE dedup_key = 'acme|ds|remote'"
                ).fetchone()
            )
            verify_conn.close()

            assert row["enrichment_tier"] == "exhausted", (
                "set_jd_full gate hit must NOT side-effect on enrichment_tier"
            )
            assert row["jd_full"] is None, "Junk JD content must NOT be written to jd_full"

        finally:
            if os.path.exists(path):
                os.remove(path)

    # PORT-SEAM: private-internal ticket/phase reference redacted from public prose
    def test_set_jd_full_receives_full_untruncated_title(self, migrated_db_path):
        """run_agentic_backfill must pass the FULL job title into set_jd_full's
        title= kwarg so a zero-overlap content-contract check runs at this
        write chokepoint (mirrors data_enricher's writers) and isn't fed a
        clipped token set.

        Regression guard: the outer loop's `title` variable is sliced to 55
        chars for log display only — a naive fix could accidentally reuse
        that truncated variable for the contract write instead of the job's
        real title.
        """
        from jobcannon.engine.agentic_enricher import run_agentic_backfill

        long_title = "Senior " * 10 + "Data Scientist"  # > 55 chars
        assert (
            len(long_title) > 55
        )  # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)

        path, conn = _make_migrated_db(migrated_db_path)
        try:
            _insert_job(conn, "acme|ds|remote", title=long_title, enrichment_tier="exhausted")
            conn.close()

            real_jd = "A" * 300 + f" {long_title} at Acme Corp requirements..."
            mock_playwright_mod, _ = self._setup_playwright_mocks()

            # PORT-SEAM: private test patched module-level `_set_jd_full`
            # (an alias for job_finder.db._jd_full.set_jd_full). No such
            # alias exists post-port — svc.set_jd_full is the seam, so the
            # override is installed on ScanServices instead.
            mock_set_jd = MagicMock(return_value=False)  # short-circuit; only call args matter here
            _install_services(path, set_jd_full=mock_set_jd, call_model=MagicMock())

            with (
                patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}),
                patch("jobcannon.engine.agentic_enricher._create_browser") as mock_browser,
                patch(
                    "jobcannon.engine.agentic_enricher.enrich_single_job",
                    return_value=real_jd,
                ),
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                run_agentic_backfill({}, limit=10, call_model=MagicMock())

            # PORT-SEAM: adapted for the public ScanServices/call_model seam (see module docstring)
            mock_set_jd.assert_called_once()
            assert mock_set_jd.call_args.kwargs.get("title") == long_title

        finally:  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
            if os.path.exists(path):
                os.remove(path)

    def test_per_job_exception_warning_logged(self, caplog, migrated_db_path):
        """Per-job exception must produce a WARNING log entry with job identity."""
        import logging

        from jobcannon.engine.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db(migrated_db_path)
        try:
            _insert_job(conn, "acme|ds|remote", title="Data Scientist", enrichment_tier="exhausted")
            conn.close()

            mock_playwright_mod, _ = self._setup_playwright_mocks()
            _install_services(path, call_model=MagicMock())

            with caplog.at_level(logging.WARNING, logger="jobcannon.engine.agentic_enricher"):
                with (
                    patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}),
                    patch(
                        "jobcannon.engine.agentic_enricher._create_browser"
                    ) as mock_browser,  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch
                    patch(
                        "jobcannon.engine.agentic_enricher.enrich_single_job",
                        side_effect=RuntimeError("Simulated IntegrityError"),
                    ),
                ):
                    mock_browser.return_value = (MagicMock(), MagicMock())
                    run_agentic_backfill({}, limit=10, call_model=MagicMock())

            warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
            assert any("Per-job error" in msg or "Simulated" in msg for msg in warning_messages), (
                f"Expected a per-job WARNING log, got: {warning_messages}"
            )  # PORT-SEAM: call_model kwarg replaces the private OllamaProvider/model_provider.call_model patch

        finally:
            if os.path.exists(path):
                os.remove(path)


class TestRunAgenticBackfill:
    def test_enriches_exhausted_jobs(self, migrated_db_path):
        """Successfully enriches one exhausted job and writes to DB."""
        from jobcannon.engine.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db(migrated_db_path)
        try:
            _insert_job(conn, "acme|ds|remote", enrichment_tier="exhausted")
            conn.close()

            long_jd = "This is a full Data Scientist job description at Acme Corp. " * 15

            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            # PORT-SEAM: OllamaProvider sys.modules injection dropped — the
            # module never constructs a provider post-port; call_model is
            # injected via _install_services / run_agentic_backfill's kwarg.
            _install_services(path, call_model=MagicMock())

            with (
                patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}),
                patch("jobcannon.engine.agentic_enricher._create_browser") as mock_browser,
                patch("jobcannon.engine.agentic_enricher.enrich_single_job") as mock_enrich,
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                mock_enrich.return_value = long_jd

                result = run_agentic_backfill({}, limit=10, call_model=MagicMock())

                mock_enrich.assert_called_once()
                enriched_job = mock_enrich.call_args[0][0]
                assert enriched_job["dedup_key"] == "acme|ds|remote", (
                    f"Expected to enrich 'acme|ds|remote', but orchestrator passed: {enriched_job.get('dedup_key')!r}"
                )
                assert enriched_job.get("enrichment_tier") == "exhausted", (
                    "Orchestrator must only select jobs with enrichment_tier='exhausted'"
                )

            verify_conn = sqlite3.connect(path)
            verify_conn.row_factory = sqlite3.Row
            row = verify_conn.execute(
                "SELECT enrichment_tier, jd_full FROM jobs WHERE dedup_key = 'acme|ds|remote'"
            ).fetchone()
            verify_conn.close()

            assert result == 1
            assert dict(row)["enrichment_tier"] == "agentic"
            assert dict(row)["jd_full"] == long_jd

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_marks_not_found_as_agentic_exhausted(self, migrated_db_path):
        """When enrich_single_job returns None, tier is set to 'agentic_exhausted'."""
        from jobcannon.engine.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db(migrated_db_path)
        try:
            _insert_job(conn, "acme|ds|remote", enrichment_tier="exhausted")
            conn.close()

            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            _install_services(path, call_model=MagicMock())

            with (
                patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}),
                patch("jobcannon.engine.agentic_enricher._create_browser") as mock_browser,
                patch("jobcannon.engine.agentic_enricher.enrich_single_job") as mock_enrich,
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                mock_enrich.return_value = None

                result = run_agentic_backfill({}, limit=10, call_model=MagicMock())

                mock_enrich.assert_called_once()
                enriched_job = mock_enrich.call_args[0][0]
                assert enriched_job["dedup_key"] == "acme|ds|remote", (
                    f"Expected to enrich 'acme|ds|remote', but orchestrator passed: {enriched_job.get('dedup_key')!r}"
                )
                assert enriched_job.get("enrichment_tier") == "exhausted", (
                    "Orchestrator must only select jobs with enrichment_tier='exhausted'"
                )

            verify_conn = sqlite3.connect(path)
            verify_conn.row_factory = sqlite3.Row
            row = verify_conn.execute(
                "SELECT enrichment_tier FROM jobs WHERE dedup_key = 'acme|ds|remote'"
            ).fetchone()
            verify_conn.close()

            assert result == 0
            assert dict(row)["enrichment_tier"] == "agentic_exhausted"

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_returns_zero_when_no_exhausted_jobs(self, migrated_db_path):
        """When there are no exhausted jobs, returns 0 without crashing."""
        from jobcannon.engine.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db(migrated_db_path)
        try:
            conn.close()

            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            _install_services(path, call_model=MagicMock())

            with patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}):
                # No jobs → returns early before ever touching playwright
                result = run_agentic_backfill({}, limit=10, call_model=MagicMock())

            assert result == 0

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_handles_malformed_sub_scores_json(self, migrated_db_path):
        """Regression test for issue #730 (already public in agentic_enricher.py's
        own comments): agentic_enricher handles
        malformed sub_scores_json without crashing."""
        from jobcannon.engine.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db(migrated_db_path)
        try:
            _insert_job(
                conn,
                "malformed|ds|remote",
                enrichment_tier="exhausted",
                sub_scores_json="not valid json",
            )
            conn.close()

            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            _install_services(path, call_model=MagicMock())

            with (
                patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}),
                patch("jobcannon.engine.agentic_enricher._create_browser") as mock_browser,
                patch("jobcannon.engine.agentic_enricher.enrich_single_job") as mock_enrich,
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                mock_enrich.return_value = None

                # Should not crash despite malformed sub_scores_json
                result = run_agentic_backfill({}, limit=10, call_model=MagicMock())

            assert result == 0

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_warning_logged_on_optimistic_concurrency_miss(self, caplog, migrated_db_path):
        """When success UPDATE rowcount == 0, WARNING is logged with dedup_key and JD length."""
        import logging

        from jobcannon.engine.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db(migrated_db_path)
        try:
            _insert_job(conn, "acme|ds|remote", enrichment_tier="exhausted")
            conn.close()

            long_jd = "Full job description for Data Scientist at Acme Corp. " * 10

            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            # PORT-SEAM: replaces the private test's counting patch on
            # db_helpers.standalone_connection — call 1 (the outer SELECT)
            # gets a real connection; every later call (the per-job
            # enrich_conn and the write_conn) gets a mock conn whose
            # execute() reports rowcount=0, simulating another process
            # advancing enrichment_tier between the SELECT and this write.
            write_call_count = [0]
            real_factory = make_connection_factory(path)

            @contextlib.contextmanager
            def _factory(*, synchronous="FULL"):
                write_call_count[0] += 1
                if write_call_count[0] == 1:
                    with real_factory(synchronous=synchronous) as c:
                        yield c
                else:
                    mock_conn = MagicMock()
                    cursor = MagicMock()
                    cursor.rowcount = 0
                    mock_conn.execute.return_value = cursor
                    yield mock_conn

            _install_services(
                connection_factory=_factory,
                set_jd_full=MagicMock(return_value=True),
                call_model=MagicMock(),
            )

            with caplog.at_level(logging.WARNING, logger="jobcannon.engine.agentic_enricher"):
                with (
                    patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}),
                    patch("jobcannon.engine.agentic_enricher._create_browser") as mock_browser,
                    patch("jobcannon.engine.agentic_enricher.enrich_single_job") as mock_enrich,
                ):
                    mock_browser.return_value = (MagicMock(), MagicMock())
                    mock_enrich.return_value = long_jd

                    result = run_agentic_backfill({}, limit=10, call_model=MagicMock())

            assert result == 0, "rowcount==0 must not count as a successful enrichment"
            warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
            assert any(
                "optimistic concurrency miss" in msg or "acme|ds|remote" in msg
                for msg in warning_messages
            )

        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_passes_open_conn_to_enrich_single_job(self, migrated_db_path):
        """Regression: each iteration must receive a fresh OPEN conn — the
        outer SELECT conn is closed at fetchall(), so it must never be reused
        for the per-job enrich call (that broke the cascade cost-recording
        write with "Cannot operate on a closed database" privately).
        """
        from jobcannon.engine.agentic_enricher import run_agentic_backfill

        path, conn = _make_migrated_db(migrated_db_path)
        try:
            _insert_job(conn, "acme|ds|remote", enrichment_tier="exhausted")
            conn.close()

            mock_pw_ctx = MagicMock()
            mock_pw_ctx.__enter__ = MagicMock(return_value=MagicMock())
            mock_pw_ctx.__exit__ = MagicMock(return_value=False)
            mock_playwright_mod = MagicMock()
            mock_playwright_mod.sync_playwright.return_value = mock_pw_ctx

            _install_services(path, call_model=MagicMock())

            received_conns: list[sqlite3.Connection] = []

            # PORT-SEAM: **_kw absorbs the call_model= kwarg the real call
            # site now always passes.
            def _capture(_job, _page, *, conn, config, **_kw):
                # The cascade calls conn.execute() on this — must be open.
                conn.execute("SELECT 1").fetchone()
                received_conns.append(conn)
                return "dummy JD" * 50

            with (
                patch.dict("sys.modules", {"playwright.sync_api": mock_playwright_mod}),
                patch("jobcannon.engine.agentic_enricher._create_browser") as mock_browser,
                patch(
                    "jobcannon.engine.agentic_enricher.enrich_single_job",
                    side_effect=_capture,
                ),
            ):
                mock_browser.return_value = (MagicMock(), MagicMock())
                run_agentic_backfill({}, limit=10, call_model=MagicMock())

            assert len(received_conns) == 1, (
                f"enrich_single_job should be called exactly once, got {len(received_conns)}"
            )

        finally:
            if os.path.exists(path):
                os.remove(path)

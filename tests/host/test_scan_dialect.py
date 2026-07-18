"""Pure-unit dialect regression tests for the ats_scanner Postgres port — no DB
required. Covers _run.py plus the sibling files (_run_html.py,
_run_playwright.py) that import _high_score_history_clause and build their
own bind-parameter lists against it (all three needed a coordinated fix: the
neutralized clause dropped from one bind parameter to zero).

Note on architecture: `_dormancy_gate_clause` and the retry-eligibility
`datetime('now')` calls are deliberately kept in SQLite dialect in the raw
engine source — tests/engine/ exercises this exact SQL directly against bare
sqlite3 with no translation layer (tests/engine/test_dormancy_cadence.py,
tests/engine/test_run_playwright.py), and Postgres-only syntax there breaks
those tests outright (`make_interval(days => ?)`'s `=>` token is a SQLite
parse error, verified empirically). jobcannon/db/compat.py's
engine_sql_to_host() is the sole Postgres-translation seam for these two
shapes — see tests/host/test_compat.py for the translation-layer tests. The
other two dialect fixes in this PR (_high_score_history_clause neutralized to
TRUE, and scan_enabled integer-literal -> TRUE/FALSE) ARE done directly in the
engine source below, because both forms are valid in SQLite and Postgres
natively — no compat-layer rewrite needed, and no tests/engine/ regression.
"""

import inspect

from jobcannon.engine.ats_scanner import _run, _run_html, _run_playwright


def test_dormancy_gate_is_sqlite_dialect_in_raw_engine_source():
    # By design (see module docstring) — the Postgres form only exists after
    # jobcannon.db.compat.engine_sql_to_host() translation, tested separately
    # in tests/host/test_compat.py.
    sql = _run._dormancy_gate_clause()
    assert "datetime('now'" in sql
    assert "make_interval" not in sql


def test_high_score_history_gate_is_neutralized():
    clause = _run._high_score_history_clause("last_scanned_at")
    assert "sub_scores_json" not in clause and clause.strip() == "TRUE"


def test_phase_a_count_sql_has_no_owner_fit_columns():
    assert "sub_scores_json" not in inspect.getsource(_run._count_phase_a_eligible)


def test_no_integer_literal_boolean_comparison_on_scan_enabled():
    # Caught live by tests/host/test_run_scan_once_smoke.py: Postgres raises
    # "operator does not exist: boolean = integer" on `scan_enabled = 1` (SQLite
    # silently accepts it since it stores booleans as 0/1). TRUE/FALSE literals
    # are valid in both dialects, so this fix (unlike the datetime one above)
    # is done directly in the engine source, not the compat layer.
    for module in (_run, _run_html, _run_playwright):
        src = inspect.getsource(module)
        assert "scan_enabled = 1" not in src
        assert "scan_enabled = 0" not in src


def test_playwright_and_html_param_lists_no_longer_bind_threshold():
    # Regression guard for the bind-parameter/placeholder-count bug: once
    # _high_score_history_clause dropped its one bind parameter, every call
    # site building a `params` list around it had to drop the threshold value
    # too, or psycopg raises on the param/placeholder count mismatch. Source
    # inspection (rather than a live DB call) keeps this test DB-free.
    playwright_src = inspect.getsource(_run_playwright)
    assert "params = [threshold]" not in playwright_src
    assert "params = [high_score_threshold]" not in playwright_src

    html_src = inspect.getsource(_run_html._run_html_fallback_scan)
    assert "params = [*non_scannable, high_score_threshold]" not in html_src

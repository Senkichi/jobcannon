"""Public-surface sentinel for the ``ats_scanner`` package.

Engine-native trim of the private repo's tests/test_package_surfaces.py: that
file also sentinels ``scheduler`` and ``careers_crawler``, neither of which
ports here (scheduler is host/APScheduler-owned; the engine's
``careers_crawler`` is only the small ``_title_contract`` /
``_title_filters`` subpackage from Task 1, not the full crawler with
``crawl_careers_batch`` etc.). The ``ats_scanner`` sentinel itself is trimmed
too: ``upsert_company`` and ``reconcile_company_ats`` are deliberately NOT
re-exported from the engine's ``ats_scanner/__init__.py`` (see that module's
docstring) — both are now host-supplied ``ScanServices`` fields resolved via
``get_services()`` per-call, not static package-level names, so dropping them
here is intentional, not a regression.

Why this exists (mirrors the private original): the ats_scanner package's
S7c module split retains a stable test-facing public surface via re-exports
in ``__init__.py``. If a future refactor drops a re-export, downstream test
files fail with a confusing ImportError; this sentinel fails first with one
clear "missing re-export" message.
"""

from __future__ import annotations


def test_ats_scanner_public_surface():
    """Names imported by ported test files must remain on the package."""
    from jobcannon.engine import ats_scanner

    required = [
        "_title_matches",
        "derive_slug_candidates",
        "probe_ats_slugs",
        "promote_ats_from_source_urls",
        "is_company_tracked",
        "run_ats_scan",
    ]
    missing = [name for name in required if not hasattr(ats_scanner, name)]
    assert not missing, (
        f"ats_scanner/ package surface missing names: {missing}. "
        "Tests will fail with ImportError or AttributeError until each is "
        "re-exported from jobcannon/engine/ats_scanner/__init__.py."
    )


def test_ats_scanner_deliberately_excludes_scan_services_fields():
    """upsert_company / reconcile_company_ats must NOT be package-level
    names in the engine — a future edit that re-adds a static top-level
    import of either would silently reintroduce a phantom
    jobcannon.engine.ats_company / ats_identity_reconcile boundary
    violation (neither module ports; both are ScanServices fields)."""
    from jobcannon.engine import ats_scanner

    assert not hasattr(ats_scanner, "upsert_company")
    assert not hasattr(ats_scanner, "reconcile_company_ats")
